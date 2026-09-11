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
    skipped unless neither nor none nobody nothing""".split()
)

#: Recall against a single replacement clause above which the two are treated as
#: the same statement for the polarity check. Lower than COVERAGE_THRESHOLD on
#: purpose: a flipped negation is worth catching even on a loose alignment.
ALIGNMENT_THRESHOLD = 0.4

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


def clause_coverage(sources: list[str], replacement: str) -> dict:
    """Which clauses of `sources` survive into `replacement`, and which invert.

    Returns `covered`, `unmatched` (the clauses no part of the replacement
    accounts for, verbatim), `polarity_changed` (clauses the replacement still
    talks about but with the negation flipped) and `ratio`. Short clauses are
    reported as `trivial` rather than as losses: "See below." carries no rule.

    A clause counts as covered when enough of its significant words appear
    anywhere in the replacement. That is deliberately a recall test on words
    rather than a similarity score on sentences: a merge is free to reorder,
    retitle and compress, and the only question being asked is whether the
    substance is still in there somewhere.

    The polarity check is separate because it is the one thing recall cannot
    see. "Always deploy on Friday" and "Never deploy on Friday" share every
    content word, so a merge that inverts a rule scores 100% coverage. Each
    source clause is therefore aligned with the replacement clause it most
    resembles, and a flip between the two is reported as the loss it is.
    """
    target_words = significant_words(replacement)
    target_norm = fingerprint(replacement)
    target_clauses = [
        (clause, significant_words(clause), is_negative(clause))
        for clause in split_clauses(replacement)
    ]
    covered: list[str] = []
    unmatched: list[str] = []
    trivial: list[str] = []
    flipped: list[dict] = []

    for source in sources:
        for clause in split_clauses(source):
            words = significant_words(clause)
            if len(words) < MIN_CLAUSE_WORDS:
                trivial.append(clause)
                continue

            exact = fingerprint(clause) in target_norm
            recall = len(words & target_words) / len(words)
            if exact or recall >= COVERAGE_THRESHOLD:
                covered.append(clause)
            else:
                unmatched.append(clause)

            aligned = _best_alignment(words, target_clauses)
            if aligned is not None and aligned[2] != is_negative(clause):
                flipped.append({"clause": clause, "became": aligned[0]})

    total = len(covered) + len(unmatched)
    return {
        "covered": covered,
        "unmatched": unmatched,
        "polarity_changed": flipped,
        "trivial": trivial,
        "ratio": round(len(covered) / total, 3) if total else 1.0,
        "clean": not unmatched and not flipped,
    }


def _best_alignment(words: set[str], target_clauses: list[tuple]):
    """The replacement clause a source clause most resembles, if any does."""
    best, best_recall = None, 0.0
    for candidate in target_clauses:
        recall = len(words & candidate[1]) / len(words) if words else 0.0
        if recall > best_recall:
            best, best_recall = candidate, recall
    return best if best_recall >= ALIGNMENT_THRESHOLD else None


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
    if coverage.get("polarity_changed"):
        problems.append(
            f"{len(coverage['polarity_changed'])} clause(s) come back with the "
            "negation flipped - the rule would be inverted, not unified"
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
