"""Diffs and clause coverage - what a digest changed, and what it dropped.

Two different questions, and the feature needs both:

`render_diff` answers "what does this change look like", line by line, so the
user can read a proposal before approving it.

`clause_coverage` answers the question a diff cannot: "did anything get LOST".
Unifying two rules into a clearer one is exactly where a business rule quietly
disappears - not by deletion, but by a tightened paragraph that no longer says
one of the things the originals said. A diff shows that as an unremarkable
removed line among many. Clause coverage names it: every normative clause of
every source has to be accounted for in the replacement, and the ones that are
not come back as `unmatched`, verbatim, for a human to rule on.
"""

from __future__ import annotations

import difflib
import re
import textwrap

# Sentence end, bullet, or hard line break. Lists are how most rules are
# actually written, so a bullet has to be its own clause or a dropped bullet
# reads as a formatting change.
_CLAUSE_SPLIT = re.compile(r"(?:(?<=[.!?;:])\s+|\n+|(?:^|\n)\s*[-*+•]\s+|(?:^|\n)\s*\d+[.)]\s+)")

_MARKDOWN_NOISE = re.compile(r"[*_`#>\[\]()]+")
_NON_WORD = re.compile(r"[^a-z0-9]+")

# Words carrying no distinguishing weight when comparing two clauses. Kept
# deliberately short: dropping too much is how two clauses that differ only in
# their negation ("must" vs "must not") start looking identical.
_STOPWORDS = frozenset(
    """a an the this that these those and or of to in on at for with by from as
    is are was were be been being it its their our your his her they we you i
    there here then than so such which who whom whose what when where how""".split()
)

# Crude suffix stripping, applied to both sides of every comparison. Word-exact
# matching flagged "run the tests" against "run the test suite" as a dropped
# clause, and a guard that cries wolf on every honest rewording is a guard the
# agent learns to wave through.
_STEM_SUFFIXES = ("ingly", "edly", "ing", "ies", "ied", "es", "ed", "ly", "s")

# Words that carry the polarity of a clause. These are never stemmed away and
# never treated as interchangeable: "always commit to main" and "never commit to
# main" sit 0.05 apart in embedding space (measured), which is to say the vectors
# cannot tell them apart at all. Only this can.
_NEGATION_WORDS = frozenset(
    """no not never nt cannot cant dont doesnt wont without avoid avoided
    forbidden prohibited refuse refused stop stopped exclude excluded skip
    skipped unless neither nor none nobody nothing exempt exempts exempted
    waive waived bypass bypassed disabled""".split()
)

# Words that turn a rule into a preference or carve an exception out of it.
# Their ARRIVAL is the loss: "never run a migration against production" and
# "never run a migration against production, unless the on-call lead approves"
# share every other word, so recall and the critical-token check both pass it.
_WEAKENERS = frozenset(
    """unless except optional may might could sometimes generally usually
    typically normally possible preferably ideally recommended encouraged
    discretion exception""".split()
)

# Words that ARE the rule rather than describing it. A clause's meaning turns on
# these, so each is checked individually instead of being averaged into a recall
# score - which is what made the first version of this guard useless.
#
# Measured failure it exists to catch: "must come back in under 200ms at the 99th
# percentile, measured at the load balancer" is 16 significant words, so changing
# 200ms to 500ms scores 12/16 = 0.75 recall and passed as clean. No threshold can
# catch that, because the number is one token however long the sentence is.
#
# Checked BY CLASS, not by exact word, and that is the difference between a guard
# that gets used and one that gets ignored. A faithful merge rewrites "always run
# the tests before you commit" as "run the full test suite before every commit":
# the word `always` is gone but its force is not, because `every` carries it. An
# exact-token check flags that, the agent learns the guard is noise, and the real
# losses go through with it. A number is the one thing still matched exactly -
# 200ms and 500ms are not the same rule under any phrasing.
_CRITICAL_CLASSES: dict[str, frozenset[str]] = {
    # Strength of the obligation. Losing the whole class is must -> should.
    "obligation": frozenset(
        "must shall mandatory required require requires requiring need needs"
        " always".split()
    ),
    # Permission and hedging. Losing it turns "may" into an instruction.
    "permission": frozenset(
        "may might could optional discretion allowed should recommended"
        " preferably ideally encouraged".split()
    ),
    # Universality: does it apply to everything or to one thing.
    "universal": frozenset(
        "always every all each any whenever everyone everything".split()
    ),
    # Exclusivity: "deploy only from main" is a different rule from "deploy from
    # main".
    "exclusivity": frozenset("only exclusively solely alone".split()),
    # Ordering and deadlines.
    "temporal": frozenset(
        "before after within until during prior since immediately first"
        " last".split()
    ),
}

