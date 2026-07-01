"""Active project context - auto-detect from CWD, persist to the SQLite registry.

Two isolation models live here, selected by settings.mode:

- local (default): a single process-global active project (`_active_project`),
  exactly as before - one user, one machine.
- server: a per-request identity (`_request_user`, a contextvar) and a per-user
  active project, so concurrent users never clobber each other. The old global
  is never consulted in server mode.
"""

import contextlib
import contextvars
import threading
from dataclasses import dataclass
from pathlib import Path

from memory_mcp.config import settings
from memory_mcp.services.portable_service import PORTABLE_DB_NAME

_active_project: str | None = None
_lock = threading.Lock()


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


def detect_project_from_cwd(cwd: str | None) -> str | None:
    """Detect a registered project purely from a directory, ignoring active state.

    1. Walk up from cwd looking for a portable .memory-mcp.duckdb
    2. Match the directory name to a registered project slug
    Returns None when the directory is not a memory project.
    """
    if not cwd:
        return None

    cwd_path = Path(cwd).resolve()

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
