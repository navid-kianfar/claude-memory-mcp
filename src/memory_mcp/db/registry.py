"""SQLite-backed local registry: the project list + app settings.

Per-project memory databases stay DuckDB - vector search needs the VSS
extension. Only the lightweight local metadata lives here in plain SQLite
(Python stdlib, so no extra dependency): which projects exist, the active
project, and the selected embedding model.

On first run this transparently imports an older DuckDB registry
(`registry.duckdb`) and the legacy `active_project.json` / `model_config.json`
files, so existing installs upgrade without losing anything.
"""

import hashlib
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memory_mcp.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    slug          TEXT PRIMARY KEY,
    project_uid   TEXT,
    display_name  TEXT NOT NULL,
    description   TEXT,
    created_at    TEXT NOT NULL,
    last_accessed TEXT NOT NULL,
    db_path       TEXT NOT NULL,
    project_path  TEXT,
    owner         TEXT,
    backend       TEXT NOT NULL DEFAULT 'local',
    remote_url    TEXT
);
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS template_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
    category    TEXT NOT NULL,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_template_items_tpl ON template_items(template_id);
CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    username     TEXT NOT NULL UNIQUE,
    display_name TEXT,
    role         TEXT NOT NULL DEFAULT 'member',
    token_hash   TEXT,
    session_hash TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    last_login   TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_token ON users(token_hash);
