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
    "3. When the user does pick one: memory_task_start(task_id) clocks on, "
    "claims it for this session and moves it to in_progress. When you stop "
    "working on it, say how: memory_task_done(task_id) finishes it and stops "
    "the clock; memory_task_update(task_id, state='paused'|'blocked') stops the "
    "clock and says why; memory_task_stop(task_id) stops the clock and leaves "
    "the state alone. Never end a session with a task still clocking.",
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


# ---------- the bound-project brief ----------
#
# The inverse of the brief above, and deliberately so. The capture contract - "a
# queued task is never an instruction" - exists to protect a session in flight
# from being derailed by the list. Binding a project to an asoode board is the
# user saying the opposite: that board IS the work queue, so a session that finds
# work on it should get on with it rather than reporting and waiting.
#
# Which brief a session gets is therefore decided by one fact: whether the project
# has a project_links row. Nothing has to be re-told per project.

_BOUND_HEADER = (
    "Project '{project}' is bound to an asoode board ({board}). {count} open "
    "{noun} waiting. THIS BOARD IS THE WORK QUEUE - work it, one task at a time, "
    "rather than reporting it and waiting to be asked:"
)

_BOUND_STEPS = (
    "1. Take the highest-priority actionable task (state todo or in_progress). "
    "Say which one you are starting before you start it, in one line, so the "
    "user can redirect you - but do not ask permission to begin.",
    "2. memory_task_start(task_id) claims it, clocks on and moves it to "
    "in_progress - on the board too, in the same call. Every later change "
    "(comments, fields, attachments, time, done) mirrors on its own; there is "
    "nothing extra to call.",
    "3. Do the work. Comment on the task as you go with "
    "memory_task_comment(task_id, body, kind=...): the decision taken and what "
    "it was chosen over, the trap found, what turned out to be wrong. A task "
    "should read back afterwards as a complete account of its own "
    "implementation - that is the point of mirroring it somewhere durable.",
    "4. Anything that outlives the task still goes to memory_add_rule / "
    "memory_store as well. A comment explains one task; a rule shapes every "
    "future one.",
    "5. memory_task_done(task_id) closes it and stops the clock. Then take the "
    "next one. Keep going until the queue is empty or the user redirects you.",
    "6. Do NOT auto-start a task whose state is blocked, blocker, paused or "
    "cancelled - those are waiting on something. And stop to ask when the work "
    "needs a decision only the user can make: an API they own, a product call, "
    "a credential - and when you stop, stop the clock: "
    "memory_task_update(task_id, state='blocked') says why and stops it. Queue "
    "what you cannot finish rather than guessing.",
    "7. Work you notice but are not doing now goes in the same list with "
    "memory_task_add(title, description=..., source='claude'). Give it a "
    "description that states the requirement in full - a bare title loses the "
    "implementation detail the list exists to keep.",
    "8. WHEN THE WORK IS DONE, the final report to the user NAMES THE TASKS. "
    "One line each: title, the state it ended in, and the minutes recorded - "
    "plus the board link once. A task left blocked or paused is named as such "
    "and says what it is waiting on; leaving it out is the failure this step "
    "exists to prevent, because a silent queue is indistinguishable from a "
    "broken one. Report tasks you CREATED but did not start too, so the user "
    "sees what is now queued for later.",
    "9. THIS BRIEF BINDS THE LEAD SESSION - the one talking to the user. If you "
    "are a DISPATCHED SUBAGENT, work only the task you were briefed on: take "
    "another solely through memory_task_claim_next(session_id, role) when you "
    "are idle and your brief allows it, and if you are verifying or reviewing "
    "(test, reviewer) start nothing on the board at all, whatever is queued. "
    "One dispatch must not become a swarm.",
)

_UNREACHABLE = (
    "Project '{project}' is bound to an asoode board, but it could not be "
    "reached ({error}). Work the local task list instead - it is the same queue, "
    "mirrored - and the board will catch up on the next push. Do not treat this "
    "as a reason to stop."
)


def bound_queue_brief(
    project: str, tasks: list, board_url: str, remote_only: list | None = None,
) -> str:
    """Instructions for a project whose task queue is an asoode board."""
    count = len(tasks)
    header = _BOUND_HEADER.format(
        project=project, board=board_url, count=count,
        noun="task is" if count == 1 else "tasks are",
    )
    lines = [header, *_BOUND_STEPS]
    if remote_only:
        lines.append(
            f"8. {len(remote_only)} task(s) are on the board but not in the local "
            "list - added in asoode by the user or a teammate: "
            + "; ".join(remote_only[:5])
            + ". Add them locally with memory_task_add before working them, so "
            "the two lists agree."
        )
    return "\n".join(lines)


def unreachable_brief(project: str, error: str) -> str:
    """What to do when the bound board cannot be read. Never a hard failure."""
    return _UNREACHABLE.format(project=project, error=error)


_UNBOUND_ASK = (
    "\n\nTHIS PROJECT IS NOT BOUND TO AN ASOODE BOARD, and a PAT is configured "
    "on this machine - so the queue can be mirrored where the user can see it "
    "outside this terminal. ASK THEM, in your next reply, before doing the "
    "work: which board should '{project}' use?"
)

_UNBOUND_ASK_TAIL = (
    "\nWhichever they pick, it is stored once and every later session starts "
    "bound and says nothing about this again.\n"
    "DO NOT CHOOSE FOR THEM. Binding writes real cards to a real board, and a "
    "guess puts a private project on a server nobody picked - ask, then call "
    "the tool. If they decline, carry on with the local queue and do not raise "
    "it again this session."
)

_UNBOUND_NO_BOARDS = (
    "\nNo existing boards were readable, so the offer is simply: create one "
    "for this project?\n"
    "  create -> memory_asoode_link(project='{project}')"
)


def unbound_hint(project: str, boards: list | None = None) -> str:
    """ASK an unbound project's session where to bind, when a PAT exists.

    It used to say only that binding was possible and to "offer that if the
    user asks about asoode" - a note a session skims past, so nothing happened
    and the next session started from exactly the same place. The user asked
    for a question instead: check for the integration at session start, and if
    it is missing, ask where to bind or where to create.

    `boards` is what the credential can already see, so the choice can be made
    BY NAME. Without them the ask is real but unanswerable without a lookup
    first, which is how an offer turns back into a note.

    Never binds anything: a wrong guess writes cards to somebody else's board,
    which is the failure the never-cross-link-projects rule exists to prevent.
    """
    lines = [_UNBOUND_ASK.format(project=project)]
    if boards:
        lines.append("\nBoards this credential can already see:")
        for board in boards[:12]:
            title = board.get("title") or "(untitled)"
            space = board.get("project_title") or ""
            where = f" - in {space}" if space else ""
            lines.append(f"  - {title}{where}   [{board.get('id')}]")
        if len(boards) > 12:
            lines.append(f"  ... and {len(boards) - 12} more")
        lines.append(
            "\n  use one of these -> memory_asoode_attach(project='"
            + project + "', work_package_id='<id from above>')"
            "\n  or a new board   -> memory_asoode_link(project='"
            + project + "')"
        )
    else:
        lines.append(_UNBOUND_NO_BOARDS.format(project=project))
    lines.append(_UNBOUND_ASK_TAIL)
    return "\n".join(lines)
