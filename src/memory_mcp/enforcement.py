"""Rule-enforcement helpers shared by the CLI, the daemon hooks, and the server.

The goal: keep a project's mandatory/forbidden rules continuously visible to
Claude so they survive context compaction and never get silently dropped.
"""

import re
import time
from pathlib import Path

from memory_mcp.container import container
from memory_mcp.db.registry import get_setting, set_setting


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
        f"listing it and waiting. memory_task_start, comment as you go, then "
        f"memory_task_done or update(state=paused|blocked) - each stops the clock "
        f"and mirrors itself. Never leave a task clocking. Do not auto-start "
        f"blocked/blocker/paused/cancelled."
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
            f"actionable one, memory_task_start it (that claims it, clocks on and "
            f"moves the card), comment as you go, memory_task_done it - which stops "
            f"the clock - then take the next; do not just report the list. Every "
            f"local change mirrors to the board on its own. Never auto-start "
            f"blocked/blocker/paused/cancelled tasks; stop to ask when the work "
            f"needs a decision only the user can make, and stop the clock when you "
            f"do (memory_task_update state='blocked'). Tools: memory_asoode_status "
            f"/ _link / _push / _links, and memory_task_plan for a request with "
            f"several deliverables."
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


# ---------- the agent team ----------
#
# The MAIN SESSION is the technical lead. Not a pm subagent underneath it.
#
# Chosen 2026-09-04 over "the session dispatches pm, and pm dispatches the rest",
# for three reasons that were measured rather than assumed: subagent output is
# never shown to the user, so every extra layer is a lossy relay; an orchestrating
# pm accumulates every agent's report, which is the context cost its own fan-out
# rule exists to avoid (one planning dispatch alone cost 102k tokens); and a user
# cannot redirect an agent that is already running, only the session.
#
# `agent: pm` in settings.json would do this natively, but it is silently ignored
# by some clients - it did nothing in the desktop app - so the brief rides the
# hook instead, which works everywhere the rules already do.

AGENT_TEAM_DIR = Path.home() / ".claude" / "agents"


#: The lead's own definition. The SESSION is pm, so pm must not appear in the
#: roster of things to dispatch - offering it re-creates the relay layer this
#: design rejected. The file still exists and can be dispatched deliberately for
#: a planning job worth doing in isolated context.
LEAD_AGENT = "pm"


def installed_agents(include_lead: bool = False) -> list[tuple[str, str]]:
    """(name, description) for every installed agent, from its frontmatter.

    Read from disk rather than hardcoded so the brief can never advertise an
    agent that was retired, or miss one that was added. `pm` is excluded unless
    asked for - see LEAD_AGENT.
    """
    if not AGENT_TEAM_DIR.is_dir():
        return []
    found: list[tuple[str, str]] = []
    for path in sorted(AGENT_TEAM_DIR.glob("*.md")):
        if path.name.lower() == "readme.md" or path.stem.startswith("_"):
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not match:
            continue
        desc = ""
        for line in match.group(1).splitlines():
            if line.startswith("description:"):
                desc = line.split(":", 1)[1].strip()
                # A description containing a colon-space has to be quoted in the
                # file to stay valid YAML. The quotes are syntax; injecting them
                # into the roster puts them in front of every session.
                if len(desc) >= 2 and desc[0] == desc[-1] and desc[0] in "\"'":
                    desc = desc[1:-1]
                break
        if path.stem == LEAD_AGENT and not include_lead:
            continue
        found.append((path.stem, desc))
    return found


def agent_team_line() -> str:
    """One line, every turn. Enough to keep delegation in mind, and no more.

    Deliberately NOT the full brief: this is injected on EVERY prompt, and a
    ninety-line orchestration prompt per turn is exactly the token waste the
    team exists to avoid.
    """
    agents = installed_agents()
    if not agents:
        return ""
    names = ", ".join(name for name, _ in agents)
    return (
        f"[Agent team] You are the technical lead. Specialists available: {names}. "
        "Delegate a real specialism or genuinely parallel work; do it yourself when "
        "you are the cheaper path. Never dispatch what two file reads would answer."
    )


