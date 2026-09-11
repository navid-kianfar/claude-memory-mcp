"""The signals a digest can compute exactly, and nothing it has to guess at.

The split matters. A daemon can measure that two embeddings are 0.08 apart,
that a path in a rule no longer exists on disk, that a memory has never been
read once in eight months. It cannot decide whether two overlapping rules
should be one rule, which of two contradicting ones is current, or whether a
sentence written as an instruction was meant as one. That judgment needs the
codebase and the user, so it belongs to the agent reading these signals - the
same division `services/adaptation.py` already draws for imported rules.

So everything here is evidence, deliberately labelled as a candidate, with the
ids and the reason attached. Nothing here decides anything.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from memory_mcp.models import Memory, MemoryCategory, RULE_CATEGORIES
from memory_mcp.utils.diff import clause_coverage, fingerprint
from memory_mcp.utils.extraction import estimate_tokens

# Cosine distance thresholds, measured against the all-MiniLM-L6-v2 embeddings
# this project actually uses rather than guessed at:
#
#   0.00  identical text
#   0.05  a rule and its exact negation (!)
#   0.14  the same rule paraphrased
#   0.31  the same rule reworded, one carrying an extra clause
#   0.75  the same topic, a different rule
#   1.00  unrelated
#
# The 0.05 line is why no pair is ever reported on distance alone: the vectors
# genuinely cannot tell "always deploy on Friday" from "never deploy on Friday".
# Every pair goes through the polarity check below before it is called a
# duplicate.
#: Distance under which two memories are worth showing side by side.
OVERLAP_DISTANCE = 0.40
#: Distance under which two memories are near-certainly the same statement.
DUPLICATE_DISTANCE = 0.10

#: A memory this small, never read, and not a rule is usually a note that
#: outlived its moment. Characters, not tokens: it is a length test, not a cost.
LOW_SIGNAL_CHARS = 140
#: How old a memory has to be before "never read" means anything at all.
LOW_SIGNAL_AGE_DAYS = 21
#: Session summaries to keep before the rest are reported as history.
SESSION_KEEP = 3

# Language that makes a sentence an instruction rather than a record.
_NORMATIVE = (
    r"\balways\b", r"\bnever\b", r"\bmust\b", r"\bmust not\b", r"\bdo not\b",
    r"\bdon'?t\b", r"\bshould\b", r"\bshall\b", r"\brequired?\b", r"\brequires\b",
    r"\bmandatory\b", r"\bforbidden\b", r"\bavoid\b", r"\bensure\b", r"\bprefer\b",
    r"\bonly ever\b", r"\bhas to\b", r"\bneeds? to\b", r"\bcannot\b", r"\bmay not\b",
    r"\bno .{0,20} allowed\b", r"\buse \w+ instead\b",
)
# Language that makes a sentence a record of something that happened once.
_RECORD = (
    r"\bdecided\b", r"\bwe chose\b", r"\bchose\b", r"\bpicked\b", r"\bmigrated\b",
    r"\bswitched\b", r"\bwas fixed\b", r"\bfixed in\b", r"\broot cause\b",
    r"\bas of \d", r"\breleased\b", r"\bbumped\b", r"\bimplemented\b",
    r"\bdiscovered\b", r"\bmeasured\b", r"\bturned out\b", r"\bincident\b",
    r"\boutage\b", r"\bv\d+\.\d+", r"\b20\d\d-\d\d-\d\d\b", r"#\d+\b",
)
# Negation lives in utils/diff.py, which owns clause polarity for the whole
# feature - the coverage guard and this contradiction check must agree on what
# counts as a negation or they would disagree about the same pair of rules.

_NORMATIVE_RE = [re.compile(p, re.I) for p in _NORMATIVE]
_RECORD_RE = [re.compile(p, re.I) for p in _RECORD]

# A path-looking token: `src/foo/bar.py`, tests/test_x.py, .claude/hooks/x.sh.
_PATH_TOKEN = re.compile(r"(?<![\w@/])((?:[\w.-]+/)+[\w.-]+|[\w-]+\.[a-z]{1,5})(?![\w/])")
_CODE_EXTENSIONS = frozenset(
    """py ts tsx js jsx mjs cjs json yaml yml toml md sh bash zsh sql rs go kt kts
    swift java rb php cs css scss html vue svelte tf tfvars ini cfg conf lock
    txt csv proto graphql prisma""".split()
)
# Tokens that look like paths but are not: hosts, packages, versions.
_NOT_A_PATH = re.compile(
    r"^(?:https?|www\.|@|\d+\.\d+)|(?:\.com|\.org|\.io|\.dev|\.net|\.sh/)$", re.I
)


def _matches(patterns, text: str) -> list[str]:
    """Every distinct marker in `patterns` the text contains."""
    found = []
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            found.append(match.group(0).lower())
    return found


def _aware(value: datetime | None) -> datetime | None:
    """DuckDB hands back naive timestamps; compare them as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _brief(memory: Memory) -> dict:
    return {"memory_id": memory.id, "title": memory.title, "category": memory.category.value}


