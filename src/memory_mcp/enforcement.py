"""Rule-enforcement helpers shared by the CLI, the daemon hooks, and the server.

The goal: keep a project's mandatory/forbidden rules continuously visible to
Claude so they survive context compaction and never get silently dropped.
"""

import re
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from memory_mcp.container import container
from memory_mcp.db.registry import (
    dispatches_for, edits_for, get_setting, running_dispatches, set_setting,
)
from memory_mcp.models import TaskFilter, TaskState
from memory_mcp.services import stack_detect
from memory_mcp.services.stack_detect import (
    GENERIC_ROLES, NON_STACK_ROLES, STACK_AGENTS, StackHit, StackProfile,
)


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


# ---------- delegation: the contract, and the turn it is enforced by name ----------
#
# Stated by the user on 2026-09-13: "most of the time the main agent does all the
# things and does not respect the rules we had like having agents for each work"
# and "for a nodejs project a node-agent must be run not a backend agent which is
# a general thing." The text this replaces told the lead to "do it yourself when
# you are the cheaper path" and named the roster alphabetically, and both were
# read as permission: the generic `backend` won over `nodejs` by sorting first.
#
# So the text is now stack-aware (the repo's own specialists are named, per path),
# delegation-first, and it ESCALATES: once the lead has edited enough source files
# on a roled task without dispatching the role that owns it, the per-turn line
# stops advising and names the agent and the task. Every input is a local read -
# the stack profile cache and the two hook ledgers - so the 2 s per-turn budget
# above still holds. Nothing here refuses anything; the dispatch gate asks the
# user (see `dispatch_gate`) and the per-turn line only speaks.

#: Distinct source files the lead may edit this session, on a task whose owner was
#: never dispatched, before the per-turn line names that owner. Three, because a
#: one- or two-file fix is the exception the contract allows the lead to make
#: itself, and a third file is where a fix has become an implementation.
DELEGATION_EDIT_THRESHOLD = 3

#: The per-turn line is asserted `< 400` characters. Built against 390 so a
#: roster or a title one character longer than measured is not a failing test.
_LINE_BUDGET = 390

#: The roles that are not about a stack, in the order the lead meets them.
_ALSO_ROLES = ("designer", "test", "reviewer", "devops", "docs")
_ALSO_LABELS = {"designer": "designer (before UI)", "test": "test (before a commit)"}

#: Paths under these segments, and any `*.md`, are not source: the lead writing
#: its own notes, the memory snapshot or the docs is not implementing a task.
_NOT_SOURCE_SEGMENTS = frozenset({".claude", ".claude-memory", "docs"})

_TITLE_MAX = 60
_PATH_DISPLAY_MAX = 40
#: How many hits a gate reason names before it says "+N more".
_REASON_MAX_HITS = 3

_LEAD_SENTENCE = (
    "[Agent team] You are the technical lead: you plan, brief and integrate; "
    "specialists implement."
)
_NAME_SENTENCE = "Name the agent type in each dispatch description."

_MARKER_LIST = (
    "package.json, pyproject.toml, go.mod, Cargo.toml, *.csproj/*.sln, "
    "build.gradle.kts, pom.xml"
)

_STACK_CONVENTIONS = (
    "  A stack expert carries conventions the generic role does not (uv/FastAPI "
    "for python, NestJS/pnpm for nodejs, Ktor for kotlin, Compose Multiplatform for "
    "app, the shadcn wrapper rule for react). Reach for `backend` or `frontend` "
    "only for a path no specialist above covers, and say that you are doing so. "
    "`kotlin` is SERVER Kotlin; `app` is Android+iOS - they are not "
    "interchangeable. A Next.js repo is `nodejs` first, with `react` dispatched on "
    "top for screens and components."
)