CREATE INDEX IF NOT EXISTS idx_users_session ON users(session_hash);
CREATE TABLE IF NOT EXISTS project_links (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                   TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
    provider               TEXT NOT NULL DEFAULT 'asoode',
    base_url               TEXT NOT NULL,
    socket_url             TEXT,
    remote_project_id      TEXT,
    remote_work_package_id TEXT,
    label                  TEXT,
    is_default             INTEGER NOT NULL DEFAULT 0,
    default_list_id        TEXT,
    default_assignee_id    TEXT,
    state_list_map         TEXT,
    match_paths            TEXT,
    active                 INTEGER NOT NULL DEFAULT 1,
    created_at             TEXT NOT NULL,
    UNIQUE(slug, remote_work_package_id)
);
CREATE INDEX IF NOT EXISTS idx_project_links_slug ON project_links(slug);
CREATE TABLE IF NOT EXISTS client_sessions (
    session_id      TEXT PRIMARY KEY,
    slug            TEXT,
    cwd             TEXT,
    transcript_path TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_dispatches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    slug        TEXT,
    agent_type  TEXT NOT NULL,
    tool_use_id TEXT,
    description TEXT,
    at          TEXT NOT NULL,
    asked       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_session_dispatches ON session_dispatches(session_id);
CREATE TABLE IF NOT EXISTS session_subagent_stops (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    agent_id    TEXT,
    agent_type  TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_subagent_stops ON session_subagent_stops(session_id);
CREATE TABLE IF NOT EXISTS session_edits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    slug        TEXT,
    path        TEXT NOT NULL,
    tool        TEXT NOT NULL,
    by_agent    TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_edits ON session_edits(session_id);
"""

# `project_links` above is the task bridge's routing table.
#
# It keys on remote_work_package_id, not on remote_project_id, because a task
# always lives under a BOARD: asoode has no route attaching a task to a project,
# and the same is true of Asana (project/section), Monday (board/group), Trello
# (board/list) and Jira (project + issue type). remote_project_id is kept for
# building a URL a human can open, never for routing.
#
# `provider` defaults to 'asoode' and is not read yet - it is the seam for the
# other platforms, so a project can hold links to several at once. One memory project
# links to MANY remote boards - `match_paths` (a JSON array of repo subpaths) is
# what lets a monorepo send apps/backend/** and apps/frontend/** to different
# boards instead of one pile.
#
# Linking is always explicit, copying the rule from ProjectService.bind_backend:
# projects default to unlinked and are never auto-bound, so a private project
# cannot leak onto someone else's server.
#
# Credentials are deliberately NOT in this table: the PAT lives in
# get_credential/set_credential below, keyed by server URL, so one token covers
# every project and never enters the committable .claude-memory snapshot.
#
# `client_sessions` / `session_dispatches` / `session_edits` are the hook
# ledgers: what the daemon now remembers about a CLAUDE CODE session, keyed on
# the session_id every hook payload carries. Before them, every hook script
# extracted only `cwd` and the daemon could not tell which session dispatched an
# agent or edited a file - so "you have edited five files without handing this
# to the specialist who owns it" was unsayable.
#
# They are machine-local bookkeeping, not project memory, which is why they live
# here and not in the per-project DuckDB: nothing in them should travel in the
# committable .claude-memory snapshot or reach an org server, and both hook paths
# already pay the cost of opening this registry. Nothing but a path, a tool name
# and an agent type is stored - never file content, never a prompt. Pruned after
# a week (prune_session_ledgers), because a stale session is noise.

_migration_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_project_uid() -> str:
    """A fresh stable project identity."""
    return str(uuid.uuid4())


@contextmanager
def registry_conn():
    """Open the SQLite registry, ensuring schema + legacy migration."""
    settings.ensure_dirs()
    conn = sqlite3.connect(str(settings.registry_path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(_SCHEMA)
        _ensure_columns(conn)
        _migrate_legacy_once(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add columns introduced after a registry.db already existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "project_path" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN project_path TEXT")
    if "owner" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN owner TEXT")
    if "backend" not in cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN backend TEXT NOT NULL DEFAULT 'local'"
        )
    if "remote_url" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN remote_url TEXT")
    if "project_uid" not in cols:
        # Stable identity, independent of slug and folder path. Written into the
        # committed .claude-memory/manifest.json so a project survives being
        # moved or renamed, on this machine and on a teammate's.
        conn.execute("ALTER TABLE projects ADD COLUMN project_uid TEXT")
    # session_dispatches predates `asked` on any registry a release candidate
    # touched. Without the column every INSERT would fail - silently, because the
    # ledger accessors swallow errors - and the ledger would stop filling.
    dispatch_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(session_dispatches)").fetchall()
    }
    if dispatch_cols and "asked" not in dispatch_cols:
        conn.execute(
            "ALTER TABLE session_dispatches ADD COLUMN asked INTEGER NOT NULL DEFAULT 0"
        )
    # Created here rather than in _SCHEMA: executescript runs before the ALTER
    # above, so on an existing registry the column would not exist yet.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_uid "
        "ON projects(project_uid) WHERE project_uid IS NOT NULL"
    )
    _backfill_project_uids(conn)


def _backfill_project_uids(conn: sqlite3.Connection) -> None:
    """Give every pre-existing project a uid, so identity works from now on."""
    rows = conn.execute(
        "SELECT slug FROM projects WHERE project_uid IS NULL OR project_uid = ''"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE projects SET project_uid = ? WHERE slug = ?",
            (new_project_uid(), row[0]),
        )


def _migrate_legacy_once(conn: sqlite3.Connection) -> None:
    done = conn.execute(
        "SELECT 1 FROM app_settings WHERE key = 'registry_ready'"
    ).fetchone()
    if done:
        return
    with _migration_lock:
        done = conn.execute(
            "SELECT 1 FROM app_settings WHERE key = 'registry_ready'"
        ).fetchone()
        if done:
            return
        _import_legacy_duckdb_registry(conn)
        _import_legacy_json(conn)
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('registry_ready', '1')"
        )
        conn.commit()


def _import_legacy_duckdb_registry(conn: sqlite3.Connection) -> None:
    """Copy projects from a pre-existing DuckDB registry, if present."""
    legacy = settings.data_dir / "registry.duckdb"
    if not legacy.exists():
        return
    try:
        import duckdb

        src = duckdb.connect(str(legacy), read_only=True)
        try:
            rows = src.execute(
                "SELECT slug, display_name, description, created_at, "
                "last_accessed, db_path FROM projects"
            ).fetchall()
        finally:
            src.close()
    except Exception:  # noqa: BLE001 - a missing/corrupt legacy DB is non-fatal
        return

    for r in rows:
        created = r[3].isoformat() if hasattr(r[3], "isoformat") else (str(r[3]) or now_iso())
        accessed = r[4].isoformat() if hasattr(r[4], "isoformat") else (str(r[4]) or now_iso())
        conn.execute(
            "INSERT OR IGNORE INTO projects "
            "(slug, display_name, description, created_at, last_accessed, db_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (r[0], r[1], r[2], created, accessed, r[5]),
        )


def _import_legacy_json(conn: sqlite3.Connection) -> None:
    """Copy the legacy active_project.json / model_config.json values."""
    import json

    active = settings.data_dir / "active_project.json"
    if active.exists():
        try:
            slug = json.loads(active.read_text()).get("active_project")
            if slug:
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('active_project', ?)",
                    (slug,),
                )
        except Exception:  # noqa: BLE001
            pass

    model = settings.data_dir / "model_config.json"
    if model.exists():
        try:
            name = json.loads(model.read_text()).get("embedding_model")
            if name:
                conn.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('embedding_model', ?)",
                    (name,),
                )
        except Exception:  # noqa: BLE001
            pass


# ---------- app settings key/value store ----------


def get_setting(key: str, default: str | None = None) -> str | None:
    with registry_conn() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with registry_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )


# ---------- hook session ledgers (Claude Code sessions) ----------
#
# Every function here is wrapped: a hook runs before an edit, and a registry
# locked by another process must cost the caller nothing worse than a missing
# ledger row. A write that fails is dropped silently, a read that fails is empty.

AGENT_ID_SUPPORTED_KEY = "hooks:agent_id_supported"


def touch_client_session(
    session_id: str,
    *,
    slug: str | None = None,
    cwd: str | None = None,
    transcript_path: str | None = None,
) -> None:
    """Record that this Claude Code session was seen, keeping `first_seen`.

    Called from every hook route, so it is an upsert that only ever fills in
    blanks: a later hook with no `transcript_path` must not erase the one an
    earlier hook knew.
    """
    if not session_id:
        return
    ts = now_iso()
    try:
        with registry_conn() as conn:
            # INSERT OR IGNORE + UPDATE rather than an UPSERT clause: two plain
            # statements work on every SQLite this package can be installed
            # against, and first_seen is preserved by construction.
            conn.execute(
                "INSERT OR IGNORE INTO client_sessions "
                "(session_id, slug, cwd, transcript_path, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, slug or None, cwd or None, transcript_path or None, ts, ts),
            )
            conn.execute(
                "UPDATE client_sessions SET "
                "  slug = COALESCE(?, slug), "
                "  cwd = COALESCE(?, cwd), "
                "  transcript_path = COALESCE(?, transcript_path), "
                "  last_seen = ? "
                "WHERE session_id = ?",
                (slug or None, cwd or None, transcript_path or None, ts, session_id),
            )
    except Exception:  # noqa: BLE001 - a ledger must never fail a hook
        pass


def client_session(session_id: str) -> dict | None:
    if not session_id:
        return None
    try:
        with registry_conn() as conn:
            row = conn.execute(
                "SELECT * FROM client_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return dict(row) if row else None


def record_dispatch(
    session_id: str,
    *,
    slug: str | None = None,
    agent_type: str,
    tool_use_id: str | None = None,
    description: str | None = None,
    asked: bool = False,
) -> None:
    """Append "this session dispatched that agent type". Append-only on purpose:
    the same agent dispatched twice is two rows, and the delegation check asks
    whether a role was EVER dispatched this session.

    `asked`: the hook put a permission prompt in front of this dispatch, so
    whether it ran is unknown (PreToolUse fires before the user answers). Such a
    row still counts as dispatch HISTORY, but not as RUNNING - a declined prompt
    must not leave a phantom agent that makes every dispatch for the next hour
    prompt too."""
    if not session_id or not agent_type:
        return
    try:
        with registry_conn() as conn:
            conn.execute(
                "INSERT INTO session_dispatches "
                "(session_id, slug, agent_type, tool_use_id, description, at, asked) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, slug or None, agent_type, tool_use_id or None,
                 description or None, now_iso(), 1 if asked else 0),
            )
    except Exception:  # noqa: BLE001
        pass


def record_edit(
    session_id: str,
    *,
    slug: str | None = None,
    path: str,
    tool: str,
    by_agent: str | None = None,
) -> None:
    """Append "this session was about to edit that file".

    `by_agent` is the agent type when the payload carried an `agent_id`, and NULL
    for the lead. On a build that sends no `agent_id` every edit looks like the
    lead's - see `agent_id_supported`.
    """
    if not session_id or not path:
        return
    try:
        with registry_conn() as conn:
            conn.execute(
                "INSERT INTO session_edits "
                "(session_id, slug, path, tool, by_agent, at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, slug or None, path, tool or "", by_agent or None, now_iso()),
            )
    except Exception:  # noqa: BLE001
        pass


def record_subagent_stop(
    session_id: str, *, agent_id: str | None = None, agent_type: str | None = None,
) -> None:
    """Append "a subagent of this session finished" (the SubagentStop hook).

    It cannot be joined to its dispatch row: PreToolUse carries a `tool_use_id`,
    SubagentStop an `agent_id`, and no payload carries both. So it is a count, and
    `running_dispatches` subtracts counts - which is all "how many are running
    now?" needs."""
    if not session_id:
        return
    try:
        with registry_conn() as conn:
            conn.execute(
                "INSERT INTO session_subagent_stops (session_id, agent_id, agent_type, at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, agent_id or None, agent_type or None, now_iso()),
            )
    except Exception:  # noqa: BLE001
        pass


#: A dispatch older than this is presumed finished. It bounds the damage of a
#: missed SubagentStop (a hook that failed, a client that never sends it): the
#: concurrency prompt then fires on a real burst, and never on dispatches that
#: ended long ago but were not counted out.
RUNNING_WINDOW_MINUTES = 60


def running_dispatches(session_id: str, *, window_minutes: int = RUNNING_WINDOW_MINUTES) -> int:
    """How many of this session's agents are still running, best estimate.

    Dispatches in the window minus stops in the window, floored at 0. When it is
    wrong it is wrong LOW - a stop counted against an older dispatch - which
    means one prompt fewer, never a prompt that cannot be satisfied.
    """
    if not session_id:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
    try:
        with registry_conn() as conn:
            started = conn.execute(
                "SELECT COUNT(*) FROM session_dispatches "
                "WHERE session_id = ? AND at >= ? AND asked = 0",
                (session_id, cutoff),
            ).fetchone()[0]
            stopped = conn.execute(
                "SELECT COUNT(*) FROM session_subagent_stops WHERE session_id = ? AND at >= ?",
                (session_id, cutoff),
            ).fetchone()[0]
    except Exception:  # noqa: BLE001 - unknown means "do not prompt"
        return 0
    return max(0, int(started) - int(stopped))


def dispatches_for(session_id: str) -> list[dict]:
    if not session_id:
        return []
    try:
        with registry_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM session_dispatches WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [dict(r) for r in rows]


def edits_for(session_id: str) -> list[dict]:
    if not session_id:
        return []
    try:
        with registry_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM session_edits WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [dict(r) for r in rows]


def prune_session_ledgers(days: int = 7) -> int:
    """Drop ledger rows older than `days`. Returns rows deleted (0 on failure).

    Called from the SessionStart path, which is the one hook that fires once per
    session rather than once per turn.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        with registry_conn() as conn:
            deleted = 0
            for sql in (
                "DELETE FROM session_dispatches WHERE at < ?",
                "DELETE FROM session_subagent_stops WHERE at < ?",
                "DELETE FROM session_edits WHERE at < ?",
                "DELETE FROM client_sessions WHERE last_seen < ?",
            ):
                deleted += conn.execute(sql, (cutoff,)).rowcount
            return deleted
    except Exception:  # noqa: BLE001
        return 0


def note_agent_id(agent_id: str | None) -> None:
    """Remember, once, that this CLI build sends `agent_id` on hook payloads.

    `agent_id`/`agent_type` are documented hook fields but were NOT verified on
    the installed CLI (2.1.236), so nothing may depend on them. This is how the
    daemon learns the truth from the payloads themselves: until a non-empty
    `agent_id` arrives, "is this a subagent?" honestly answers "unknown" instead
    of guessing "the lead".
    """
    if not agent_id:
        return
    try:
        if get_setting(AGENT_ID_SUPPORTED_KEY) == "1":
            return
        set_setting(AGENT_ID_SUPPORTED_KEY, "1")
    except Exception:  # noqa: BLE001
        pass


def agent_id_supported() -> bool:
    try:
        return get_setting(AGENT_ID_SUPPORTED_KEY) == "1"
    except Exception:  # noqa: BLE001
        return False


# ---------- remote credentials (client side) ----------
#
# Org-server tokens for remote-bound projects, keyed by server URL. Stored in the
# local app_settings only (never in the committable .claude-memory snapshot), so a
# private project's credentials never travel with a repo.


def get_credential(remote_url: str) -> str | None:
    if not remote_url:
        return None
    return get_setting(f"cred:{remote_url.rstrip('/')}")


def set_credential(remote_url: str, token: str) -> None:
    set_setting(f"cred:{remote_url.rstrip('/')}", token)


# ---------- users + tokens (server mode) ----------
#
# Dormant in local mode: the table is created but never read or written unless
# the server-mode auth paths call these functions. API tokens are high-entropy
# random secrets (not user-chosen passwords), so a single SHA-256 is the correct,
# standard hash - no salt/PBKDF needed - and keeps this pure stdlib.


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token() -> str:
    """Generate a new opaque bearer token. Shown once; only its hash is stored."""
    return "mmcp_" + secrets.token_urlsafe(32)


def _user_public(row: sqlite3.Row | None) -> dict | None:
    """Public user view - never exposes token/session hashes."""
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "last_login": row["last_login"],
    }


def create_user(
    username: str, display_name: str | None = None, role: str = "member"
) -> tuple[dict, str]:
    """Create a user and return (public_user, plaintext_token).

    The plaintext token is returned exactly once - only its hash is persisted.
    Raises ValueError if the username already exists.
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    if role not in ("admin", "member"):
        raise ValueError("role must be 'admin' or 'member'")
    user_id = secrets.token_hex(8)
    token = issue_token()
    with registry_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone()
        if exists:
            raise ValueError(f"user '{username}' already exists")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, token_hash, "
            "active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (user_id, username, display_name, role, _hash_token(token), now_iso()),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_public(row), token


def list_users() -> list[dict]:
    with registry_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at ASC"
        ).fetchall()
    return [_user_public(r) for r in rows]


def get_user(user_id: str) -> dict | None:
    with registry_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_public(row)


def get_user_by_username(username: str) -> dict | None:
    with registry_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
        ).fetchone()
    return _user_public(row)