#: Recall against a single replacement clause above which the two are treated as
#: the same statement, for the negation check. As strict as COVERAGE_THRESHOLD
#: deliberately: a looser bar aligned unrelated clauses and reported honest
#: merges as inversions, and a guard that cries wolf gets waved through.
ALIGNMENT_THRESHOLD = 0.7

#: Word support in the sources below which a replacement clause counts as NEW
#: text rather than a rewording of something that was there.
ADDED_THRESHOLD = 0.6

#: A clause shorter than this carries no rule on its own ("See below.", "Why:").
MIN_CLAUSE_WORDS = 3

#: Fraction of a source clause's significant words that must appear in the
#: replacement for the clause to count as covered. Not 1.0: a rewrite is
#: allowed to say the same thing in fewer words, and demanding every token
#: would flag every genuine improvement. Not lower either - below this, two
#: clauses that merely share a topic start counting as the same clause.
COVERAGE_THRESHOLD = 0.7


def normalize(text: str) -> str:
    """Case-folded, markdown-stripped, whitespace-collapsed text."""
    text = _MARKDOWN_NOISE.sub(" ", (text or "").lower())
    return " ".join(text.split())


def fingerprint(text: str) -> str:
    """A near-duplicate key: normalized text with all punctuation removed.

    Two memories whose fingerprints match say the same thing in the same words,
    whatever their formatting - the strongest duplicate signal there is, and the
    only one that needs no judgment.
    """
    return _NON_WORD.sub(" ", normalize(text)).strip()


def stem(word: str) -> str:
    """Strip a common inflectional suffix. Never applied to a negation word."""
    if word in _NEGATION_WORDS or len(word) < 5:
        return word
    for suffix in _STEM_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def significant_words(text: str) -> set[str]:
    """The stemmed content words of a piece of text."""
    return {
        stem(w) for w in fingerprint(text).split() if w and w not in _STOPWORDS
    }


def is_negative(text: str) -> bool:
    """Whether a clause forbids rather than requires."""
    return bool(_NEGATION_WORDS & set(fingerprint(text).split()))


def critical_tokens(text: str) -> set[str]:
    """The words and numbers a clause's meaning turns on.

    Never stemmed and never filtered as stopwords: `200ms` and `500ms` must stay
    different, and `must` versus `should` is the whole difference between a rule
    and a suggestion.
    """
    words = set(fingerprint(text).split())
    critical = {word for word in words if any(ch.isdigit() for ch in word)}
    for vocabulary in _CRITICAL_CLASSES.values():
        critical |= words & vocabulary
    return critical | (words & _NEGATION_WORDS)


def critical_profile(text: str) -> dict:
    """What a piece of text asserts, as classes plus exact figures.

    Comparing two of these says whether a rewrite changed the KIND of statement
    being made - its force, its scope, its exclusivity, its deadline, its
    polarity - independently of the words chosen to make it.
    """
    words = set(fingerprint(text).split())
    classes = {
        name for name, vocabulary in _CRITICAL_CLASSES.items() if words & vocabulary
    }
    if words & _NEGATION_WORDS:
        classes.add("negation")
    return {
        "classes": classes,
        "numbers": {word for word in words if any(ch.isdigit() for ch in word)},
        "words": words,
    }