_DIVISION_OF_WORK = (
    "HOW THE WORK IS DIVIDED - this is the contract, not advice:",
    "  - Implementation is DISPATCHED. A task on the board with a role is "
    "implemented by an agent of that role: you brief it, you integrate its report, "
    "you do not write it yourself. The exception is genuinely small - a single-file "
    "fix, a config value, a typo. Answering questions, reading code and git "
    "operations are always yours.",
    "  - SPEND AS IF IT IS MEASURED, BECAUSE IT IS: a real dispatch costs 115k-380k "
    "tokens (measured 2026-09-13), not a rounding error. Dispatch a genuine "
    "specialism or genuinely parallel implementation. Keep integration work whose "
    "context you already hold - an agent pays to rediscover it. Never dispatch what "
    "two file reads would answer.",
    "  - ONLY YOU DISPATCH, AND AT MOST TWO AGENTS OF A KIND AT ONCE. No installed "
    "agent has the Agent tool, so none can fan out - a reviewer splitting itself "
    "into 'angles' is how one review became eleven agents. A third agent of a kind "
    "(a third `python`) while two are running puts a prompt in front of the user; "
    "different kinds may run side by side.",
    "  - Sequence: a stack expert before `backend` when the structure is undecided; "
    "`designer` before `frontend`/`react`/`app`; `reviewer` after an "
    "implementation, never instead of one; `test` before every commit, against the "
    "RUNNING instance (daemon, UI, board) - the repo's suite proves the code, the "
    "test agent proves the product.",
    "  - Agents run at once only when their file sets are disjoint: name each "
    "agent's files in its brief. They share this one checkout. NEVER pass "
    "`isolation` to the Agent tool and never ask for a worktree: whether a dispatch "
    "runs isolated is the user's choice in the Claude interface, and a worktree "
    "sits at the last commit, so an agent sent to verify uncommitted work would see "
    "a tree without it.",
    "  - A subagent cannot see this conversation. Give it the goal, the constraint "
    "that shapes it, the files involved, and what done looks like - an "
    "under-specified brief buys a second dispatch.",
    "  - NO SURVEY FAN-OUTS by default: read the code yourself. A survey dispatch is "
    "for an area genuinely too large to read, one per concern, never a batch. ONE "
    "`reviewer` and ONE `test` per release, scoped to the riskiest surfaces - never "
    "one per task, never one per angle.",
    "  - An agent reporting a cross-boundary risk is reporting it to YOU. Decide "
    "whether the other side changes and brief that agent; never let one agent "
    "reshape another's contract.",
    "  - NAME THE AGENT IN THE DESCRIPTION: end every dispatch description with the "
    "agent type in parentheses - `Verify the mirror (test)`, `Build the task dialog "
    "(react)`, `Review the depth guard (reviewer)`. It is the only thing the user "
    "sees while an agent runs.",
    "  - Subagents share this client's MCP connection: tell every agent to pass the "
    "session_id memory_session_start gave IT on memory_task_start, "
    "memory_task_claim_next and memory_session_end, and to pass agent=<its type> to "
    "memory_session_start, or its session displaces yours.",
)


@dataclass(frozen=True)
class Escalation:
    """The lead is implementing work that belongs to an agent it never dispatched."""

    role: str
    task_id: str
    task_title: str
    #: Distinct source files the lead edited this session.
    edited: int
    #: None when the task carries its role; otherwise the display of the path
    #: prefix the role was inferred from.
    detected_prefix: str | None = None


def _stack_profile(cwd: str | None, slug: str | None) -> StackProfile | None:
    """The repo's stack profile, or None. Never raises: the intro and the rules
    block must not fail a session because a directory could not be read."""
    if not cwd and not slug:
        return None
    try:
        return stack_detect.profile_for(cwd, slug)
    except Exception:  # noqa: BLE001
        return None


def _stack_hits(profile: StackProfile | None) -> tuple[StackHit, ...]:
    """The hits the lead is told about: the strong ones when there are any.

    A tooling-only root `package.json` in a monorepo is a weak `nodejs` hit (the
    detector keeps it so `hit_for_path` can still answer for unclaimed paths), and
    naming it beside the real modules would tell the lead to dispatch `nodejs` for
    a repo root that holds prettier. When nothing strong was found, the weak hits
    are the best answer there is.
    """
    if profile is None:
        return ()
    strong = tuple(h for h in profile.hits if h.strength == "strong")
    return strong or profile.hits


