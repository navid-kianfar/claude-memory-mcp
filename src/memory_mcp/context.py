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

Nothing is keyed on the MCP session id either. The transport is stateless
HTTP, so every request arrives under a freshly minted MCP session id: a map
from it to the memory session never read back what it stored, and was removed.
Clock and claim calls name their memory session explicitly (`session_id=`).
"""

import contextlib
import contextvars
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from memory_mcp.config import settings
from memory_mcp.constants import (
    LEGACY_SNAPSHOT_DIRNAME,
    MANIFEST_NAME,
    PORTABLE_DB_NAME,
    SNAPSHOT_DIRNAME,
)

if TYPE_CHECKING:
    from memory_mcp.models import ProjectInfo

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

    The same evidence `detect_project_strictly` accepts - a committed manifest
    uid, a registered portable DB, a bound project_path at or above cwd - and
    nothing looser. The old fallbacks (cwd a SUBSTRING of some db_path, a folder
    named like a slug) handed a session opened in the home folder whichever
    project lived under it: its rules, its brief, its board. Returns None when
    the directory is not a memory project.
    """
    if not cwd:
        return None
    return detect_project_strictly(cwd)


#: How many directories a cwd walk visits: the cwd and its nearest parents.
_WALK_UP_LIMIT = 10


def _walk_up(path: Path) -> tuple[Path, ...]:
    """`path` and its parents, nearest first, at most _WALK_UP_LIMIT of them."""
    return (path, *path.parents)[:_WALK_UP_LIMIT]


def detect_project_strictly(cwd: str) -> str | None:
    """A project only on evidence that names it, never on a resemblance.

    For memory_session_start, whose answer scopes every write of the session.
    Accepts, nearest folder first:
    1. a committed manifest whose project_id is a registered uid;
    2. a portable .memory-mcp.duckdb that is the registered db_path of a project;
    3. a registered project_path equal to the cwd or an ancestor of it.
    `detect_project_from_cwd`'s loose fallbacks - a cwd that is a SUBSTRING of
    some db_path, a folder named like a slug - are left out: from a home folder
    the substring match named whichever project lived under it.
    """
    cwd_path = Path(cwd).resolve()
    folders = _walk_up(cwd_path)
    for folder in folders:
        slug = _slug_from_uid(folder)
        if slug:
            return slug

    projects = _real_projects()
    for folder in folders:
        portable_db = folder / PORTABLE_DB_NAME
        if not portable_db.exists():
            continue
        slug = _slug_owning_file(projects, portable_db.resolve())
        if slug:
            return slug
    return _slug_bound_to(projects, cwd_path)


def _real_projects() -> tuple["ProjectInfo", ...]:
    """Every registered project except the reserved org-wide one; () if unreadable."""
    from memory_mcp.models import is_global_project
    from memory_mcp.repositories import ProjectRepository

    try:
        projects = ProjectRepository().list_all()
    except Exception:  # noqa: BLE001 - no registry means no project, not a crash
        return ()
    return tuple(p for p in projects if not is_global_project(p.slug))


def _slug_owning_file(projects: Sequence["ProjectInfo"], db_file: Path) -> str | None:
    for p in projects:
        if p.db_path and Path(p.db_path).resolve() == db_file:
            return p.slug
    return None


def _slug_bound_to(projects: Sequence["ProjectInfo"], path: Path) -> str | None:
    for p in projects:
        if not p.project_path:
            continue
        bound = Path(p.project_path).resolve()
        if path == bound or bound in path.parents:
            return p.slug
    return None


def resolve_project(project: str | None = None, cwd: str | None = None) -> str | None:
    """Resolve project slug: explicit > cwd-detected > None.

    There is no fallback beyond these two - a caller that has neither gets
    None and must be told to name its project, never handed one silently.
    """
    if project:
        return project
    return detect_project_from_cwd(cwd)