def _class_words(profile: dict, names: set[str]) -> list[str]:
    """The actual words in `profile` that put it in each of `names`."""
    found: list[str] = []
    for name in sorted(names):
        vocabulary = _NEGATION_WORDS if name == "negation" else _CRITICAL_CLASSES[name]
        found.extend(sorted(profile["words"] & vocabulary))
    return found


def split_clauses(text: str) -> list[str]:
    """Break text into the clauses a rule is made of, in order.

    Sentences, bullets and numbered items each become one clause; fenced code is
    kept whole, because splitting a snippet on its punctuation would produce
    fragments no coverage check can match.
    """
    text = (text or "").strip()
    if not text:
        return []
    clauses: list[str] = []
    for block in _split_fenced(text):
        if block.startswith("```"):
            clauses.append(block.strip())
            continue
        for piece in _CLAUSE_SPLIT.split(block):
            piece = (piece or "").strip(" \t-*•")
            if piece:
                clauses.append(piece)
    return clauses


def _split_fenced(text: str) -> list[str]:
    """Split text into alternating prose and ``` fenced blocks, in order."""
    parts: list[str] = []
    buffer: list[str] = []
    in_fence = False

    def flush():
        if buffer and "\n".join(buffer).strip():
            parts.append("\n".join(buffer))
        buffer.clear()

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if in_fence:
                buffer.append(line)
                flush()
                in_fence = False
            else:
                flush()
                buffer.append(line)
                in_fence = True
            continue
        buffer.append(line)
    flush()
    return parts


def clause_coverage(
    sources: list[str], replacement: str, body: str | None = None,
) -> dict:
    """What survives from `sources` into `replacement`, and what quietly changed.

    Four questions, because a rule can be lost in four different ways and only
    the first one is visible in a diff as a removal:

    `unmatched`        a clause the replacement does not account for at all.
                       Word recall answers this well.
    `altered`          a clause still there, but with a word its meaning turned
                       on now missing: a threshold, a deadline, a quantifier,
                       `must` downgraded to `should`. Each of those is ONE token,
                       so no recall threshold can ever see it - they are checked
                       individually. This is the case that made the first version
                       of this guard report `clean` on eleven rule-losing edits.
    `polarity_changed` a clause that came back inverted, in either direction.
    `added`            normative text in the replacement that no source supports -
                       an obligation nobody wrote. Recall alone is blind to this,
                       since it only ever measures the sources.

    `clean` is true only when all four are empty. Short clauses are reported as
    `trivial` rather than as losses: "See below." carries no rule.

    `replacement` is the whole new text, title included, because a statement the
    author moved up into the title has not been lost. `body` is the new text
    WITHOUT the title, and it is what the `added` and `weakened` scans read - a
    new title is new text by definition, and scanning it flagged every retitled
    merge as inventing a rule. Defaults to `replacement` when there is no title
    to separate.
    """
    target_words = significant_words(replacement)
    target_norm = fingerprint(replacement)
    target_profile = critical_profile(replacement)
    target_clauses = [
        (clause, significant_words(clause), is_negative(clause))
        for clause in split_clauses(replacement)
    ]
    covered: list[str] = []
    unmatched: list[str] = []
    trivial: list[str] = []
    altered: list[dict] = []
    flipped: list[dict] = []
    source_words: set[str] = set()

    for source in sources:
        source_words |= significant_words(source)
        for clause in split_clauses(source):
            words = significant_words(clause)
            if len(words) < MIN_CLAUSE_WORDS:
                trivial.append(clause)
                continue

            exact = fingerprint(clause) in target_norm
            recall = len(words & target_words) / len(words)
            is_covered = exact or recall >= COVERAGE_THRESHOLD
            (covered if is_covered else unmatched).append(clause)

            profile = critical_profile(clause)
            lost_classes = _lost_classes(profile, target_profile)
            lost_numbers = profile["numbers"] - target_profile["numbers"]
            if is_covered and (lost_classes - {"negation"} or lost_numbers):
                lost_words = _class_words(profile, lost_classes - {"negation"})
                altered.append({
                    "clause": clause,
                    "missing": sorted(set(lost_words) | lost_numbers),
                    "changed": sorted(lost_classes - {"negation"}),
                    "why": (
                        "the replacement still covers this clause, but it no longer "
                        "makes the same KIND of statement - a threshold, a deadline, "
                        "the scope, or the strength of the obligation has gone"
                    ),
                })
            if "negation" in lost_classes:
                flipped.append({
                    "clause": clause,
                    "missing": _class_words(profile, {"negation"}),
                    "why": "the words that made this a prohibition are gone",
                })

            aligned = _best_alignment(words, target_clauses)
            if aligned is not None and aligned[2] != is_negative(clause):
                flipped.append({"clause": clause, "became": aligned[0]})

    new_text = replacement if body is None else body
    added = _added_clauses(
        [(c, significant_words(c), is_negative(c)) for c in split_clauses(new_text)],
        source_words,
    )
    weakened = _weakeners_added(sources, new_text)
    total = len(covered) + len(unmatched)
    return {
        "covered": covered,
        "unmatched": unmatched,
        "altered": altered,
        "polarity_changed": flipped,
        "added": added,
        "weakened": weakened,
        "trivial": trivial,
        "ratio": round(len(covered) / total, 3) if total else 1.0,
        "clean": not (unmatched or altered or flipped or added or weakened),
    }


