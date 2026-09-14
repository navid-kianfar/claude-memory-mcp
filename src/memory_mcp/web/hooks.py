"""The Claude Code hook endpoints, and the identity every hook now forwards.

These used to live in `routes.py`. They are here for two reasons. The first is
mechanical: the hook routes and the UI/task routes are edited by different people
for different reasons, and a module boundary is what lets both happen in one
checkout. The second is that the hooks have acquired a shape of their own - a
payload with an identity in it - and `HookIdentity` is that shape, parsed once per
request instead of re-read field by field in five handlers.

WHAT A HOOK MAY DO IN HERE. Every handler fails open and fails silent: a non-project
directory, an unreachable board, a locked registry, an exception anywhere - the
answer is still "carry on", because a gate that stops a person editing a file is a
worse bug than the one it fixes. The only path to a block is the daemon explicitly
answering `allow: false`. Nothing here touches the network.

WHAT IS RECORDED. `cwd`, `session_id`, `transcript_path`, the tool name and the
edited path - never a prompt, never the model's text. The rows land in the
machine-local SQLite registry and travel nowhere. The one exception is
`/api/hook/attachments`: it copies the files the USER attached in the compose box
out of the transcript into the task store, because putting them on a task is the
point - and it binds none of them to a task until the session has asked.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from anyio import to_thread
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from memory_mcp.config import settings
from memory_mcp.container import container
from memory_mcp.db.registry import (
    agent_id_supported, get_setting, note_agent_id, prune_session_ledgers,
    record_dispatch, record_edit, record_subagent_stop, set_setting,
    touch_client_session,
)
from memory_mcp.models import TaskFilter, TaskState
# "Is this folder a worktree?" is the project service's question, not the web
# layer's; both the auto-register hook and claim_folder must answer it identically
# or one of them binds what the other refuses.
from memory_mcp.services.project_service import WORKTREE_MARKER, is_linked_worktree


# ---------- identity ----------


@dataclass(frozen=True)
class HookIdentity:
    """What a hook payload says about who is calling.

    `cwd` is the only field that existed before; the rest is what every script
    now forwards. All of them are strings and all of them may be empty - a hook
    payload is untrusted input from a CLI whose fields vary by version, so
    nothing in here is ever assumed to be present.
    """

    cwd: str = ""
    session_id: str = ""
    agent_id: str = ""
    agent_type: str = ""
    transcript_path: str = ""
    tool: str = ""
    file_path: str = ""

    @property
    def is_subagent(self) -> bool | None:
        """True, False, or None when this build cannot say.

        `agent_id`/`agent_type` are documented hook fields but were not verified
        on the installed CLI (2.1.236), so "absent" does not mean "the lead" - it
        means "unknown", until a payload carrying one has proved the build sends
        them (`note_agent_id` / `agent_id_supported`). A cwd inside a Claude Code
        worktree is a second, independent witness that does not depend on the
        build at all.

        None is a real answer and callers must handle it. Treating it as False
        would silently attribute a subagent's edits to the lead on exactly the
        builds where we cannot tell.
        """
        if self.agent_id:
            return True
        if WORKTREE_MARKER in self.cwd:
            return True
        if agent_id_supported():
            return False
        return None

    @property
    def by_agent(self) -> str | None:
        """The ledger's `by_agent`: the agent type when we know an agent is
        calling, NULL for the lead (and for "unknown", which reads as the lead)."""
        if not self.agent_id:
            return None
        return self.agent_type or "unknown"


def _identity(data, getter) -> HookIdentity:
    def s(key: str) -> str:
        value = getter(data, key)
        return value.strip() if isinstance(value, str) else ""

    identity = HookIdentity(
        cwd=s("cwd"),
        session_id=s("session_id"),
        agent_id=s("agent_id"),
        agent_type=s("agent_type"),
        transcript_path=s("transcript_path"),
        tool=s("tool_name"),
        file_path=s("file_path"),
    )
    # Learn from the payload itself whether this build sends agent_id at all.
    note_agent_id(identity.agent_id)
    return identity


def identity_from_query(request) -> HookIdentity:
    """Parse the identity a GET hook (rules, gate, auto-register) forwarded."""
    return _identity(request.query_params, lambda q, k: q.get(k, ""))


def identity_from_body(body: dict) -> HookIdentity:
    """Parse the identity a POST hook (claim, dispatch) forwarded."""
    return _identity(body or {}, lambda b, k: b.get(k) or "")


def _seen(identity: HookIdentity, slug: str | None = None) -> None:
    """Record that this Claude session exists. Never fatal: the ledger is a
    convenience for the next turn, not a precondition for this one.

    Wrapped here as well as inside the accessor, and deliberately. Bookkeeping
    must not be able to change a decision: in `_hook_gate` an exception escaping
    this would reach the handler's own catch-all and open the gate, turning a
    ledger bug into a way past the one thing this hook exists to enforce.
    """
    if not identity.session_id:
        return
    try:
        touch_client_session(
            identity.session_id,
            slug=slug,
            cwd=identity.cwd or None,
            transcript_path=identity.transcript_path or None,
        )
    except Exception:  # noqa: BLE001 - see above
        pass


# ---------- handlers ----------


def _hook_authorized(request) -> bool:
    """Hook endpoints are raw handlers, not `_api`-wrapped. In server mode they
    must carry a valid bearer token so rules are never served to an
    unauthenticated caller; in local mode always allowed.

    Imported from `routes` lazily and on purpose: `routes` imports this module at
    module level to publish HOOK_ROUTES, so the dependency can only run one way
    at import time.
    """
    if not settings.server_mode:
        return True
    from memory_mcp.web.routes import _request_user_obj

    return _request_user_obj(request) is not None


async def _hook_auto_register(request):
    """Auto-register the working directory as a project (used by the hook).

    When Claude Code starts a session in a git repository that is not yet a
    memory project, register it so it shows up in the UI - even before it has
    any rules. Returns a short note, or empty when nothing was done.
    """
    if not _hook_authorized(request):
        return PlainTextResponse("")
    identity = identity_from_query(request)
    cwd = identity.cwd

    def _resolve() -> str:
        from memory_mcp.context import detect_project_from_cwd
        from memory_mcp.utils.text import slugify

        # SessionStart is the one hook that fires once per session rather than
        # once per turn, so it is where a week-old ledger gets swept.
        try:
            prune_session_ledgers()
        except Exception:  # noqa: BLE001 - housekeeping, never the point of the call
            pass
        if not cwd:
            return ""
        folder = Path(cwd)
        if not folder.is_dir():
            return ""
        slug = detect_project_from_cwd(cwd)
        _seen(identity, slug)
        if slug:
            return ""  # already a registered project
        if is_linked_worktree(folder):
            # A worktree is a second checkout of a project that lives elsewhere.
            # Registering it would create a duplicate project whose path points
            # into .claude/worktrees/ and disappears when the worktree is removed.
            return ""
        if not (folder / ".git").is_dir():
            return ""  # only auto-register actual repositories
        slug = slugify(folder.name)
        if not slug or container.project_repo.get(slug) is not None:
            return ""  # no name, or the slug is already taken by another project
        container.project_service.init_project(slug, folder.name, project_path=cwd)
        return (
            f"[Memory MCP] Registered this folder as project '{slug}' - "
            f"it now appears in the management UI."
        )

    try:
        text = await to_thread.run_sync(_resolve)
    except Exception:  # noqa: BLE001
        text = ""
    return PlainTextResponse(text)


async def _hook_claim(request):
    """Bind a folder to the project its committed snapshot names.

    The SessionStart hook calls this before anything else. Keying on
    manifest.json's project_id means a project that was moved or renamed is
    re-bound to its new location instead of being registered a second time,
    and a teammate's fresh clone adopts the same identity.
    """
    if not _hook_authorized(request):
        return JSONResponse({"slug": None, "action": "unauthorized"})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    identity = identity_from_body(body)

    def _resolve() -> dict:
        cwd = (body.get("cwd") or "").strip()
        if not cwd:
            return {"slug": None, "action": "unclaimed"}
        result = container.project_service.claim_folder(
            cwd,
            project_uid=body.get("project_id"),
            slug_hint=body.get("slug"),
            display_name=body.get("display_name"),
        )
        _seen(identity, result.get("slug"))
        return result

    try:
        result = await to_thread.run_sync(_resolve)
    except Exception as exc:  # noqa: BLE001 - a hook must never see a 500
        result = {"slug": None, "action": "error", "error": str(exc)}
    return JSONResponse(result)


async def _hook_rules(request):
    """Plain-text rules block for Claude Code hooks (cwd -> project -> rules).

    Returns an empty body when the directory is not a memory project, so the
    hook stays silent in unrelated repos.
    """
    if not _hook_authorized(request):
        return PlainTextResponse("")
    identity = identity_from_query(request)
    cwd = identity.cwd
    mode = request.query_params.get("mode", "rules")

    def _resolve() -> str:
        from memory_mcp.context import detect_project_from_cwd
        from memory_mcp.enforcement import (
            format_intro, format_session_end, rules_text_for_project,
        )

        slug = detect_project_from_cwd(cwd)
        _seen(identity, slug)
        if not slug:
            return ""
        # `cwd` makes the team text name this repo's own specialists; the
        # session id lets the per-turn line read this session's dispatch and
        # edit ledgers and escalate by name. Both reads are wrapped inside
        # enforcement, so a broken ledger costs the escalation, never the rules.
        if mode == "intro":
            return format_intro(slug, cwd=cwd or None)
        if mode == "end":
            return format_session_end(slug)
        return rules_text_for_project(
            slug, cwd=cwd or None, session_id=identity.session_id or None,
        )

    try:
        text = await to_thread.run_sync(_resolve)
    except Exception:  # noqa: BLE001
        text = ""
    return PlainTextResponse(text)


async def _hook_gate(request):
    """Should a mutating tool call be allowed to proceed? (PreToolUse hook.)

    Answers `{"allow": bool, "reason": str}`. The hook denies ONLY on an
    explicit `allow: false`; every other outcome - a non-project directory, an
    unbound project, an unreachable daemon, an exception in here - is an allow.

    That asymmetry is the whole design. This gate exists because a rule saying
    "start a task first" was followed about 70% of the time, and text cannot
    require anything. But a gate that blocks a person from editing a file
    because a board is down would be far worse than the problem it fixes, so
    every failure mode opens it.

    Gates on the PROJECT having a task in progress rather than THIS session
    having one: the Claude Code session id this now receives is not the memory
    session id that claims a task, and a task somebody else left in progress
    opening the gate is a much smaller problem than a gate that cannot be
    satisfied.

    It is also the edit ledger. Every Edit/Write already comes through here, so
    recording the path costs no extra round trip and needs no second hook.
    """
    if not _hook_authorized(request):
        return JSONResponse({"allow": True, "reason": "unauthorized - failing open"})
    identity = identity_from_query(request)
    cwd = identity.cwd

    def _decide() -> dict:
        from memory_mcp.context import detect_project_from_cwd
        from memory_mcp.db.registry import get_project_links

        slug = detect_project_from_cwd(cwd)
        _seen(identity, slug)
        if not slug:
            return {"allow": True, "reason": "not a memory project"}
        if identity.file_path and identity.session_id:
            try:
                record_edit(
                    identity.session_id,
                    slug=slug,
                    path=identity.file_path,
                    tool=identity.tool,
                    by_agent=identity.by_agent,
                )
            except Exception:  # noqa: BLE001 - a lost row, never a changed answer
                pass
        # An edit outside the project's own root is not work this board can
        # track, and blocking it blocked every scratchpad write of every agent
        # this gate was installed to help.
        if _outside_project_root(slug, identity.file_path):
            return {"allow": True, "reason": "outside the project root"}
        if not get_project_links(slug):
            return {"allow": True, "reason": "project is not bound to a board"}
        open_tasks = container.task_service.list_tasks(
            slug, TaskFilter(state=TaskState.IN_PROGRESS), limit=1,
        ).tasks
        if open_tasks:
            return {
                "allow": True,
                "reason": f"working: {open_tasks[0].title}",
                "task_id": open_tasks[0].id,
            }
        return {
            "allow": False,
            "slug": slug,
            "reason": (
                f"No task is in progress for '{slug}', and this project's board "
                "is its work queue.\n\n"
                "Put the work on the board BEFORE doing it, so it is tracked and "
                "time is recorded:\n"
                "  - several deliverables -> memory_task_plan(request=..., tasks=[...])\n"
                "  - one deliverable      -> memory_task_add(...) then "
                "memory_task_start(task_id)\n"
                "  - already on the board -> memory_task_start(task_id)\n\n"
                "Then make this edit again. Set MEMORY_MCP_NO_GATE=1 to switch "
                "this off."
            ),
        }

    try:
        answer = await to_thread.run_sync(_decide)
    except Exception as e:  # noqa: BLE001 - a gate that errors must not block work
        answer = {"allow": True, "reason": f"gate error, failing open: {e}"}
    return JSONResponse(answer)


def _outside_project_root(slug: str, file_path: str) -> bool:
    """Is `file_path` outside the project this gate is deciding for?

    Unknowable answers are False - no project_path recorded, no file_path
    forwarded (an older installed hook), an unresolvable path - so the gate keeps
    its existing behaviour rather than silently opening on a path it misread.
    """
    if not file_path:
        return False
    try:
        project = container.project_repo.get(slug)
    except Exception:  # noqa: BLE001
        return False
    if project is None or not project.project_path:
        return False
    try:
        root = Path(project.project_path).resolve()
        target = Path(file_path).resolve()
    except OSError:
        return False
    return root != target and root not in target.parents


async def _hook_dispatch(request):
    """A subagent is about to be dispatched (PreToolUse on the Agent tool).

    Records it, and answers either `{}` or `{"decision": "ask", "reason": ...,
    "context": ...}` - never a refusal. The user chose ASK on 2026-09-13: a generic
    `backend`/`frontend` dispatch where this repo's own specialist applies and
    has not gone first puts a permission prompt in front of the user naming that
    specialist (`enforcement.dispatch_gate` holds the rule). `record-dispatch.sh`
    turns the answer into Claude Code's `permissionDecision: "ask"`.

    The decision is taken BEFORE this dispatch is recorded, so it reads what the
    session had dispatched until now. The dispatch is recorded either way: a
    PreToolUse hook cannot know whether the user then said yes.
    """
    if not _hook_authorized(request):
        return JSONResponse({})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    identity = identity_from_body(body)

    def _stopped() -> dict:
        # SubagentStop: one of this session's agents finished. Counted, never
        # gated - it is what lets the concurrency prompt stop firing.
        if identity.session_id:
            record_subagent_stop(
                identity.session_id,
                agent_id=identity.agent_id or None,
                agent_type=identity.agent_type or None,
            )
        return {}

    def _record() -> dict:
        from memory_mcp.context import detect_project_from_cwd
        from memory_mcp.enforcement import (
            combine_gates, concurrency_gate, dispatch_gate, nested_dispatch_denial,
        )

        # A subagent trying to dispatch is refused before anything is recorded:
        # the agent will not run, so it must not count as dispatched or running.
        denial = nested_dispatch_denial(identity.agent_id)
        if denial:
            return denial

        slug = detect_project_from_cwd(identity.cwd) if identity.cwd else None
        _seen(identity, slug)
        raw_type = body.get("subagent_type")
        subagent_type = (
            raw_type.strip() if isinstance(raw_type, str) else ""
        ) or "general-purpose"
        answers = []
        # Each gate on its own: one that errors lets the dispatch run, and must
        # not take the other gate's answer down with it.
        for gate in (
            lambda: concurrency_gate(identity.session_id or None),
            lambda: dispatch_gate(
                subagent_type,
                cwd=identity.cwd or None,
                slug=slug,
                session_id=identity.session_id or None,
            ),
        ):
            try:
                answers.append(gate())
            except Exception:  # noqa: BLE001
                answers.append({})
        answer = combine_gates(*answers)
        if identity.session_id:
            try:
                record_dispatch(
                    identity.session_id,
                    slug=slug,
                    # An Agent call with no subagent_type is the default agent.
                    # The ledger records what ran, so it records that name too.
                    agent_type=subagent_type,
                    tool_use_id=(body.get("tool_use_id") or "").strip() or None,
                    description=(body.get("description") or "").strip() or None,
                    asked=answer.get("decision") == "ask",
                )
            except Exception:  # noqa: BLE001 - a lost row, never a changed answer
                pass
        return answer

    handler = _stopped if body.get("event") == "SubagentStop" else _record
    try:
        return JSONResponse(await to_thread.run_sync(handler))
    except Exception:  # noqa: BLE001 - never deny a dispatch because of a ledger
        return JSONResponse({})


#: A hook payload is a few hundred bytes. Anything larger is not one.
_HOOK_BODY_LIMIT = 64 * 1024

#: The events the attachments hook scans on. UserPromptSubmit delivers notices
#: (its stdout is context); Stop only scans, because the transcript may lag the
#: prompt at UserPromptSubmit and is complete by the end of the turn.
_ATTACHMENT_EVENTS = frozenset({"UserPromptSubmit", "Stop"})


async def _small_json(request, limit: int = _HOOK_BODY_LIMIT) -> dict:
    """The request body as a JSON object, read no further than `limit` bytes.

    `request.json()` reads whatever is sent; a hook route bound to a local port is
    still an endpoint, and an unbounded read is an allocation the caller controls.
    Anything oversized, unparsable or not an object is `{}`.
    """
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return {}
    try:
        parsed = json.loads(bytes(body) or b"{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _hook_attachments(request):
    """Copy what the user attached in the compose box out of the transcript.

    Body: `{session_id, cwd, transcript_path, event, agent_id?, agent_type?}` from
    `inject-rules.sh` (UserPromptSubmit) and `session-end.sh` (Stop). Answers plain
    text: this session's undelivered attachment notices, one per line, or nothing.

    Silent - an empty body, never a 500 - for a subagent's turn (the user does not
    type to it), a directory that is not a project, an unknown event, and every
    exception. The scan, the confinement of `transcript_path` and the notices live
    in `AttachmentInboxService.handle_hook`; nothing here touches the network, and
    nothing is ever bound to a task without the session asking the user first.
    """
    if not _hook_authorized(request):
        return PlainTextResponse("")
    try:
        body = await _small_json(request)
    except Exception:  # noqa: BLE001
        body = {}
    identity = identity_from_body(body)
    event = body.get("event") if isinstance(body.get("event"), str) else ""

    def _run() -> str:
        from memory_mcp.context import detect_project_from_cwd

        if event not in _ATTACHMENT_EVENTS:
            return ""
        if identity.agent_id or WORKTREE_MARKER in identity.cwd:
            return ""
        if not identity.session_id or not identity.transcript_path or not identity.cwd:
            return ""
        slug = detect_project_from_cwd(identity.cwd)
        if not slug:
            return ""
        return container.attachment_inbox_service.handle_hook(
            slug, identity.session_id, identity.transcript_path,
            deliver=event == "UserPromptSubmit",
        )

    try:
        text = await to_thread.run_sync(_run)
    except Exception:  # noqa: BLE001 - a hook must never see a 500
        text = ""
    return PlainTextResponse(text)


def _hook_update(request):
    """Plain-text answer for the Stop hook: apply, or not.

    Deliberately not JSON - the hook is bash, and `[ "$ANSWER" = "apply" ]` needs
    no parser. Public like the other hook routes.
    """
    from memory_mcp.services import update_poller
    # The flag's home is the updates region of routes.py, which owns approving
    # and cancelling it; read it from there rather than writing the key twice.
    from memory_mcp.web.routes import UPDATE_APPROVED_KEY

    try:
        approved = bool(get_setting(UPDATE_APPROVED_KEY))
        answer = "apply" if (approved and update_poller.update_available()) else "no"
        # The hook cannot infer the repo from its own path - it is installed to
        # ~/.claude-memory-mcp/hooks/. Setup recorded it; hand it over.
        repo = get_setting("install:repo_dir") or ""
    except Exception:  # noqa: BLE001 - the hook must never see a 500
        answer, repo = "no", ""
    return PlainTextResponse(f"{answer} {repo}".strip())


def _hook_update_done(request):
    """The hook says it finished; clear the approval so it does not loop."""
    import contextlib

    from memory_mcp.web.routes import UPDATE_APPROVED_KEY

    with contextlib.suppress(Exception):
        set_setting(UPDATE_APPROVED_KEY, "")
    return PlainTextResponse("ok")


#: Every hook route, in one list `routes.build_routes()` extends. Adding a hook
#: endpoint touches this module only.
HOOK_ROUTES = [
    Route("/api/hook/rules", _hook_rules, methods=["GET"]),
    Route("/api/hook/auto-register", _hook_auto_register, methods=["GET"]),
    Route("/api/hook/gate", _hook_gate, methods=["GET"]),
    Route("/api/hook/claim", _hook_claim, methods=["POST"]),
    Route("/api/hook/dispatch", _hook_dispatch, methods=["POST"]),
    Route("/api/hook/attachments", _hook_attachments, methods=["POST"]),
    Route("/api/hook/update", _hook_update, methods=["GET"]),
    Route("/api/hook/update-done", _hook_update_done, methods=["POST"]),
]