def _path_display(path: str) -> str:
    shown = "." if path in ("", ".") else f"{path.strip('/')}/"
    if len(shown) > _PATH_DISPLAY_MAX:
        shown = "…/" + shown[-_PATH_DISPLAY_MAX:]
    return shown


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _installed_names(agents: list[tuple[str, str]]) -> set[str]:
    return {name for name, _ in agents}


def agent_team_line(
    *, cwd: str | None = None, slug: str | None = None, session_id: str | None = None,
) -> str:
    """One line, every turn. Enough to keep delegation in mind, and no more.

    Deliberately NOT the full brief: this is injected on EVERY prompt, and a
    ninety-line orchestration prompt per turn is exactly the token waste the
    team exists to avoid. Four states, one line each, every one under
    `_LINE_BUDGET`: escalated (the lead is implementing a task whose owner it
    never dispatched), the repo's own specialists by path, or - with no stack
    detected - the roster.
    """
    agents = installed_agents()
    if not agents:
        return ""
    installed = _installed_names(agents)
    profile = _stack_profile(cwd, slug)

    if session_id and slug:
        try:
            escalation = delegation_escalation(
                slug, session_id, profile=profile, installed=installed,
            )
        except Exception:  # noqa: BLE001 - a ledger read must never cost the turn
            escalation = None
        if escalation is not None:
            return _escalated_line(escalation)

    specialists = [h for h in _stack_hits(profile) if h.agent not in GENERIC_ROLES]
    if specialists:
        line = _stack_line(specialists, installed)
        if line:
            return line
    return _roster_line([name for name, _ in agents])


def _roster_line(names: list[str]) -> str:
    """State 1: nothing detected. Names drop off the end, never the rule."""
    shown = list(names)
    while True:
        dropped = len(names) - len(shown)
        more = f" (+{dropped} more)" if dropped else ""
        line = (
            f"{_LEAD_SENTENCE} Available: {', '.join(shown)}{more}. Dispatch the "
            "stack's own expert over generic backend/frontend; designer before UI; "
            f"test before a commit. {_NAME_SENTENCE}"
        )
        if len(line) <= _LINE_BUDGET or len(shown) <= 1:
            return line
        shown.pop()


def _line_hit(hit: StackHit, installed: set[str]) -> str:
    where = _path_display(hit.path)
    if hit.agent not in installed:
        return f"{where}={hit.agent} (not installed; use {hit.fallback})"
    co = f"+{hit.co_agent}" if hit.co_agent and hit.co_agent in installed else ""
    return f"{where}={hit.agent}{co}"


def _stack_line(hits: list[StackHit], installed: set[str]) -> str:
    """States 2-3. Deterministic shrink: three hits, then two, then one, the rest
    folded into "+N more"; empty when even one hit does not fit."""
    rendered = [_line_hit(h, installed) for h in hits]
    also = [_ALSO_LABELS.get(n, n) for n in _ALSO_ROLES if n in installed]
    also_sentence = f" Also: {', '.join(also)}." if also else ""
    for keep in (3, 2, 1):
        shown = rendered[:keep]
        dropped = len(rendered) - len(shown)
        more = f" (+{dropped} more, see session intro)" if dropped else ""
        line = (
            f"{_LEAD_SENTENCE} This repo: {'; '.join(shown)}{more} - dispatch "
            f"those, never generic backend/frontend.{also_sentence} {_NAME_SENTENCE}"
        )
        if len(line) <= _LINE_BUDGET:
            return line
    return ""


def _escalated_line(escalation: Escalation) -> str:
    """State 4. It REPLACES the stack line while the condition holds."""
    role = escalation.role

    def build(title: str, note: str) -> str:
        return (
            f"[Agent team] STOP: you have edited {escalation.edited} source files "
            f"this session on task '{title}' ({note}) without dispatching `{role}`, "
            f"who owns that work. Hand it over now: brief `{role}` with the files and "
            "what done looks like, then integrate its report. Name the agent type in "
            "the dispatch description."
        )

    title = _one_line(escalation.task_title)
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 3] + "..."
    if escalation.detected_prefix is None:
        notes = [f"role {role}"]
    else:
        # The prefix is the first thing to give up: the role is what matters.
        notes = [
            f"role unset; detected {role} for {escalation.detected_prefix}",
            f"role unset; detected {role}",
        ]
    for note in notes:
        line = build(title, note)
        if len(line) <= _LINE_BUDGET:
            return line
    over = len(line) - _LINE_BUDGET
    shorter = title.removesuffix("...")
    shorter = shorter[: max(len(shorter) - over - 3, 8)] + "..."
    return build(shorter, notes[-1])


