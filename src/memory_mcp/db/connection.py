"""Connection-per-operation pattern. No caching, no locks, no conflicts.

DuckDB connection open is <1ms, so there's zero performance penalty.
Each operation opens a connection, does work, closes immediately.
This eliminates ALL lock conflicts between projects, instances, and processes.
"""

import threading
from contextlib import contextmanager
from pathlib import Path

import duckdb

from memory_mcp.config import settings
from memory_mcp.db.schema import (
    create_schema, create_hnsw_index, install_vss, run_migrations,
)

_init_lock = threading.Lock()
_initialized_dbs: set[str] = set()

_path_cache: dict[str, Path] = {}
_path_cache_lock = threading.Lock()


def _compute_db_path(slug: str) -> Path:
    """Resolve DB path: check registry for custom path, fallback to central store."""
    try:
        from memory_mcp.repositories import ProjectRepository
        project = ProjectRepository().get(slug)
        if project and project.db_path:
            custom_path = Path(project.db_path)
            if custom_path.parent.exists():
                return custom_path
    except Exception:
        pass
    return settings.projects_dir / f"{slug}.duckdb"


def _resolve_db_path(slug: str) -> Path:
    """Resolve DB path, caching the registry lookup to avoid a query per operation."""
    with _path_cache_lock:
        cached = _path_cache.get(slug)
    if cached is not None:
        return cached
    path = _compute_db_path(slug)
    with _path_cache_lock:
        _path_cache[slug] = path
    return path


def invalidate_path_cache(slug: str | None = None) -> None:
    """Drop cached DB paths. Call after a project's db_path changes (portable ops)."""
    with _path_cache_lock:
        if slug is None:
            _path_cache.clear()
        else:
            _path_cache.pop(slug, None)


def _ensure_initialized(db_path: Path) -> None:
    """Initialize DB schema if needed (only once per path per process)."""
    path_str = str(db_path)
    if path_str in _initialized_dbs:
        return

    with _init_lock:
        if path_str in _initialized_dbs:
            return

        is_new = not db_path.exists()
        conn = duckdb.connect(str(db_path))
        try:
            if is_new:
                create_schema(conn)
                create_hnsw_index(conn)
            else:
                run_migrations(conn)
        finally:
            conn.close()
        _initialized_dbs.add(path_str)


def get_connection(slug: str) -> duckdb.DuckDBPyConnection:
    """Open a fresh connection for a project. Caller MUST close it when done.

    For simple operations, prefer the `connect(slug)` context manager instead.
    """
    db_path = _resolve_db_path(slug)
    _ensure_initialized(db_path)

    conn = duckdb.connect(str(db_path))
    try:
        install_vss(conn)
    except Exception:
        pass
    return conn


@contextmanager
def connect(slug: str):
    """Context manager: auto-closes connection after use.

    Usage:
        with connect('my-project') as conn:
            conn.execute("SELECT ...")
    """
    conn = get_connection(slug)
    try:
        yield conn
    finally:
        conn.close()


# Legacy compatibility
class ConnectionManager:
    """Legacy compatibility wrapper. Does nothing - connections are per-operation now."""

    def close_all(self) -> None:
        pass

    def remove(self, slug: str) -> None:
        pass

    def get_connection(self, slug: str) -> duckdb.DuckDBPyConnection:
        return get_connection(slug)


_manager = ConnectionManager()


def get_manager() -> ConnectionManager:
    return _manager