def authenticate_token(token: str) -> dict | None:
    """Resolve a bearer token to a public user, or None. Only active users match."""
    if not token:
        return None
    with registry_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE token_hash = ? AND active = 1",
            (_hash_token(token),),
        ).fetchone()
    return _user_public(row)


def authenticate_session(session_token: str) -> dict | None:
    """Resolve a UI session token to a public user, or None. Active users only."""
    if not session_token:
        return None
    with registry_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE session_hash = ? AND active = 1",
            (_hash_token(session_token),),
        ).fetchone()
    return _user_public(row)


def create_session(user_id: str) -> str | None:
    """Start a UI session for a user: store a fresh session-token hash, stamp
    last_login, and return the plaintext session token (goes in the cookie).
    Returns None if the user does not exist or is inactive."""
    session_token = issue_token()
    with registry_conn() as conn:
        row = conn.execute(
            "SELECT active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None or not row["active"]:
            return None
        conn.execute(
            "UPDATE users SET session_hash = ?, last_login = ? WHERE id = ?",
            (_hash_token(session_token), now_iso(), user_id),
        )
    return session_token


def clear_session(session_token: str) -> None:
    """Invalidate a UI session (logout)."""
    if not session_token:
        return
    with registry_conn() as conn:
        conn.execute(
            "UPDATE users SET session_hash = NULL WHERE session_hash = ?",
            (_hash_token(session_token),),
        )


def rotate_token(user_id: str) -> str | None:
    """Issue a new bearer token for a user; returns the plaintext once."""
    token = issue_token()
    with registry_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET token_hash = ? WHERE id = ?",
            (_hash_token(token), user_id),
        )
        if cur.rowcount == 0:
            return None
    return token


