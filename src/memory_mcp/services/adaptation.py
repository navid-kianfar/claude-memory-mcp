"""The brief handed to the agent for adapting imported memories.

An imported rule is written for the project it came from. Enforcing it verbatim
is how an agent ends up following instructions about components, paths and
conventions that do not exist here. So imports arrive `pending`: stored, but out
of the rule block, out of search, out of the snapshot - and the next session
start hands the agent this brief instead, to rewrite each one against THIS
codebase before it takes effect.
"""

_HEADER = (
    "{count} imported {noun} waiting to be adapted to project '{project}'. "
    "They are NOT in force and are NOT in the rules above - do this before "
    "acting on any of them:"
)

_STEPS = (
    "1. Read each pending item below. `imported_from` holds the original text "
    "and the project it came from.",
    "2. Rewrite it for THIS project: keep the underlying principle, drop the "
    "other project's specifics - its component and package names, file paths, "
    "repo URLs, service names, ticket ids, stack details that do not apply "
    "here. Check the codebase for the local equivalent rather than guessing.",
    "3. If a rule cannot be translated without knowing something only the user "
    "can answer (which component library this project actually uses, whether a "
    "convention should apply at all), ASK THE USER in chat and wait. Do not "
    "invent an answer.",
    "4. Call memory_adapt_pending(memory_id, title, content) with the rewritten "
    "text. That clears the pending flag: the rule joins this session's rules "
    "from then on, and syncs to the project's .claude-memory/ snapshot.",
    "5. If a rule simply does not belong in this project, call "
    "memory_discard_pending(memory_id, reason) instead.",
)


def adaptation_brief(project: str, pending: list) -> str | None:
    """Instructions for the pending imports, or None when there are none."""
    if not pending:
        return None
    count = len(pending)
    header = _HEADER.format(
        count=count, noun="item is" if count == 1 else "items are", project=project,
    )
    return "\n".join([header, *_STEPS])
