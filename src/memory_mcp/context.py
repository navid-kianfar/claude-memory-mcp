"""Project resolution: EXPLICIT, or derived from a cwd - never global state.

There is no "active project" any more (removed 2026-11-03, user decision). A
process-global that one caller sets and every other caller silently inherits
is a wrong-project-write waiting to happen when several projects are worked at
once - it caused exactly that on 2026-09-04 (seven tasks stored and mirrored
into the wrong project), and the rule that followed ("pass project= on every
write") depended on the agent REMEMBERING, which is model compliance, not a
mechanism. Now a project-scoped call without an explicit project FAILS with a
message that says what to pass; hooks and the CLI derive the project from the
working directory they already carry.

What remains keyed on the MCP session id is the MEMORY-session mapping
(`remember_memory_session`) - which memory session owns this connection's
clocks and claims. That is per-connection bookkeeping, not project state, and
cannot leak a write into the wrong project.
"""

import contextlib
import contextvars
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from memory_mcp.config import settings
from memory_mcp.constants import (
    LEGACY_SNAPSHOT_DIRNAME,
    MANIFEST_NAME,
    PORTABLE_DB_NAME,
    SNAPSHOT_DIRNAME,
)

_lock = threading.Lock()


def current_session_id() -> str | None:
    """The calling MCP session, or None when there is no MCP request in flight.

    Never raises: this is consulted from the CLI and from background tasks
    where there is no context at all.
    """
    try:
        from fastmcp.server.dependencies import get_context

        return getattr(get_context(), "session_id", None) or None
    except Exception:  # noqa: BLE001 - no context, or a FastMCP that lacks it
        return None


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


def _uid_from_manifest(path: Path) -> str | None:
    """Read the stable project uid out of `<path>/.agent-memory/manifest.json`.

    `.claude-memory/` (the name before the multi-harness rename) is a read-only
    fallback, so a teammate's older clone or a git pull mid-migration still
    resolves; the one-time rename to the new name happens in
    ProjectService.migrate_snapshot_dir, which runs before this on the claim
    path.

    Returns None for anything unreadable - a folder the daemon has no rights to,
    a half-written file, an unresolved git conflict. Identity then falls back to
    path and name matching, exactly as before uids existed.

    THIS IS WHY THE MANIFEST STAYED JSON when the memories moved into DuckDB:
    it is read here on every project detection, in the daemon, for every folder
    walked up from the cwd. Opening a DuckDB file to answer "which project is
    this?" would take a lock and cost milliseconds per candidate directory. The
    snapshot database carries the same uid as a fallback (below) for the case
    where the manifest is lost, and nothing more.
    """
    try:
        manifest = path / SNAPSHOT_DIRNAME / MANIFEST_NAME
        if not manifest.is_file():
            manifest = path / LEGACY_SNAPSHOT_DIRNAME / MANIFEST_NAME
        if not manifest.is_file():
            return _uid_from_snapshot_db(path)
        uid = json.loads(manifest.read_text()).get("project_id")
        if isinstance(uid, str) and uid:
            return uid
        return _uid_from_snapshot_db(path)
    except Exception:  # noqa: BLE001
        return None


def _uid_from_snapshot_db(path: Path) -> str | None:
    """Fallback only: the uid the snapshot database carries in snapshot_meta.

    Reached when manifest.json is missing or does not name a project - a
    hand-deleted manifest, a conflict resolved by removing the file. Guarded by
    the file existing first so the common path never opens DuckDB at all.
    """
    from memory_mcp.constants import SNAPSHOT_DB_NAME

    db_path = path / SNAPSHOT_DIRNAME / SNAPSHOT_DB_NAME
    if not db_path.is_file():
        db_path = path / LEGACY_SNAPSHOT_DIRNAME / SNAPSHOT_DB_NAME
    if not db_path.is_file():
        return None
    try:
        import duckdb

        conn = duckdb.connect()
        try:
            conn.execute(
                "ATTACH '{}' AS s (READ_ONLY)".format(str(db_path).replace("'", "''"))
            )
            row = conn.execute(
                "SELECT value FROM s.snapshot_meta WHERE key = 'project_id'"
            ).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a locked or corrupt snapshot is not fatal
        return None
    return row[0] if row and isinstance(row[0], str) and row[0] else None


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

    1. Walk up from cwd looking for a committed .agent-memory/manifest.json
       (or the legacy .claude-memory one) and match on its project_id - the
       only identity that survives a move/rename
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
    """Resolve project slug: explicit > cwd-detected > None.

    There is no fallback beyond these two - a caller that has neither gets
    None and must be told to name its project, never handed one silently.
    """
    if project:
        return project
    return detect_project_from_cwd(cwd)