# ---------- the escalation condition ----------


def delegation_escalation(
    slug: str,
    session_id: str,
    *,
    profile: StackProfile | None = None,
    installed: set[str] | None = None,
) -> Escalation | None:
    """Is the lead implementing a task whose owner it never dispatched?

    Every clause must hold, and every one is a local read:

    1. agents are installed and `session_id` is known - the CLI path has none and
       never escalates;
    2. the LEAD's edits this session (`by_agent IS NULL`), de-duplicated by
       resolved path, inside the project root, not under `.claude`,
       `.claude-memory` or `docs` and not `*.md`, number at least
       DELEGATION_EDIT_THRESHOLD;
    3. an in-progress, unarchived task exists - sub-tasks included - taken
       running-clock first, then most recently updated;
    4. its owner R is `task.role`, or when unset the agent detected for the
       stack hit covering most of the edited files; R is installed, is not `pm`,
       and was never dispatched this session.

    No throttle: dispatching R clears it on the next turn, which is the point.

    On a CLI build that sends no `agent_id`, a subagent's edits read as the
    lead's. A dispatched role is excluded by clause 4, so the only false positive
    is naming a *different* undispatched owner while some other agent edits -
    which is the right message anyway.
    """
    if not slug or not session_id:
        return None
    if installed is None:
        installed = _installed_names(installed_agents())
    if not installed:
        return None
    if profile is None:
        # Called without one (anything but the per-turn line): the project's own
        # bound folder is where its stack is read from.
        profile = _stack_profile(None, slug)
    root = _project_root(slug, profile)
    if root is None:
        return None
    edited = _lead_source_edits(session_id, root)
    if len(edited) < DELEGATION_EDIT_THRESHOLD:
        return None
    candidates = _in_progress_tasks(slug)
    if not candidates:
        return None
    dispatched = {row.get("agent_type") for row in dispatches_for(session_id)}

    inferred: tuple[str, str] | None = None
    inferred_done = False
    for task in candidates:
        role = (task.role or "").strip()
        prefix: str | None = None
        if not role:
            if not inferred_done:
                inferred = _inferred_owner(profile, edited)
                inferred_done = True
            if inferred is None:
                continue
            role, prefix = inferred
        if role == LEAD_AGENT or role not in installed or role in dispatched:
            continue
        return Escalation(
            role=role,
            task_id=task.id,
            task_title=task.title,
            edited=len(edited),
            detected_prefix=prefix,
        )
    return None


def _project_root(slug: str, profile: StackProfile | None) -> Path | None:
    """The project's bound folder, else the repo root the stack was read from."""
    try:
        project = container.project_repo.get(slug)
    except Exception:  # noqa: BLE001
        project = None
    candidate = project.project_path if project is not None else None
    if not candidate and profile is not None:
        candidate = profile.root
    if not candidate:
        return None
    try:
        return Path(candidate).resolve()
    except OSError:
        return None


def _lead_source_edits(session_id: str, root: Path) -> list[Path]:
    """Distinct source files the lead edited this session, inside `root`.

    Runs every turn over a ledger that gains a row per Edit, so each distinct
    recorded string is resolved once, not once per edit of the same file.
    """
    raw_paths = dict.fromkeys(
        row.get("path") or "" for row in edits_for(session_id) if not row.get("by_agent")
    )
    seen: dict[str, Path] = {}
    for raw in raw_paths:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        try:
            path = path.resolve()
            rel = path.relative_to(root)
        except (OSError, ValueError):
            continue
        if not rel.parts:
            continue
        if path.suffix.lower() == ".md":
            continue
        if any(part in _NOT_SOURCE_SEGMENTS for part in rel.parts):
            continue
        seen.setdefault(str(path), path)
    return list(seen.values())


