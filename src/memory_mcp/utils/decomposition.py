"""Notice when one task's description is really several tasks.

WHY: the user, looking at the board on 2026-09-05 - "i have seen tasks with
huge description, logically that much description usually requires couple of
things to be fixed for. so it make sence to make them sub tasks."

That is the decomposition rule stated from the other end. The rule says a part
that could be assigned to someone else, or finished on a different day, deserves
its own record; this says how to SPOT one you have already written - it is
sitting in a description as item 3 of 5.

A HINT, NOT A GATE. It rides back in the tool result next to the task, while the
agent still has the context to act on it. It never refuses a write and never
rewrites a description: judgement about whether two paragraphs are two
deliverables belongs to the model, and a validator that guessed would be wrong
often enough to be ignored - which is worse than saying nothing.

WHAT MAKES IT FIRE, and why it is not just length. A long single explanation is
a GOOD description; nagging about it teaches the agent to write short ones,
which is the opposite of what the project wants. So both must hold:

- the description is long enough that a reader would skim it, and
- it is DIVIDED - numbered items, checkboxes, or several headings.

Division is the real signal. Five numbered items in 1500 characters is a plan
with five deliverables written as prose. Fifteen hundred characters of
continuous argument about one change is exactly what a task description is for.
"""

from __future__ import annotations

import re

__all__ = ["decomposition_hint", "MIN_CHARS", "MIN_SIGNALS"]

#: Below this a description is not long enough for anything to hide in it.
MIN_CHARS = 1200
#: Fewer divisions than this and it reads as one thing with some structure.
MIN_SIGNALS = 3

_NUMBERED = re.compile(r"^\s{0,3}\d+[.)]\s+\S", re.M)
_CHECKBOX = re.compile(r"^\s{0,3}[-*+]\s+\[[ xX]\]\s+\S", re.M)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.M)


def decomposition_hint(
    description: str | None, *, has_parent: bool = False, child_count: int = 0,
) -> str | None:
    """One sentence when a description looks like several deliverables, else None.

    `has_parent` silences it for a sub-task: one level is the limit, so there is
    nothing it could be decomposed INTO. `child_count` silences it for a task
    that has already been decomposed - the long description is then the parent's
    overview, which is what a parent is supposed to have.
    """
    text = (description or "").strip()
    if has_parent or child_count or len(text) < MIN_CHARS:
        return None

    numbered = len(_NUMBERED.findall(text))
    checkboxes = len(_CHECKBOX.findall(text))
    headings = len(_HEADING.findall(text))
    signals = numbered + checkboxes + headings
    if signals < MIN_SIGNALS:
        return None

    parts = []
    if numbered:
        parts.append(f"{numbered} numbered item{'s' if numbered != 1 else ''}")
    if checkboxes:
        parts.append(f"{checkboxes} checkbox{'es' if checkboxes != 1 else ''}")
    if headings:
        parts.append(f"{headings} heading{'s' if headings != 1 else ''}")
    divisions = ", ".join(parts)

    return (
        f"This description is {len(text)} characters and carries {divisions}. "
        "If any of those parts could be assigned to someone else or finished on "
        "a different day, it is a SUB-TASK, not a paragraph: memory_task_add "
        "with parent_id (or parent_index in a plan). Sub-tasks carry every "
        "property a task does and are hidden from the top-level list, so the "
        "cost of making one is near zero. Ignore this if it really is one "
        "deliverable explained thoroughly."
    )