def set_user_active(user_id: str, active: bool) -> None:
    with registry_conn() as conn:
        conn.execute(
            "UPDATE users SET active = ? WHERE id = ?",
            (1 if active else 0, user_id),
        )


def count_users() -> int:
    with registry_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def count_admins() -> int:
    with registry_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
        ).fetchone()[0]


# ---------- per-user active project (server mode) ----------
#
# In local mode the single global 'active_project' key is used (see context.py).
# In server mode each user gets their own key so concurrent users never clobber
# one another's active project.


def get_user_active_project(user_id: str) -> str | None:
    return get_setting(f"active_project:{user_id}")


def set_user_active_project(user_id: str, slug: str) -> None:
    set_setting(f"active_project:{user_id}", slug)


# ---------- project links (the asoode bridge's routing table) ----------


def _link_public(row: sqlite3.Row) -> dict:
    import json

    link = dict(row)
    for field in ("state_list_map", "match_paths"):
        raw = link.get(field)
        link[field] = json.loads(raw) if raw else None
    link["is_default"] = bool(link["is_default"])
    link["active"] = bool(link["active"])
    return link


# What a `match_paths` entry may contain, and why so little.
#
# An entry is a repo-relative PREFIX matched on whole path segments: `apps/api`
# owns `apps/api/tests/x.py` and not `apps/api-old/x.py`. A trailing `/**` or `/*`
# is accepted and normalised away, because it is what people type and a prefix
# already means "and everything under it". ANY OTHER glob is refused here, at
# write time, naming the entry: `apps/*/api` or `*.py` would otherwise be stored
# and silently never match, and tasks would pile onto the default board with no
# error - which is exactly the bug path routing exists to fix. "Longest prefix
# wins" also has no defensible tie-break once a wildcard sits mid-path, so
# supporting one needs a match mode and a stated rule first, not a looser check.
_GLOB_CHARS = frozenset("*?[]")