def _lost_classes(profile: dict, target: dict) -> set[str]:
    """Which kinds of statement the replacement no longer makes.

    One refinement, and it is what keeps this usable: a missing `obligation`
    class counts only when the replacement hedges instead. "Every migration MUST
    be reviewed" becoming "SHOULD be reviewed" is a downgrade and is reported.
    The same clause rewritten as a plain imperative - "Review every migration
    before it runs" - carries exactly as much force in a rule block and has no
    modal at all, so flagging it would fire on most honest rewrites.
    """
    lost = profile["classes"] - target["classes"]
    if "obligation" in lost:
        hedged = "permission" in target["classes"] and "permission" not in profile["classes"]
        if not hedged:
            lost.discard("obligation")
    return lost


def _weakeners_added(sources: list[str], replacement: str) -> list[dict]:
    """Exceptions and hedges the replacement introduced, and numbers it invented.

    The other checks all ask what the sources had. This one asks what the
    replacement gained, because a rule can be dismantled by addition: append
    "unless the on-call lead approves" and every word of the original is still
    there. Scoped to exception and permission words plus new numbers - a merge is
    free to reword an obligation, so `must` arriving is not reported, but a
    threshold nobody wrote is.
    """
    source_text = " ".join(sources)
    source_all = set(fingerprint(source_text).split())
    target_all = set(fingerprint(replacement).split())

    new_hedges = sorted((_WEAKENERS & target_all) - source_all)
    new_numbers = sorted(
        word for word in target_all - source_all
        if any(ch.isdigit() for ch in word)
    )
    findings: list[dict] = []
    if new_hedges:
        findings.append({
            "words": new_hedges,
            "why": (
                "the replacement introduces an exception or a hedge that no source "
                "contains - this weakens the rule without removing a single word "
                "from it, which is why nothing else here can see it"
            ),
        })
    if new_numbers:
        findings.append({
            "words": new_numbers,
            "why": "these figures appear in the replacement and in no source memory",
        })
    return findings


def _best_alignment(words: set[str], target_clauses: list[tuple]):
    """The replacement clause a source clause most resembles, if any does.

    Used only for the negation check, and held to ALIGNMENT_THRESHOLD: a looser
    bar paired unrelated clauses and reported faithful merges as inversions.
    """
    best, best_recall = None, 0.0
    for candidate in target_clauses:
        recall = len(words & candidate[1]) / len(words) if words else 0.0
        if recall > best_recall:
            best, best_recall = candidate, recall
    return best if best_recall >= ALIGNMENT_THRESHOLD else None


