"""Connection-per-operation pattern. No caching, no locks, no conflicts.

DuckDB connection open is <1ms, so there's zero performance penalty.
Each operation opens a connection, does work, closes immediately.
This eliminates ALL lock conflicts between projects, instances, and processes.

The one exception is `transaction(slug)`: a block that must be all-or-nothing
publishes an ambient connection that every `connect(slug)` inside it joins, so
the connection-per-operation repositories take part in one transaction without
a single signature change. See that function for why it is shaped this way.
"""

import contextvars
import threading
from contextlib import contextmanager, suppress
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

# The open transaction's connection, per project slug.
#
# A contextvar rather than a thread-local or a module global, and that choice is
# load-bearing: a contextvar is per-thread AND per-async-task, and a NEW thread
# starts from an empty context (verified, not assumed). Container._mirror_soon
# spawns a daemon thread on every task mutation, a DuckDB connection is not safe
# to share across threads, and that thread must therefore never be able to pick
# ours up. Precedent for the pattern: context._request_user.
_ambient_conns: contextvars.ContextVar[
    dict[str, duckdb.DuckDBPyConnection] | None
] = contextvars.ContextVar("_ambient_conns", default=None)

# Side effects deferred to after the commit. Present (a list) only while a
# transaction is open, which is how `after_commit` tells the caller whether it
# deferred anything.
_after_commit_hooks: contextvars.ContextVar[
    list[tuple[object, object]] | None
] = contextvars.ContextVar("_after_commit_hooks", default=None)


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

    This deliberately does NOT join an open `transaction(slug)`: it hands
    ownership to the caller, who closes it (portable_service, project_service),
    and closing the transaction's connection out from under it mid-flight would
    be a new bug. Only `connect` joins.
    """
    db_path = _resolve_db_path(slug)
    _ensure_initialized(db_path)

    try:
        conn = duckdb.connect(str(db_path))
    except duckdb.IOException as e:
        # DuckDB is single-writer per file across processes, so this is almost
        # always the daemon rather than real disk trouble. Its own message names
        # a PID and nothing else, which sends people looking for a hung process.
        from memory_mcp.daemon_client import lock_message

        hint = lock_message(e)
        if hint is None:
            raise
        raise duckdb.IOException(f"{db_path.name}: {hint}") from e
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

    Inside `transaction(slug)` this yields that transaction's connection and does
    NOT close it - the transaction owns it. That is what lets a repository keep
    its `with connect(project)` and still take part in an atomic operation.
    """
    ambient = _ambient_conns.get()
    if ambient is not None:
        joined = ambient.get(slug)
        if joined is not None:
            yield joined
            return

    conn = get_connection(slug)
    try:
        yield conn
    finally:
        conn.close()


def in_transaction(slug: str) -> bool:
    """True when this context has an open `transaction(slug)`."""
    ambient = _ambient_conns.get()
    return ambient is not None and slug in ambient


def after_commit(fn, key=None) -> bool:
    """Defer `fn` until the open transaction commits. False if there is none.

    For a side effect a ROLLBACK cannot undo - an HTTP call, a spawned thread,
    a file write. False means "no transaction, run it yourself now".

    `key` de-duplicates: registering the same key twice runs it once, so a
    nine-task plan nudges the mirror once rather than nine times.
    """
    hooks = _after_commit_hooks.get()
    if hooks is None:
        return False
    if key is not None and any(existing == key for existing, _ in hooks):
        return True
    hooks.append((key, fn))
    return True


@contextmanager
def transaction(slug: str):
    """Run a block as ONE DuckDB transaction. Every `connect(slug)` inside joins.

    The point is an operation that must not be half-applied: `memory_task_plan`
    creating four of nine tasks and failing left a decomposition that reads like
    a considered plan when it is the first fragment of one.

    Why the ambient connection rather than threading a `conn=` parameter through
    the repositories: `connect` is the single chokepoint every task write already
    goes through, so this makes 40+ call sites transactional at once and needs no
    change to a public repository signature.

    Re-entrant on purpose: DuckDB raises TransactionException "cannot start a
    transaction within a transaction" on a nested BEGIN, so a second
    `transaction(slug)` inside the first joins it instead of opening one.

    Two things a caller must know:
    - A ROLLBACK undoes local rows and NOTHING else. Anything remote or threaded
      belongs in `after_commit`, not in the block.
    - A failed statement does not poison the transaction (verified on duckdb
      1.5.1), so a swallowed error inside the block leaves it usable.
    """
    if in_transaction(slug):
        yield _ambient_conns.get()[slug]
        return

    conn = get_connection(slug)
    ambient = dict(_ambient_conns.get() or {})
    ambient[slug] = conn
    ambient_token = _ambient_conns.set(ambient)
    hooks: list[tuple[object, object]] = []
    hooks_token = _after_commit_hooks.set(hooks)
    committed = False

    try:
        conn.execute("BEGIN TRANSACTION")
        try:
            yield conn
            conn.execute("COMMIT")
            committed = True
        except BaseException:
            # Guarded: the error that got us here is the one worth raising, and
            # a ROLLBACK failure would otherwise replace it with noise.
            with suppress(Exception):
                conn.execute("ROLLBACK")
            raise
    finally:
        _after_commit_hooks.reset(hooks_token)
        _ambient_conns.reset(ambient_token)
        conn.close()

    if committed:
        for _key, hook in hooks:
            # The rows are already durable. A deferred side effect that fails is
            # a mirror that has not run yet, never a reason to fail the write
            # that has already happened.
            with suppress(Exception):
                hook()


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