# Sentinel for update_project_link: "leave this field alone", distinct from None,
# which for match_paths and label means "clear it".
_UNSET = object()


def _normalise_match_path(entry: object) -> str:
    """One `match_paths` entry in its stored form, or ValidationError naming it."""
    from memory_mcp.exceptions import ValidationError

    if not isinstance(entry, str):
        raise ValidationError(f"match_paths entry {entry!r} is not a string")
    path = entry.strip().replace("\\", "/")
    if path.startswith(("/", "~")) or (len(path) > 1 and path[1] == ":"):
        raise ValidationError(
            f"match_paths entry {entry!r} is absolute. Give a path relative to "
            "the repository root, e.g. 'apps/api'."
        )
    while True:
        path = path.rstrip("/")
        if path.endswith("/**"):
            path = path[:-3]
        elif path.endswith("/*"):
            path = path[:-2]
        else:
            break
    segments = [s for s in path.split("/") if s not in ("", ".")]
    if ".." in segments:
        raise ValidationError(
            f"match_paths entry {entry!r} contains '..'. A binding names a "
            "subtree inside this repository, never a way out of it."
        )
    normalised = "/".join(segments)
    if any(ch in _GLOB_CHARS for ch in normalised):
        raise ValidationError(
            f"match_paths entry {entry!r} uses a glob that is not supported. Only "
            "a plain prefix is matched - 'apps/backend', or 'apps/backend/**', "
            "which means the same. A wildcard anywhere else would be stored and "
            "silently never match."
        )
    if not normalised:
        raise ValidationError(
            f"match_paths entry {entry!r} names the whole repository. Leave "
            "match_paths empty and make that board the default instead."
        )
    return normalised


