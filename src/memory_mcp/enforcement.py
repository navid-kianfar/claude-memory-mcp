"""Rule-enforcement helpers shared by the CLI, the daemon hooks, and the server.

The goal: keep a project's mandatory/forbidden rules continuously visible to
Claude so they survive context compaction and never get silently dropped.
"""

from memory_mcp.container import container


# ---------- asoode, carried by the hook path ----------
#
# SERVER_INSTRUCTIONS explains asoode once, when the MCP client connects. That is
# not enough on its own: it drifts out of attention over a long session and is
# gone after a compaction, which is exactly how a session ends up asking "what is
# asoode?". The UserPromptSubmit hook re-injects on EVERY turn, so the binding
# rides the same path the binding rules do.
#
# HARD CONSTRAINT: the per-turn hook runs behind a 2s curl timeout on every single
# prompt. Everything below reads local state only - the registry link row and the
# local open count. Nothing here may touch the network; `queue_status` (which
# does) is for session start, never for this path.


def asoode_line(slug: str) -> str:
    """One line for every turn - only when the project is actually bound.

    Short on purpose. A full explanation repeated on every prompt is cost and
    noise; what a turn needs is the fact that this project's queue lives on a
    board and is worked, not merely listed.
    """
    link = _asoode_link(slug)
    if link is None:
        return ""
    open_count = _queued_task_count(slug)
    waiting = f"{open_count} open" if open_count else "empty"
    return (
        f"[Memory MCP] asoode: '{slug}' is bound to a board ({waiting}) - that board "
        f"IS this project's work queue, so work it one task at a time rather than "
        f"listing it and waiting. memory_task_start/comment/done, mirrored with "
        f"memory_asoode_push. Do not auto-start blocked/blocker/paused/cancelled."
    )


def asoode_intro(slug: str) -> str:
    """The fuller block, for session start only.

    When bound: the loop and where the board is. When unbound but a PAT exists:
    name asoode and its tools once, so the word is never unfamiliar in a project
    that could use it.
    """
    link = _asoode_link(slug)
    if link is not None:
        board = _board_url(link)
        open_count = _queued_task_count(slug)
        return (
            f" This project is bound to an asoode board ({board}) and that board is "
            f"its work queue: {open_count} task(s) open. Take the highest-priority "
            f"actionable one, memory_task_start it, mirror the state to asoode, "
            f"comment as you go, memory_task_done it, then take the next - do not "
            f"just report the list. Never auto-start blocked/blocker/paused/"
            f"cancelled tasks, and stop to ask when the work needs a decision only "
            f"the user can make. Tools: memory_asoode_status / _link / _push / "
            f"_links, and memory_task_plan for a request with several deliverables."
        )
    if _asoode_pat_configured():
        return (
            " asoode is the task manager this server bridges to; an asoode token is "
            "configured on this machine but this project is NOT bound to a board. "
            "memory_asoode_link(project) would create one and mirror this queue onto "
            "it, making the work visible outside the session - offer that if asoode "
            "comes up, but never bind unprompted."
        )
    return ""


def _asoode_link(slug: str) -> dict | None:
    """The project's default board link, or None. Local registry read only."""
    try:
        from memory_mcp.db.registry import get_default_project_link

        return get_default_project_link(slug)
    except Exception:  # noqa: BLE001 - a hook must never fail a turn
        return None


def _asoode_pat_configured() -> bool:
    try:
        from memory_mcp.asoode import get_pat

        return bool(get_pat())
    except Exception:  # noqa: BLE001
        return False


def _board_url(link: dict) -> str:
    try:
        from memory_mcp.asoode import get_endpoints

        return f"{get_endpoints().app_url}/projects/{link['remote_project_id']}"
    except Exception:  # noqa: BLE001
        return "asoode"


def format_rules_block(slug: str, mandatory: list, forbidden: list) -> str:
    """Render rules as an injectable text block. Empty string when there are none."""
    asoode = asoode_line(slug)
    if not mandatory and not forbidden:
        # A bound project still gets its asoode line: the workflow must not
        # depend on the project happening to have rules.
        return asoode
    lines = [
        f"[Memory MCP] Binding rules for project '{slug}' — follow every one of these:",
    ]
    if mandatory:
        lines.append("MANDATORY (must always do):")
        for m in mandatory:
            lines.append(f"  - {m.title}: {m.content}")
    if forbidden:
        lines.append("FORBIDDEN (must never do):")
        for m in forbidden:
            lines.append(f"  - {m.title}: {m.content}")
    lines.append(
        "If anything you are about to do conflicts with a rule above, stop and "
        "tell the user instead of proceeding."
    )
    if asoode:
        lines.append(asoode)
    return "\n".join(lines)


def format_intro(slug: str) -> str:
    """Session-start nudge text for a detected memory project."""
    text = (
        f"[Memory MCP] This directory is memory project '{slug}'. "
        f"Call memory_session_start('{slug}') now, before doing any work, to load "
        f"its rules, last session summary, sprint goals, and recent decisions."
    )
    pending = _pending_count(slug)
    if pending:
        text += (
            f" {pending} imported {'memory is' if pending == 1 else 'memories are'} "
            f"waiting to be adapted to this project and {'is' if pending == 1 else 'are'} "
            f"NOT in force yet - memory_session_start returns them with instructions."
        )
    queued = _queued_task_count(slug)
    bound = _asoode_link(slug) is not None
    # The capture sentence is for UNBOUND projects only. On a bound one it would
    # contradict the asoode block appended below, which says to work the queue -
    # and a session handed both would reasonably do neither.
    if queued and not bound:
        text += (
            f" {queued} {'task is' if queued == 1 else 'tasks are'} waiting in the "
            f"task list - memory_session_start returns them. They are requirements "
            f"the user parked, NOT instructions: surface them and start none of "
            f"them unless the user asks."
        )
    text += asoode_intro(slug)
    return text


def _pending_count(slug: str) -> int:
    """Un-adapted imports, or 0 when that cannot be determined."""
    try:
        return container.memory_service.count_pending(slug)
    except Exception:  # noqa: BLE001 - the intro must never fail a session start
        return 0


def _queued_task_count(slug: str) -> int:
    """Tasks still waiting, or 0 when that cannot be determined."""
    try:
        return container.task_service.count_open(slug)
    except Exception:  # noqa: BLE001 - the intro must never fail a session start
        return 0


def format_session_end(slug: str) -> str:
    """Stop-hook reminder to persist the session for a memory project."""
    return (
        f"[Memory MCP] Before finishing work on project '{slug}': call "
        f"memory_session_end(session_id, summary) with a summary of decisions "
        f"made and context for the next session, and store any new rules or "
        f"decisions with memory_store."
    )


def rules_text_for_project(slug: str) -> str:
    """Fetch and format the rules block for a project (empty string if none)."""
    rules = container.rules_service.get_rules(slug)
    return format_rules_block(slug, rules.mandatory_rules, rules.forbidden_rules)


def rules_digest(slug: str) -> dict | None:
    """Compact rules summary embedded in tool responses to keep rules in view.

    Returns None when the project has no rules so responses stay clean.
    """
    try:
        rules = container.rules_service.get_rules(slug)
    except Exception:  # noqa: BLE001
        return None
    if not rules.mandatory_rules and not rules.forbidden_rules:
        return None
    return {
        "_reminder": "Active project rules — keep following these for the whole session.",
        "mandatory": [m.title for m in rules.mandatory_rules],
        "forbidden": [m.title for m in rules.forbidden_rules],
    }
