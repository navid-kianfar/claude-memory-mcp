"""Connection-per-operation, over ONE long-lived database per file.

Every operation still gets a connection of its own and closes it when done, so
no connection is ever shared between threads. What changed (v0.11.9) is where
that connection comes from: a CURSOR of a database handle this process opens
once per file and keeps, instead of a fresh `duckdb.connect(path)` each time.

Why that is not cosmetic: `duckdb.connect(path)` + `close()` per operation
opened and tore down the whole database instance whenever no other connection
happened to be open. A second thread opening the same file while the first was
tearing it down raced DuckDB's instance cache. The daemon died of exactly that
26 times in a week (every macOS crash report: SIGSEGV inside `duckdb.connect`
on one thread while another thread was inside DuckDB - an asoode flush thread
against a tool worker), and the torn writes left stale PRIMARY KEY index entries
behind. A row with a stale entry then aborts the process on its next
delete+insert UPDATE ("INTERNAL Error: Failed to append to PRIMARY_tasks_0:
duplicate key"), launchd restarts it, the next write aborts it again - 57 times.
Reproduced outside the daemon: three sparse threads on per-op connect raise
"Unique file handle conflict ... already attached"; the same load on cursors of
one handle never does.

A cursor is a full connection of its own to the same instance, safe to use on
its own thread, and closing it never closes the database. The instance lives
until `release_database` / `close_all_databases` - called where a database file
is moved (portable mode) and at process exit.

The one exception is `transaction(slug)`: a block that must be all-or-nothing
publishes an ambient connection that every `connect(slug)` inside it joins, so
the connection-per-operation repositories take part in one transaction without
a single signature change. See that function for why it is shaped this way.
"""

import atexit
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
    """Drop cached DB paths. Call after a project's db_path changes (portable ops).

    Also closes the handle on the path being forgotten - the project now lives
    in another file, and a handle left on the old one would pin it open. With
    no slug, every handle is closed.
    """
    with _path_cache_lock:
        if slug is None:
            _path_cache.clear()
            stale = None
        else:
            stale = _path_cache.pop(slug, None)
    if slug is None:
        close_all_databases()
    elif stale is not None:
        release_database(stale)


# The open database per file, keyed by its resolved path. Guarded by
# `_databases_lock`, which is held for every open, close and `cursor()` call on
# a handle - the handle object itself is shared, only its cursors are not.
_databases: dict[str, duckdb.DuckDBPyConnection] = {}
_HNSW_PERSISTENCE = "SET hnsw_enable_experimental_persistence = true"
_databases_lock = threading.Lock()


def _database(db_path: Path) -> duckdb.DuckDBPyConnection:
    """The process's handle on `db_path`, opened (and migrated) on first use."""
    key = str(db_path)
    with _databases_lock:
        db = _databases.get(key)
        if db is None:
            _ensure_initialized(db_path)
            db = duckdb.connect(key)
            # INSTALL/LOAD once per database, here under the lock - NEVER per
            # operation. Two threads running `LOAD vss` on one instance at the
            # same moment segfault inside DuckDB: that is the crash in every
            # macOS report (always inside `connect()`, where install_vss ran on
            # every operation), reproduced in tests/test_db_lifecycle.py.
            with suppress(Exception):
                install_vss(db)
            # Also once per database: `disabled_optimizers` is a GLOBAL setting.
            # Set on every operation, it rewrote the instance's config while
            # other threads were planning queries - the second segfault path,
            # reproduced by tests/test_db_lifecycle.py's service-level stress.
            try:
                _disable_unsafe_optimizers(db)
            except Exception:
                db.close()
                raise
            _databases[key] = db
        return db


def _cursor(db_path: Path) -> duckdb.DuckDBPyConnection:
    db = _database(db_path)
    with _databases_lock:
        try:
            return db.cursor()
        except duckdb.ConnectionException:
            # Closed under us by a release on another thread; open it again.
            _databases.pop(str(db_path), None)
    return _cursor(db_path)