def _validate_match_paths(entries: object) -> list[str] | None:
    """Normalise a `match_paths` list for storage, refusing what cannot match.

    Called by every writer - upsert_project_link and update_project_link - so no
    surface can store a pattern the router cannot honour. Blank entries are
    dropped, duplicates collapse, order is kept. None or an empty result is
    stored as NULL: "this board owns no subtree".
    """
    from memory_mcp.exceptions import ValidationError

    if entries is None:
        return None
    if isinstance(entries, str) or not isinstance(entries, (list, tuple)):
        raise ValidationError(
            'match_paths must be a list of repo-relative paths, e.g. ["apps/api"]'
        )
    normalised: list[str] = []
    for entry in entries:
        if isinstance(entry, str) and not entry.strip():
            continue
        path = _normalise_match_path(entry)
        if path not in normalised:
            normalised.append(path)
    return normalised or None


def upsert_project_link(
    slug: str, *, base_url: str, remote_project_id: str,
    remote_work_package_id: str, socket_url: str | None = None,
    label: str | None = None, default_list_id: str | None = None,
    default_assignee_id: str | None = None, state_list_map: dict | None = None,
    match_paths: list | None = None, provider: str = "asoode",
    is_default: bool = True,
) -> dict:
    """Create or refresh the link between a memory project and a remote board.

    Keyed on (slug, remote_work_package_id), which is the UNIQUE constraint, so
    re-running a bootstrap updates the existing row rather than adding a second
    link to the same board.

    `match_paths=None` KEEPS what an existing row holds; a list (even `[]`)
    replaces it. Before this, every refresh - `refresh_state_map` after a column
    change, a re-run bootstrap, a re-attach - wrote NULL over the board's path
    bindings, and routing would quietly have gone back to the default.
    """
    import json

    paths = _validate_match_paths(match_paths)
    with registry_conn() as conn:
        # A project links to many boards but only one is the default, so promoting
        # this row demotes the rest. Without this, re-linking a project to a new
        # board leaves two rows flagged default and get_default_project_link keeps
        # returning the OLDER one - the push would silently go to the old board.
        if is_default:
            conn.execute(
                "UPDATE project_links SET is_default = 0 WHERE slug = ?", (slug,)
            )
        conn.execute(
            """INSERT INTO project_links (
                   slug, provider, base_url, socket_url, remote_project_id,
                   remote_work_package_id, label, is_default, default_list_id,
                   default_assignee_id, state_list_map, match_paths, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(slug, remote_work_package_id) DO UPDATE SET
                   base_url = excluded.base_url,
                   socket_url = excluded.socket_url,
                   remote_project_id = excluded.remote_project_id,
                   label = excluded.label,
                   is_default = excluded.is_default,
                   default_list_id = excluded.default_list_id,
                   default_assignee_id = excluded.default_assignee_id,
                   state_list_map = excluded.state_list_map,
                   match_paths = CASE WHEN ? THEN excluded.match_paths
                                      ELSE project_links.match_paths END,
                   active = 1""",
            (
                slug, provider, base_url.rstrip("/"), socket_url,
                remote_project_id, remote_work_package_id, label,
                1 if is_default else 0, default_list_id, default_assignee_id,
                json.dumps(state_list_map) if state_list_map else None,
                json.dumps(paths) if paths else None,
                now_iso(),
                1 if match_paths is not None else 0,
            ),
        )
        row = conn.execute(
            "SELECT * FROM project_links WHERE slug = ? AND remote_work_package_id = ?",
            (slug, remote_work_package_id),
        ).fetchone()
    return _link_public(row)