def _in_progress_tasks(slug: str) -> list:
    """In-progress, unarchived tasks - sub-tasks too - running clocks first.

    The repository, not `TaskService.list_tasks`: the service also computes
    per-task meta for every row, which this path has no use for.
    """
    tasks, _total, _open = container.task_repo.list_tasks(
        slug,
        TaskFilter(state=TaskState.IN_PROGRESS, include_subtasks=True),
        50,
        0,
    )
    if not tasks:
        return []
    running = set(container.task_repo.running_task_ids(slug))
    ordered = sorted(
        tasks,
        key=lambda t: t.updated_at.isoformat() if t.updated_at else "",
        reverse=True,
    )
    # Stable: the clock-running tasks move to the front, recency kept within.
    ordered.sort(key=lambda t: t.id not in running)
    return ordered


def _inferred_owner(
    profile: StackProfile | None, edited: list[Path],
) -> tuple[str, str] | None:
    """(agent, prefix display) for the stack hit covering most edited files.

    Ties go to the deeper hit. The prefix shown is the deepest directory every
    one of that hit's edited files shares - it says where the work is, which the
    hit's own path (often `.`) does not.
    """
    if profile is None:
        return None
    root = Path(profile.root)
    groups: dict[str, tuple[StackHit, list[PurePosixPath]]] = {}
    for path in edited:
        try:
            rel = PurePosixPath(path.relative_to(root).as_posix())
        except ValueError:
            continue
        hit = stack_detect.hit_for_path(profile, rel.as_posix())
        if hit is None:
            continue
        groups.setdefault(hit.path, (hit, []))[1].append(rel)
    if not groups:
        return None

    def weight(group: tuple[StackHit, list[PurePosixPath]]) -> tuple[int, int]:
        hit_path = group[0].path
        depth = 0 if hit_path == "." else hit_path.count("/") + 1
        return len(group[1]), depth

    hit, rels = max(groups.values(), key=weight)
    common: list[str] = []
    for segments in zip(*(r.parent.parts for r in rels)):
        if any(s != segments[0] for s in segments):
            break
        common.append(segments[0])
    return hit.agent, _path_display("/".join(common))


# ---------- the session-start brief ----------


def agent_team_intro(*, cwd: str | None = None, slug: str | None = None) -> str:
    """The full orchestration brief, at session start only."""
    agents = installed_agents()
    if not agents:
        return ""
    installed = _installed_names(agents)
    profile = _stack_profile(cwd, slug)
    hits = _stack_hits(profile)

    lines = [
        "",
        "[Agent team] YOU are the technical lead for this session - the `pm` role. "
        "You orchestrate directly; you do not dispatch a pm agent to do it, because "
        "a subagent's output is never shown to the user and cannot be redirected "
        "once running.",
        "",
    ]
    if hits:
        lines.extend(_stack_block(hits, installed))
        lines.extend(["", "Available specialists:"])
        lines.extend(_roster_rows(agents, profile, hits, installed))
    else:
        where = profile.root if profile is not None else "this directory"
        lines.append(
            f"THIS REPO'S STACK could not be detected: nothing at depth 2 under "
            f"{where} matched a known marker ({_MARKER_LIST}). Read the manifest "
            "yourself, or ask, before dispatching backend or frontend - the generic "
            "roles do not carry a stack's conventions."
        )
        lines.extend(["", "Available specialists:"])
        lines.extend(f"  - {name}: {desc}" for name, desc in agents)
    lines.append("")
    lines.extend(_DIVISION_OF_WORK)
    return "\n".join(lines)


def _markers_for(hit: StackHit) -> str:
    prefix = "" if hit.path == "." else f"{hit.path}/"
    return ", ".join(m.removeprefix(prefix) for m in hit.markers)


