"""Active project context - auto-detect from CWD, persist to the SQLite registry.

Two isolation models live here, selected by settings.mode:

- local (default): per MCP SESSION where the transport gives us one, falling
  back to a process-global (`_active_project`) for callers that have no session -
  the CLI, the hooks, background tasks in the daemon.
- server: a per-request identity (`_request_user`, a contextvar) and a per-user
  active project, so concurrent users never clobber each other. The old global
  is never consulted in server mode.
"""

import contextlib
import contextvars
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from memory_mcp.config import settings
from memory_mcp.constants import MANIFEST_NAME, PORTABLE_DB_NAME, SNAPSHOT_DIRNAME

_active_project: str | None = None
_lock = threading.Lock()

# ---------- per-session active project ----------
#
# One daemon serves EVERY Claude session on this machine, every subagent of every
# session, and the management UI. A single process-global active project means
# session A calling memory_use('x') silently redirects session B's next
# project-less write. That is not hypothetical: on 2026-09-04 it put seven tasks
# in the wrong project and mirrored them to that project's board within seconds,
# and the asoode side has no hard delete, so the cleanup was archiving seven
# cards by hand.
#
# FastMCP's streamable-HTTP transport gives each client its own session id, so
# the fix is to key the active project on it. Callers with no session - the CLI,
# the hook scripts, the daemon's own background tasks - keep using the global
# exactly as before.
#
# Writes go to BOTH: the session's own entry, which is what that session reads,
# and the global, so a caller without a session still sees the last choice made.
_session_projects: "OrderedDict[str, str]" = OrderedDict()
#: Sessions are ephemeral and we are never told when one ends, so the map is
#: bounded rather than left to grow for the daemon's lifetime.
_SESSION_LIMIT = 256


def current_session_id() -> str | None:
    """The calling MCP session, or None when there is no MCP request in flight.

    Never raises: this is consulted on every project resolution, including from
    the CLI and from background tasks where there is no context at all.
    """
    try:
        from fastmcp.server.dependencies import get_context

        return getattr(get_context(), "session_id", None) or None
    except Exception:  # noqa: BLE001 - no context, or a FastMCP that lacks it
        return None


def _remember_session_project(session_id: str, slug: str) -> None:
    with _lock:
        _session_projects[session_id] = slug
        _session_projects.move_to_end(session_id)
        while len(_session_projects) > _SESSION_LIMIT:
            _session_projects.popitem(last=False)


def _session_project(session_id: str) -> str | None:
    with _lock:
        slug = _session_projects.get(session_id)
        if slug is not None:
            _session_projects.move_to_end(session_id)
    return slug


def forget_session_project(session_id: str) -> None:
    """Drop a session's choice. Called when a session ends."""
    with _lock:
        _session_projects.pop(session_id, None)


# ---------- which MEMORY session an MCP session is running ----------
#
# memory_session_start hands back a session id that memory_session_end and
# memory_task_claim_next take explicitly. The clock and the claim need it too -
# a task started by a session must be handed back and stopped by that session's
# end - and asking the agent to thread it through every memory_task_start is
# one more thing to forget. So the MCP session remembers it.
_memory_sessions: "OrderedDict[str, str]" = OrderedDict()


def remember_memory_session(memory_session_id: str) -> None:
    session_id = current_session_id()
    if not session_id or not memory_session_id:
        return
    with _lock:
        _memory_sessions[session_id] = memory_session_id
        _memory_sessions.move_to_end(session_id)
        while len(_memory_sessions) > _SESSION_LIMIT:
            _memory_sessions.popitem(last=False)


def current_memory_session() -> str | None:
    """The memory session this MCP session started, or None."""
    session_id = current_session_id()
    if not session_id:
        return None
    with _lock:
        return _memory_sessions.get(session_id)


def forget_memory_session(memory_session_id: str | None = None) -> None:
    """Drop the mapping when a memory session ends."""
    session_id = current_session_id()
    with _lock:
        if session_id and (
            memory_session_id is None or _memory_sessions.get(session_id) == memory_session_id
        ):
            _memory_sessions.pop(session_id, None)


@dataclass(frozen=True)
class RequestUser:
    """The authenticated caller for the current request (server mode)."""

    id: str
    username: str
    role: str = "member"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# The implicit single user in local mode: full rights, enforces nothing.
LOCAL_USER = RequestUser(id="local", username="local", role="admin")

# Per-request caller. Default None; bound by the MCP tool layer and the JSON API
# wrapper in server mode, and reset when the request finishes. contextvars are
# isolated per async task / per thread, so concurrent requests never collide.
_request_user: contextvars.ContextVar[RequestUser | None] = contextvars.ContextVar(
    "request_user", default=None
)


def set_request_user(user: RequestUser | None):
    """Bind the caller for the current request. Returns a token for reset()."""
    return _request_user.set(user)


def reset_request_user(token) -> None:
    with contextlib.suppress(Exception):
        _request_user.reset(token)


def get_request_user() -> RequestUser | None:
    return _request_user.get()


def _user_from_mcp_token() -> "RequestUser | None":
    """Derive the caller from the current MCP access token, if inside an MCP
    request. Covers tools that don't go through the JSON API's explicit binding
    (and tools that never call _resolve, e.g. memory_use)."""
    try:
        from fastmcp.server.dependencies import get_access_token

        tok = get_access_token()
    except Exception:
        tok = None
    if tok is None:
        return None
    c = getattr(tok, "claims", None) or {}
    uid = c.get("user_id")
    if not uid:
        return None
    return RequestUser(
        id=uid, username=c.get("username", uid), role=c.get("role", "member")
    )