def release_database(path: str | Path) -> bool:
    """Close this process's handle on a database file. True if one was open.

    For a file about to be copied, moved or replaced: closing the last handle
    checkpoints the WAL into the file, so a copy taken after this is complete.
    Cursors still open elsewhere keep the instance alive until they close; the
    next operation opens the file afresh.
    """
    key = str(Path(path))
    with _databases_lock:
        db = _databases.pop(key, None)
        if db is None:
            return False
        with suppress(Exception):
            db.close()
    return True


def release_project_database(slug: str) -> bool:
    """`release_database` for a project's current file."""
    with _path_cache_lock:
        cached = _path_cache.get(slug)
    return release_database(cached or _compute_db_path(slug))


def close_all_databases() -> int:
    """Close every handle this process holds. Called at exit, and by tests."""
    with _databases_lock:
        handles = list(_databases.values())
        _databases.clear()
        for db in handles:
            with suppress(Exception):
                db.close()
    return len(handles)


atexit.register(close_all_databases)


def _ensure_initialized(db_path: Path) -> None:
    """Initialize DB schema if needed (only once per path per process).

    Called with `_databases_lock` held, before this process has a handle on the
    file, so the short-lived connection here is never a second instance racing
    a live one.
    """
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


# DuckDB optimizers that return WRONG results on this store's workload.
#
# `row_group_pruner` (DuckDB 1.5) answers `ORDER BY <column> ... LIMIT n` by
# reading only the row groups whose min/max statistics can hold the first n
# rows - and it counts rows that were already DELETED. A queue table is exactly
# the shape that breaks it: each flushed row is deleted, and a delete held open
# while another thread appends starts a new, tiny row group. Once the oldest
# groups are fully deleted, the pruner keeps them, discards the groups that hold
# the live rows, and the query returns nothing. On 2026-09-14 that made
# `OutboxRepository.pending()` read ZERO rows from an outbox holding 558, so the
# asoode mirror had silently stopped for three days with no error anywhere.
# Reproduced from scratch on duckdb 1.5.1 and 1.5.2; see
# tests/repositories/test_outbox_repository.py.
#
# Disabled on every project connection, so no `ORDER BY ... LIMIT` in any
# repository can hit it - not only the one that was caught. An optimizer this
# DuckDB does not have is skipped, because naming an unknown one is an error.
_UNSAFE_OPTIMIZERS = ("row_group_pruner",)
_disabled_optimizers_sql: str | None = None
_disabled_optimizers_lock = threading.Lock()


def _disable_unsafe_optimizers(conn: duckdb.DuckDBPyConnection) -> None:
    global _disabled_optimizers_sql
    if _disabled_optimizers_sql is None:
        with _disabled_optimizers_lock:
            if _disabled_optimizers_sql is None:
                try:
                    known = {row[0] for row in conn.execute(
                        "SELECT name FROM duckdb_optimizers()").fetchall()}
                except duckdb.Error:
                    known = set()
                names = [name for name in _UNSAFE_OPTIMIZERS if name in known]
                _disabled_optimizers_sql = (
                    f"SET disabled_optimizers = '{','.join(names)}'" if names else ""
                )
    if _disabled_optimizers_sql:
        conn.execute(_disabled_optimizers_sql)


def get_connection(slug: str) -> duckdb.DuckDBPyConnection:
    """Open a fresh connection for a project. Caller MUST close it when done.

    For simple operations, prefer the `connect(slug)` context manager instead.

    This deliberately does NOT join an open `transaction(slug)`: it hands
    ownership to the caller, who closes it (portable_service, project_service),
    and closing the transaction's connection out from under it mid-flight would
    be a new bug. Only `connect` joins.
    """
    db_path = _resolve_db_path(slug)

    try:
        conn = _cursor(db_path)
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
        # Per connection: this setting is session-scoped. The extension itself
        # was loaded once, when the handle opened - see _database.
        conn.execute(_HNSW_PERSISTENCE)
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