def _stack_action(hit: StackHit, installed: set[str]) -> str:
    agent = hit.agent
    if agent not in installed:
        return (
            f"{agent} is NOT installed on this machine; use `{hit.fallback}` and "
            f"tell the user `{agent}` is missing."
        )
    action = f"dispatch `{agent}`"
    if hit.co_agent:
        if hit.co_agent in installed:
            action += f", with `{hit.co_agent}` on top for screens and components"
        else:
            action += (
                f"; `{hit.co_agent}` is NOT installed, so screens go to `frontend`"
            )
    if hit.warning:
        action += f" - {hit.warning}"
    return action


def _stack_block(hits: tuple[StackHit, ...], installed: set[str]) -> list[str]:
    rows = [
        (f"`{_path_display(h.path)}`", h.stack, f"({_markers_for(h)})",
         _stack_action(h, installed))
        for h in hits
    ]
    path_w = max(len(r[0]) for r in rows)
    stack_w = max(len(r[1]) for r in rows)
    markers_w = max(len(r[2]) for r in rows)
    lines = ["THIS REPO'S STACK, detected from its own markers:"]
    lines.extend(
        f"  - {p.ljust(path_w)}  {s.ljust(stack_w)}  {m.ljust(markers_w)}  -> {a}"
        for p, s, m, a in rows
    )
    lines.append(_STACK_CONVENTIONS)
    return lines


def _roster_rows(
    agents: list[tuple[str, str]],
    profile: StackProfile | None,
    hits: tuple[StackHit, ...],
    installed: set[str],
) -> list[str]:
    """The roster in the order this repo needs it, annotations outside the
    description: this repo's specialists, the non-stack roles, the generic
    fallbacks, then one line for the stacks this repo does not have."""
    # order_roster keeps input order inside a bucket, so the non-stack roles are
    # fed in the order the lead meets them rather than alphabetically.
    fed = sorted(
        agents,
        key=lambda a: (0, _ALSO_ROLES.index(a[0])) if a[0] in _ALSO_ROLES else (1, a[0]),
    )
    shown_profile = replace(profile, hits=hits) if profile is not None else None
    ordered = stack_detect.order_roster(fed, shown_profile, installed)

    paths: dict[str, list[str]] = {}
    for hit in hits:
        owner = hit.agent if hit.agent in installed else hit.fallback
        paths.setdefault(owner, []).append(hit.path)
        if hit.co_agent and hit.co_agent in installed:
            paths.setdefault(hit.co_agent, []).append(hit.path)

    def where(hit_paths: list[str]) -> str:
        cap = stack_detect.ANNOTATION_MAX_PATHS
        shown = ", ".join(f"`{_path_display(p)}`" for p in hit_paths[:cap])
        extra = len(hit_paths) - cap
        return f"{shown} (+{extra} more)" if extra > 0 else shown

    rows: list[str] = []
    other_stacks: list[str] = []
    for name, desc, _annotation in ordered:
        hit_paths = paths.get(name)
        if name in NON_STACK_ROLES:
            rows.append(f"  - {name}: {desc}")
        elif name in GENERIC_ROLES:
            if hit_paths:
                rows.append(
                    f"  - {name}: {desc} [THIS REPO - {where(hit_paths)}, where no "
                    "installed specialist applies]"
                )
            else:
                rows.append(
                    f"  - {name}: {desc} [generic fallback - only where no "
                    "specialist above applies]"
                )
        elif hit_paths:
            rows.append(f"  - {name}: {desc} [THIS REPO - {where(hit_paths)}]")
        elif name in STACK_AGENTS:
            other_stacks.append(name)
        else:
            rows.append(f"  - {name}: {desc}")
    if other_stacks:
        rows.append(
            f"  - {', '.join(other_stacks)}: other stacks' experts, not this repo's"
        )
    return rows


# ---------- the dispatch gate ----------
#
# The user's decision (2026-09-13), offered deny / ask / text-only: ASK. A generic
# `backend`/`frontend` dispatch where the repo's own specialist applies, is
# installed, and has not gone first produces a permission prompt naming that
# specialist - never a refusal. The route answers the shape below and
# `record-dispatch.sh` turns it into Claude Code's PreToolUse
# `permissionDecision: "ask"`: `reason` is what the USER reads in the prompt,
# `context` is what the MODEL reads beside the tool result.