def _added_clauses(target_clauses: list[tuple], source_words: set[str]) -> list[dict]:
    """Normative clauses in the replacement that no source supports.

    Coverage measures recall of the sources, so anything the replacement ADDS is
    free by construction - an invented obligation appended to a rule scored a
    perfect score. Only clauses that carry an obligation or a number are reported:
    a merge is allowed to add a heading or a connective sentence, and flagging
    those would bury the one that matters.
    """
    added: list[dict] = []
    for clause, words, _negative in target_clauses:
        if len(words) < MIN_CLAUSE_WORDS:
            continue
        support = len(words & source_words) / len(words)
        if support >= ADDED_THRESHOLD or not critical_tokens(clause):
            continue
        added.append({
            "clause": clause,
            "support": round(support, 3),
            "why": (
                "this states an obligation that no source memory says - it would "
                "be a NEW rule, written by the digest rather than by the user"
            ),
        })
    return added


def coverage_problems(coverage: dict | None) -> list[str]:
    """Human-readable reasons a coverage report blocks an approve_all.

    One place decides what "this proposal loses something" means, so the
    proposal view, the apply gate and the tests cannot disagree about it.
    """
    if not coverage:
        return []
    problems = []
    if coverage.get("unmatched"):
        problems.append(
            f"{len(coverage['unmatched'])} clause(s) of the original are not in "
            "the replacement"
        )
    if coverage.get("altered"):
        missing = sorted({
            word for entry in coverage["altered"] for word in entry.get("missing", [])
        })
        problems.append(
            f"{len(coverage['altered'])} clause(s) lost a word their meaning turned "
            f"on ({', '.join(missing)})"
        )
    if coverage.get("polarity_changed"):
        problems.append(
            f"{len(coverage['polarity_changed'])} clause(s) come back with the "
            "negation flipped - the rule would be inverted, not unified"
        )
    if coverage.get("weakened"):
        words = sorted({
            word for entry in coverage["weakened"] for word in entry.get("words", [])
        })
        problems.append(
            "the replacement introduces wording no source has "
            f"({', '.join(words)}) - an exception or figure the user never wrote"
        )
    if coverage.get("added"):
        problems.append(
            f"{len(coverage['added'])} clause(s) state an obligation no source "
            "memory contains - the digest would be writing a new rule"
        )
    return problems


def render_diff(before: str, after: str, before_label: str, after_label: str) -> str:
    """A unified diff of two blocks of text, wrapped for readability.

    Text is re-wrapped to a fixed width first. A memory is stored as one long
    paragraph per line, and diffing that line-wise reports the whole paragraph
    changed when one word did - useless for reviewing a rewrite.
    """
    diff = difflib.unified_diff(
        _wrapped(before), _wrapped(after),
        fromfile=before_label, tofile=after_label, lineterm="", n=2,
    )
    return "\n".join(diff)


def _wrapped(text: str, width: int = 72) -> list[str]:
    lines: list[str] = []
    for line in (text or "").splitlines() or [""]:
        if not line.strip():
            lines.append("")
            continue
        lines.extend(textwrap.wrap(line, width=width) or [""])
    return lines


def field_diff(before: dict, after: dict, fields: tuple[str, ...]) -> list[dict]:
    """Per-field before/after for the fields that actually changed.

    Short scalar fields (category, priority, tags) read better as a pair of
    values than as a diff, so those are reported as `old`/`new` and only the
    long text fields carry a rendered diff.
    """
    changes: list[dict] = []
    for field in fields:
        old, new = before.get(field), after.get(field)
        if new is None or old == new:
            continue
        change = {"field": field, "old": old, "new": new}
        if field in ("title", "content") and isinstance(old, str) and isinstance(new, str):
            change["diff"] = render_diff(old, new, f"a/{field}", f"b/{field}")
        changes.append(change)
    return changes