def get_project_links(slug: str, *, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM project_links WHERE slug = ?"
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY is_default DESC, id ASC"
    with registry_conn() as conn:
        rows = conn.execute(sql, (slug,)).fetchall()
    return [_link_public(r) for r in rows]


def get_default_project_link(slug: str) -> dict | None:
    links = get_project_links(slug)
    return links[0] if links else None


def update_project_link(
    link_id: int, *, match_paths: object = _UNSET, label: object = _UNSET,
    is_default: bool | None = None,
) -> dict | None:
    """Change a link in place, with no network call. None when there is no such link.

    Only what is passed changes. `match_paths=None` or `[]` clears the binding;
    `label=None` or `""` clears the label, so tasks route by work package id.
    `is_default=True` promotes this link and demotes the rest, exactly as
    upsert_project_link does. `is_default=False` on the CURRENT default is
    refused: a project whose boards have no default cannot route a task that
    names none, and the right move is promoting another board.
    """
    import json

    from memory_mcp.exceptions import ValidationError

    fields: dict[str, object] = {}
    if match_paths is not _UNSET:
        paths = _validate_match_paths(match_paths)
        fields["match_paths"] = json.dumps(paths) if paths else None
    if label is not _UNSET:
        fields["label"] = (str(label).strip() if label is not None else "") or None

    with registry_conn() as conn:
        row = conn.execute(
            "SELECT * FROM project_links WHERE id = ?", (link_id,)
        ).fetchone()
        if row is None:
            return None
        if is_default is False and row["is_default"]:
            raise ValidationError(
                f"board {row['label'] or row['remote_work_package_id']!r} is the "
                f"default for '{row['slug']}'. Promote another board to default "
                "instead - a project with boards and no default cannot route a "
                "task that names none."
            )
        if is_default:
            conn.execute(
                "UPDATE project_links SET is_default = 0 WHERE slug = ?",
                (row["slug"],),
            )
            fields["is_default"] = 1
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            conn.execute(
                f"UPDATE project_links SET {assignments} WHERE id = ?",
                (*fields.values(), link_id),
            )
        row = conn.execute(
            "SELECT * FROM project_links WHERE id = ?", (link_id,)
        ).fetchone()
    return _link_public(row)


def delete_project_link(link_id: int) -> bool:
    """Forget a link. The remote board is left completely alone."""
    with registry_conn() as conn:
        cur = conn.execute("DELETE FROM project_links WHERE id = ?", (link_id,))
    return cur.rowcount > 0


def linked_slugs() -> list[str]:
    """Every project with at least one active link - what a machine-wide
    outbox sweep or catch-up iterates."""
    with registry_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT slug FROM project_links WHERE active = 1 ORDER BY slug"
        ).fetchall()
    return [r[0] for r in rows]