def agent_team_intro() -> str:
    """The full orchestration brief, at session start only."""
    agents = installed_agents()
    if not agents:
        return ""
    lines = [
        "",
        "[Agent team] YOU are the technical lead for this session - the `pm` role. "
        "You orchestrate directly; you do not dispatch a pm agent to do it, because "
        "a subagent's output is never shown to the user and cannot be redirected "
        "once running.",
        "",
        "Available specialists:",
    ]
    lines.extend(f"  - {name}: {desc}" for name, desc in agents)
    lines.extend([
        "",
        "HOW TO USE THEM, and the economics that decide it:",
        "  - A dispatch costs ~60k tokens at the floor, measured. Do the work "
        "yourself when you are the cheapest way to do it - a one-file change never "
        "needs an agent.",
        "  - Delegate when the work is a genuine specialism (a migration, a "
        "pixel-accurate screen, a test suite, an infra change), or when two pieces "
        "are genuinely parallel. frontend and backend are worktree-isolated so they "
        "can run at once.",
        "  - NEVER dispatch what you could answer by reading two files.",
        "  - A subagent cannot see this conversation. Give it the goal, the "
        "constraint that shapes it, the files involved, and what done looks like - "
        "an under-specified brief buys a second dispatch.",
        "  - Sequence deliberately: designer before frontend; reviewer after an "
        "implementation, never instead of one.",
        "  - FAN OUT only to keep a large codebase out of your own context: several "
        "agents survey, ONE folds their findings into a digest, you read the digest. "
        "Never fan out work a single agent could do.",
        "  - An agent reporting a cross-boundary risk is reporting it to YOU. Decide "
        "whether the other side changes and brief that agent; never let one agent "
        "reshape another's contract.",
        "  - BEFORE A COMMIT, dispatch `test` to verify the change on the running "
        "instance (daemon, UI, board) and commit only on its green report. The "
        "repo's own suite proves the code; the test agent proves the product.",
        "  - Subagents share this client's MCP connection: tell every agent to pass "
        "the session_id memory_session_start gave IT on memory_task_start, "
        "memory_task_claim_next and memory_session_end, or its session displaces "
        "yours.",
    ])
    return "\n".join(lines)


# ---------- update notice, carried by the hook path ----------
#
# Reads the poller's CACHED answer. It never checks for itself: this runs on
# every prompt, and a network call there would put GitHub on the critical path
# of the user typing.
#
# THE RULE HERE IS "DO NOT NAG". An update notice repeated on every turn for a
# week is worse than no notice - the user stops reading the injected block
# entirely, which costs them the binding rules too. So the full notice appears
# once at session start, and the per-turn line at most once every few hours.

#: How long before a running session is reminded again. A session that started
#: this morning should still learn about an update that landed at lunchtime; it
#: should not hear about it forty times.
NOTIFY_INTERVAL_SECONDS = 6 * 3600.0
NOTIFIED_AT_KEY = "update:last_notified_at"


def _update_status() -> dict | None:
    """The cached result, only when a SUCCESSFUL check found something."""
    try:
        from memory_mcp.services.update_poller import read_status, update_available

        return read_status() if update_available() else None
    except Exception:  # noqa: BLE001 - a notice must never break the hook
        return None


def update_intro() -> str:
    """The full notice, at session start."""
    status = _update_status()
    if not status:
        return ""
    current = status.get("current_version") or "?"
    latest = status.get("latest_version") or "?"
    behind = status.get("commits_behind")
    detail = f" ({behind} commits behind)" if behind else ""
    return (
        f"\n[Memory MCP] An update is available: {current} -> {latest}{detail}. "
        "Approve it in the management UI, or say so here and it will be applied "
        "at the END of a turn - never mid-turn, because installing reloads the "
        "daemon and drops this MCP connection."
    )


def update_line() -> str:
    """One line, and only occasionally. Empty most of the time, by design."""
    status = _update_status()
    if not status:
        return ""
    try:
        last = float(get_setting(NOTIFIED_AT_KEY) or 0)
    except (TypeError, ValueError):
        last = 0.0
    now = time.time()
    if now - last < NOTIFY_INTERVAL_SECONDS:
        return ""
    try:
        set_setting(NOTIFIED_AT_KEY, str(now))
    except Exception:  # noqa: BLE001
        pass
    return (
        f"[Memory MCP] Update available: {status.get('current_version')} -> "
        f"{status.get('latest_version')}. Approve in the UI or ask to apply it."
    )


def format_rules_block(slug: str, mandatory: list, forbidden: list) -> str:
    """Render rules as an injectable text block. Empty string when there are none."""
    asoode = asoode_line(slug)
    if not mandatory and not forbidden:
        # A bound project still gets its asoode line: the workflow must not
        # depend on the project happening to have rules. Same for the team line.
        return "\n".join(
            x for x in (asoode, agent_team_line(), update_line()) if x
        )
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
    team = agent_team_line()
    if team:
        lines.append(team)
    update = update_line()
    if update:
        lines.append(update)
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
    text += agent_team_intro()
    text += update_intro()
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
        f"[Memory MCP] Before finishing work on project '{slug}': finish or "
        f"pause the task you are on (memory_task_done, or memory_task_update "
        f"state='paused'|'blocked') so no clock is left running, then call "
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