def candidate_paths(text: str) -> list[str]:
    """Path-like tokens in a memory's text, best effort and de-duplicated."""
    out: list[str] = []
    for raw in _PATH_TOKEN.findall(text or ""):
        token = raw.strip("`'\",.:;()[]")
        if not token or _NOT_A_PATH.search(token):
            continue
        has_dir = "/" in token
        extension = token.rsplit(".", 1)[-1].lower() if "." in token else ""
        if not has_dir and extension not in _CODE_EXTENSIONS:
            continue
        if has_dir and extension and extension not in _CODE_EXTENSIONS and "." in token:
            # A dotted host or package that happens to contain a slash.
            continue
        if token not in out:
            out.append(token)
    return out


def missing_paths(text: str, root: Path) -> list[str]:
    """Which path-like tokens in the text do not exist under `root`.

    Reported as a candidate, never as a fact about the memory: a rule may name a
    file that moved, or a path in another repository, or an example. The agent
    checks before proposing anything - which is cheap for it and impossible
    here.
    """
    missing = []
    for token in candidate_paths(text):
        target = root / token.lstrip("/")
        if not target.exists():
            missing.append(token)
    return missing


def collect_signals(
    memories: list[Memory],
    pairs: list[tuple[str, str, float]],
    project_root: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Every finding the store can stand behind, grouped by kind."""
    now = now or datetime.now(timezone.utc)
    by_id = {m.id: m for m in memories}
    signals: dict[str, list] = {
        "duplicate": [], "overlap": [], "contradiction": [], "stale_reference": [],
        "expired": [], "misfiled_rule": [], "misfiled_memory": [],
        "low_signal": [], "aged_session": [],
    }

    _pair_signals(signals, pairs, by_id)
    _item_signals(signals, memories, project_root, now)
    _session_signals(signals, memories, now)
    return signals


def _pair_signals(signals: dict, pairs, by_id: dict[str, Memory]) -> None:
    for left_id, right_id, distance in pairs:
        left, right = by_id.get(left_id), by_id.get(right_id)
        if left is None or right is None:
            continue
        same_text = fingerprint(f"{left.title} {left.content}") == fingerprint(
            f"{right.title} {right.content}"
        )
        entry = {
            "memory_ids": [left_id, right_id],
            "titles": [left.title, right.title],
            "categories": [left.category.value, right.category.value],
            "distance": round(distance, 4),
            "identical_text": same_text,
        }
        # Polarity first, and exclusively. A pair that says opposite things is a
        # contradiction, never a duplicate - and it is precisely the pair the
        # distance is most confident about, so classifying on distance first
        # would file "always X" and "never X" as the same statement twice.
        polarity = _polarity_conflict(left, right)
        if polarity:
            signals["contradiction"].append({**entry, "why": polarity})
        elif same_text or distance <= DUPLICATE_DISTANCE:
            entry["why"] = (
                "same text, whatever the formatting" if same_text
                else "near-identical wording - almost certainly one statement twice"
            )
            signals["duplicate"].append(entry)
        elif distance <= OVERLAP_DISTANCE:
            entry["why"] = (
                "close enough to be the same instruction said twice; both are in "
                "force, so whichever the agent reads last effectively wins"
            )
            signals["overlap"].append(entry)


def _polarity_conflict(left: Memory, right: Memory) -> str | None:
    """Whether two similar memories point in opposite directions.

    Clause-aligned, not document-level: the two clauses have to be about the
    same thing before their polarity is compared. A document-level test - "one
    of them contains the word never" - fires on almost every real pair, because
    a rule that adds one prohibition to another rule is not a contradiction, and
    drowning the genuine ones was worse than not looking.

    Still a candidate, never a verdict: it cannot tell a contradiction from a
    rule and its exception, so both texts go to the agent.
    """
    flips = clause_coverage(
        [left.content], f"{right.title}\n{right.content}"
    )["polarity_changed"]
    if not flips:
        return None
    pairs = "; ".join(f"{f['clause']!r} vs {f['became']!r}" for f in flips[:2])
    return (
        "these say opposite things about the same subject - candidate "
        f"contradiction ({pairs}). Embedding distance cannot see this at all, so "
        "read both and ASK THE USER which one is current. Do not pick a winner."
    )


def _item_signals(
    signals: dict, memories: list[Memory], project_root: Path | None, now: datetime
) -> None:
    root_usable = project_root is not None and project_root.is_dir()
    for memory in memories:
        text = f"{memory.title}\n{memory.content}"
        is_rule = memory.category in RULE_CATEGORIES

        expires_at = _aware(memory.expires_at)
        if expires_at and expires_at < now:
            signals["expired"].append({
                **_brief(memory), "expires_at": expires_at.isoformat(),
                "why": "its TTL has passed; it is already out of search and the rule block",
            })

        if root_usable:
            missing = missing_paths(text, project_root)
            if missing:
                signals["stale_reference"].append({
                    **_brief(memory), "missing_paths": missing,
                    "why": (
                        "names paths that are not in the project folder now - "
                        "VERIFY with your own tools before proposing anything: the "
                        "file may have moved, or belong to another repository"
                    ),
                })

        normative = _matches(_NORMATIVE_RE, text)
        record = _matches(_RECORD_RE, text)
        if not is_rule and normative and len(normative) > len(record):
            signals["misfiled_rule"].append({
                **_brief(memory), "markers": normative,
                "why": (
                    "written as a standing instruction but filed as a "
                    f"'{memory.category.value}' memory, so it is NOT in the rule "
                    "block and is not being enforced"
                ),
            })
        if is_rule and record and not normative:
            signals["misfiled_memory"].append({
                **_brief(memory), "markers": record,
                "why": (
                    "filed as a rule but reads as a record of something that "
                    "happened once; it costs rule-block tokens every session"
                ),
            })

        created_at = _aware(memory.created_at)
        age_days = (now - created_at).days if created_at else 0
        if (
            not is_rule
            and memory.category != MemoryCategory.SESSION
            and len(memory.content) < LOW_SIGNAL_CHARS
            and memory.access_count == 0
            and age_days >= LOW_SIGNAL_AGE_DAYS
        ):
            signals["low_signal"].append({
                **_brief(memory), "chars": len(memory.content),
                "age_days": age_days, "tokens": estimate_tokens(text),
                "why": "short, never recalled, and old enough that it is unlikely to be",
            })


def _session_signals(signals: dict, memories: list[Memory], now: datetime) -> None:
    """Session summaries beyond the few that are still useful.

    Session start reads the most recent summary. The ones behind it are history:
    worth keeping while they are recent, worth folding into one architecture or
    decision memory once they are not, because each one costs tokens on every
    search that touches its topic.
    """
    sessions = [m for m in memories if m.category == MemoryCategory.SESSION]
    if len(sessions) <= SESSION_KEEP:
        return
    sessions.sort(key=lambda m: _aware(m.created_at) or now, reverse=True)
    for memory in sessions[SESSION_KEEP:]:
        created_at = _aware(memory.created_at)
        signals["aged_session"].append({
            **_brief(memory),
            "age_days": (now - created_at).days if created_at else None,
            "tokens": estimate_tokens(f"{memory.title}\n{memory.content}"),
            "why": (
                f"session summary behind the most recent {SESSION_KEEP}; anything "
                "in it that still matters belongs in a decision or architecture "
                "memory, not in a session log"
            ),
        })