def current_user() -> RequestUser:
    """The effective caller.

    - local mode: always LOCAL_USER.
    - server mode: the request user bound by the JSON API wrapper, else the MCP
      access token's identity, else LOCAL_USER as a last-resort fallback for
      internal paths that run outside any authenticated request.
    """
    if not settings.server_mode:
        return LOCAL_USER
    return _request_user.get() or _user_from_mcp_token() or LOCAL_USER


def set_active_project(slug: str) -> None:
    """Set the active project slug and persist it to the registry.

    Server mode persists per-user so one caller cannot change another's active
    project; local mode uses the single process-global as before.
    """
    if settings.server_mode:
        try:
            from memory_mcp.db.registry import set_user_active_project

            set_user_active_project(current_user().id, slug)
        except Exception:
            pass
        return

    # The session's own choice, so it cannot be moved by another session.
    session_id = current_session_id()
    if session_id:
        _remember_session_project(session_id, slug)

    # And the global, so a caller with no session (CLI, hooks, background work)
    # still sees the most recent choice - unchanged behaviour for them.
    global _active_project
    with _lock:
        _active_project = slug
    try:
        from memory_mcp.db.registry import set_setting

        set_setting("active_project", slug)
    except Exception:
        pass


def load_active_project() -> None:
    """Load the persisted active project on startup."""
    global _active_project
    try:
        from memory_mcp.db.registry import get_setting

        slug = get_setting("active_project")
        if slug:
            _active_project = slug
    except Exception:
        pass


def _uid_from_manifest(path: Path) -> str | None:
    """Read the stable project uid out of `<path>/.claude-memory/manifest.json`.

    Returns None for anything unreadable - a folder the daemon has no rights to,
    a half-written file, an unresolved git conflict. Identity then falls back to
    path and name matching, exactly as before uids existed.
    """
    try:
        manifest = path / SNAPSHOT_DIRNAME / MANIFEST_NAME
        if not manifest.is_file():
            return None
        uid = json.loads(manifest.read_text()).get("project_id")
        return uid if isinstance(uid, str) and uid else None
    except Exception:  # noqa: BLE001
        return None


def _slug_from_uid(path: Path) -> str | None:
    """Match a folder to a project by the uid its committed snapshot carries."""
    uid = _uid_from_manifest(path)
    if not uid:
        return None
    try:
        from memory_mcp.repositories import ProjectRepository

        project = ProjectRepository().get_by_uid(uid)
    except Exception:  # noqa: BLE001
        return None
    return project.slug if project else None


def detect_project_from_cwd(cwd: str | None) -> str | None:
    """Detect a registered project purely from a directory, ignoring active state.

    1. Walk up from cwd looking for a committed .claude-memory/manifest.json and
       match on its project_id - the only identity that survives a move/rename
    2. Walk up looking for a portable .memory-mcp.duckdb
    3. Match the bound path, then the directory name, to a registered project
    Returns None when the directory is not a memory project.
    """
    if not cwd:
        return None

    cwd_path = Path(cwd).resolve()

    check = cwd_path
    for _ in range(10):
        slug = _slug_from_uid(check)
        if slug:
            return slug
        if check.parent == check:
            break
        check = check.parent

    check = cwd_path
    for _ in range(10):
        portable_db = check / PORTABLE_DB_NAME
        if portable_db.exists():
            slug = _slug_from_path(check)
            if slug:
                return slug
        if check.parent == check:
            break
        check = check.parent

    return _slug_from_path(cwd_path)


def get_active_project(cwd: str | None = None) -> str | None:
    """Get the active project, with CWD auto-detection fallback.

    Resolution order:
    1. The caller's active project (per-user in server mode, global in local)
    2. CWD-based detection (see `detect_project_from_cwd`)
    3. None
    """
    if settings.server_mode:
        try:
            from memory_mcp.db.registry import get_user_active_project

            slug = get_user_active_project(current_user().id)
        except Exception:
            slug = None
        if slug:
            return slug
        return detect_project_from_cwd(cwd)

    # This session's own choice wins over anything another session has set.
    session_id = current_session_id()
    if session_id:
        slug = _session_project(session_id)
        if slug:
            return slug

    if _active_project:
        return _active_project
    return detect_project_from_cwd(cwd)


def _slug_from_path(path: Path) -> str | None:
    """Find a registered project matching this directory.

    Priority: an explicit project_path (the folder a project is bound to,
    exact or an ancestor of cwd) wins; then a portable DB inside the folder;
    then a folder name that equals the slug.
    """
    from memory_mcp.models import is_global_project
    from memory_mcp.repositories import ProjectRepository
    from memory_mcp.utils.text import slugify

    dir_slug = slugify(path.name)
    if is_global_project(dir_slug):
        return None  # the reserved org-wide project is never a real folder

    try:
        projects = [
            p for p in ProjectRepository().list_all()
            if not is_global_project(p.slug)
        ]
        for p in projects:
            if p.project_path:
                bound = Path(p.project_path).resolve()
                if path == bound or bound in path.parents:
                    return p.slug
        for p in projects:
            if p.db_path and path.as_posix() in p.db_path:
                return p.slug
        for p in projects:
            if p.slug == dir_slug:
                return p.slug
    except Exception:
        pass

    return None


def resolve_project(project: str | None = None, cwd: str | None = None) -> str | None:
    """Resolve project slug: explicit > active > cwd-detected."""
    if project:
        return project
    return get_active_project(cwd)