def _owner_for(hit: StackHit, role: str) -> str | None:
    """Who owns this hit's work of the kind `role` names, or None if the hit is
    not that kind of work. A Next.js hit is `nodejs` for server work and `react`
    for screens (decision d8b8baa3)."""
    if role == "frontend":
        if hit.co_agent:
            return hit.co_agent
        return hit.agent if hit.fallback == "frontend" else None
    return hit.agent if hit.fallback == "backend" else None


def dispatch_gate(
    subagent_type: str,
    *,
    cwd: str | None,
    slug: str | None,
    session_id: str | None,
) -> dict:
    """`{}` to let a dispatch through, or `{"decision": "ask", ...}`.

    Asks ONLY when every one of these holds:
    - the dispatch is a generic `backend` or `frontend`;
    - it is in a memory project, with a Claude session id (without one the
      "dispatch the specialist first" remedy could never be satisfied);
    - the stack profile has at least one hit whose work is that role's kind;
    - EVERY such hit's owner is a specialist that is installed and carries no
      warning - a hit the generic role legitimately owns, anywhere, lets the
      dispatch through, because it may be for that path;
    - none of those hits' agents (or co-agents) was dispatched this session:
      `backend` after `python` has planned is the sequence, not a mistake.

    Never asks about `general-purpose`, `Explore`, `Plan`, `test`, `reviewer`,
    `designer`, `devops`, `docs`, `pm` or any specialist.
    """
    role = (subagent_type or "").strip()
    if role not in GENERIC_ROLES or not slug or not session_id:
        return {}
    installed = _installed_names(installed_agents())
    if not installed:
        return {}
    profile = _stack_profile(cwd, slug)
    owned: list[tuple[StackHit, str]] = []
    for hit in _stack_hits(profile):
        owner = _owner_for(hit, role)
        if owner is None:
            continue
        if owner in GENERIC_ROLES or owner not in installed or hit.warning:
            return {}
        owned.append((hit, owner))
    if not owned:
        return {}
    dispatched = {row.get("agent_type") for row in dispatches_for(session_id)}
    for hit, owner in owned:
        if owner in dispatched or hit.agent in dispatched or (
            hit.co_agent and hit.co_agent in dispatched
        ):
            return {}

    owners: list[str] = []
    for _hit, owner in owned:
        if owner not in owners:
            owners.append(owner)
    named = " or ".join(f"`{o}`" for o in owners)
    carriers = " and ".join(f"`{o}`" for o in owners)
    verb = "carries" if len(owners) == 1 else "carry"
    where = "; ".join(
        f"{owner} at `{_path_display(hit.path)}` ({', '.join(hit.markers)})"
        for hit, owner in owned[:_REASON_MAX_HITS]
    )
    if len(owned) > _REASON_MAX_HITS:
        where += f" (+{len(owned) - _REASON_MAX_HITS} more)"
    why = (
        f"this repo is {where} and {carriers} {verb} the stack's conventions "
        f"`{role}` does not."
    )
    return {
        "decision": "ask",
        "specialists": owners,
        "reason": (
            f"[Memory MCP] Claude is dispatching the generic `{role}` agent, but "
            f"{why} Yes runs `{role}` anyway; No stops it, and Claude should "
            f"dispatch {named} instead. MEMORY_MCP_NO_GATE=1 turns this prompt off."
        ),
        "context": (
            f"[Memory MCP] Dispatch {named}, not `{role}`: {why} `{role}` is "
            f"allowed without a prompt after {named} has planned (dispatch it "
            "first), or with MEMORY_MCP_NO_GATE=1."
        ),
    }


#: How many agents OF ONE KIND may run at once before the next dispatch of that
#: kind asks the user. The user's limit, set on 2026-09-14 after one session ran
#: 15+ agents at once and corrected the same day from "two in total": "at most
#: two agents OF A KIND at once". A `python`, a `react` and a `test` agent may
#: run side by side; a third `python` asks.
MAX_RUNNING_PER_KIND = 2


