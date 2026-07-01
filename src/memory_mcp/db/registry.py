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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from memory_mcp.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    slug          TEXT PRIMARY KEY,
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
"""

_migration_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
