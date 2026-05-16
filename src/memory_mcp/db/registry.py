"""SQLite-backed local registry: the project list + app settings.

Per-project memory databases stay DuckDB - vector search needs the VSS
extension. Only the lightweight local metadata lives here in plain SQLite
(Python stdlib, so no extra dependency): which projects exist, the active
project, and the selected embedding model.

On first run this transparently imports an older DuckDB registry
(`registry.duckdb`) and the legacy `active_project.json` / `model_config.json`
files, so existing installs upgrade without losing anything.
"""

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
    db_path       TEXT NOT NULL
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
        _migrate_legacy_once(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


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