def nested_dispatch_denial(agent_id: str | None) -> dict:
    """`{"decision": "deny", ...}` when a SUBAGENT tries to dispatch, else `{}`.

    Keyed on `agent_id` alone - the field a hook payload carries only inside a
    subagent. Never on the cwd: a lead session the user started in a worktree
    runs under `.claude/worktrees/` too, and must keep dispatching.

    The primary guard is `disallowedTools: Agent` in every installed definition;
    this is the second one, for a client that does not honour it. It DENIES
    where the other gates ask, because there is no one to ask: the prompt would
    reach the user about an agent they never saw dispatch anything.
    """
    if not (agent_id or "").strip():
        return {}
    return {
        "decision": "deny",
        "reason": (
            "[Memory MCP] Only the lead session dispatches agents. A subagent "
            "starting another is how one review became eleven agents; do the work "
            "yourself, in sequence, or report how it should be split."
        ),
    }


def concurrency_gate(session_id: str | None, agent_type: str | None) -> dict:
    """`{"decision": "ask", ...}` when MAX_RUNNING_PER_KIND agents of this
    dispatch's kind are already running.

    Kinds are counted apart: two `python` agents running do not stop a `react`
    or a `test` dispatch. Every kind counts, reviewers and test agents included -
    three reviewers cost as much as three implementers. Without a session id or
    a type there is nothing to count, so no prompt.
    """
    kind = (agent_type or "").strip()
    if not session_id or not kind:
        return {}
    running = running_dispatches(session_id, kind)
    if running < MAX_RUNNING_PER_KIND:
        return {}
    return {
        "decision": "ask",
        "running": running,
        "agent_type": kind,
        "reason": (
            f"[Memory MCP] {running} `{kind}` agents are already running in this "
            f"session and this would start another `{kind}`. Each real dispatch "
            "costs 115k-380k tokens. Yes starts it anyway; No makes Claude wait for "
            "one to finish. MEMORY_MCP_NO_GATE=1 turns this prompt off."
        ),
        "context": (
            f"[Memory MCP] {running} `{kind}` agents are still running. Wait for "
            f"one to report before dispatching another `{kind}`: at most "
            f"{MAX_RUNNING_PER_KIND} agents of a kind run at once. Agents of other "
            "kinds may still be dispatched, with files disjoint from the running ones."
        ),
    }


def combine_gates(*answers: dict) -> dict:
    """One answer from several: a deny wins outright; asks merge their text."""
    for answer in answers:
        if answer.get("decision") == "deny":
            return answer
    asks = [a for a in answers if a.get("decision") == "ask"]
    if not asks:
        return {}
    if len(asks) == 1:
        return asks[0]
    merged = dict(asks[0])
    merged["reason"] = " ".join(a["reason"] for a in asks if a.get("reason"))
    merged["context"] = " ".join(a["context"] for a in asks if a.get("context"))
    return merged


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


def format_rules_block(
    slug: str,
    mandatory: list,
    forbidden: list,
    *,
    cwd: str | None = None,
    session_id: str | None = None,
) -> str:
    """Render rules as an injectable text block. Empty string when there are none.

    `cwd` and `session_id` make the team line stack-aware and let it escalate;
    without them it is the roster line, which is what the CLI path gets.
    """
    asoode = asoode_line(slug)
    team = agent_team_line(cwd=cwd, slug=slug, session_id=session_id)
    if not mandatory and not forbidden:
        # A bound project still gets its asoode line: the workflow must not
        # depend on the project happening to have rules. Same for the team line.
        return "\n".join(
            x for x in (asoode, team, update_line()) if x
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
    if team:
        lines.append(team)
    update = update_line()
    if update:
        lines.append(update)
    return "\n".join(lines)


def format_intro(slug: str, *, cwd: str | None = None) -> str:
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
    text += agent_team_intro(cwd=cwd, slug=slug)
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


def rules_text_for_project(
    slug: str, *, cwd: str | None = None, session_id: str | None = None,
) -> str:
    """Fetch and format the rules block for a project (empty string if none)."""
    rules = container.rules_service.get_rules(slug)
    return format_rules_block(
        slug, rules.mandatory_rules, rules.forbidden_rules,
        cwd=cwd, session_id=session_id,
    )


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
