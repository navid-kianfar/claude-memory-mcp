"""The brief handed to the agent for the queued task list.

The whole reason this list exists is that the user wants to record a requirement
WITHOUT interrupting whatever is in flight. That only holds if a queued task is
never read as "do this now". So session start hands over the list together with
this brief: show the user what is waiting, then wait to be asked.

Sibling of `adaptation.py`, and the same shape - a pure function of (project,
items) returning text, so nothing here can reach a database or a service.
"""

_HEADER = (
    "{count} {noun} waiting in the task list for project '{project}'. These are "
    "QUEUED REQUIREMENTS the user parked - they are NOT instructions for this "
    "session and not a plan to execute:"
)

_STEPS = (
    "1. Surface them. Tell the user what is waiting - title, state, priority - "
    "briefly, as a status line rather than a proposal. That is what makes the "
    "list worth adding to: they can drop a requirement in mid-session and see "
    "it again next time without ever interrupting the work in progress.",
    "2. Do NOT start any of them. The user decides what gets picked up and "
    "when; a task appearing here is not permission to begin it. Carry on with "
    "whatever this session is actually for, and wait to be asked.",
    "3. When the user does pick one: memory_task_start(task_id) clocks on and "
    "moves it to in_progress; memory_task_done(task_id) closes it and stops the "
    "clock. memory_task_stop(task_id) only stops the clock - it deliberately "
    "leaves the state alone, so say explicitly whether the work is paused, "
    "blocked, or finished with memory_task_update(task_id, state=...).",
    "4. Record what the work turns up on the task itself: "
    "memory_task_comment(task_id, body, kind='note'|'rule'|'decision'|"
    "'reminder'). Anything that outlives the task - a project rule, a decision "
    "that shapes future work - still belongs in memory_add_rule / memory_store.",
    "5. Work you notice but are not doing now goes in the same list with "
    "memory_task_add(title, source='claude'). Queue it and say you queued it; "
    "do not silently widen the current task to cover it.",
    "6. Several sessions may be running against this project at once, so a task "
    "is picked up by claiming it: memory_task_claim_next(session_id) when you "
    "have FINISHED what you were doing, never mid-task and never just because "
    "the list above is non-empty. Reporting the list is not claiming from it.",
)


def task_brief(project: str, tasks: list) -> str | None:
    """Instructions for the queued tasks, or None when the list is empty."""
    if not tasks:
        return None
    count = len(tasks)
    header = _HEADER.format(
        count=count, noun="task is" if count == 1 else "tasks are", project=project,
    )
    return "\n".join([header, *_STEPS])
