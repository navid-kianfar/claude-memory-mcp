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

SELF-HEALING (v0.11.10). DuckDB 1.5 answers an internal error - a stale index
entry, "Failed to delete all rows from index" - by INVALIDATING the database:
that statement raises, and every later one on any cursor of that instance fails
with "database has been invalidated because of a previous fatal error". With
one long-lived handle per file that meant every call on the project failed
until someone restarted the daemon (kalagh, 2026-10-29). So a fatal error seen
by `connect` or `transaction` now heals the file: new cursors wait, the dead
handle is dropped once the cursors still on it close, the file is reopened
(the WAL replays only committed work), the stale tables are rebuilt by
db/repair.py, and the caller (server._safe) retries once. See `_heal`.
"""

import atexit
import contextvars
import logging
import re
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import duckdb

from memory_mcp.config import settings
from memory_mcp.db.schema import (
    create_schema, create_hnsw_index, install_vss, run_migrations,
)

log = logging.getLogger(__name__)

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
        # Under the same lock `_heal` takes to drop the dead handle, so no
        # thread can open the invalidated instance (still in DuckDB's cache
        # until the heal closes it) and leave it here to be served again.
        _wait_while_healing_locked(key)
        db = _databases.get(key)
        if db is None:
            if _shut_down:
                raise duckdb.ConnectionException(
                    f"{db_path.name}: the daemon is shutting down")
            _ensure_initialized(db_path)
            db = duckdb.connect(key)
            # INSTALL/LOAD once per database, here under the lock - NEVER per
            # operation. Two threads running `LOAD vss` on one instance at the
            # same moment segfault inside DuckDB: that is the crash in every
            # macOS report (always inside `connect()`, where install_vss ran on
            # every operation), reproduced in tests/test_db_lifecycle.py.
            with suppress(Exception):
                install_vss(db)
            # Before anything else can checkpoint - see _checkpoint_after_replay.
            _checkpoint_after_replay(db)
            # Also once per database: `disabled_optimizers` is a GLOBAL setting.
            # Set on every operation, it rewrote the instance's config while
            # other threads were planning queries - the second segfault path,
            # reproduced by tests/test_db_lifecycle.py's service-level stress.
            try:
                _disable_unsafe_optimizers(db)
                # DuckDB hands back a still-open INVALIDATED instance from its
                # cache while any connection to it lives (a heal whose drain
                # timed out, an untracked cursor). Never keep one: fail this
                # call, and the next one opens again.
                db.execute("SELECT 1").fetchone()
            except Exception:
                with suppress(Exception):
                    db.close()
                raise
            _databases[key] = db
        return db


def _checkpoint_after_replay(conn: duckdb.DuckDBPyConnection) -> None:
    """CHECKPOINT a file just opened, before anything else can checkpoint it.

    THE ROOT CAUSE of the stale indexes (2026-10-29, upstream duckdb/duckdb#26106,
    also #26828; reproduced on 1.5.1 by tests/test_db_crash_stress.py): rows a
    WAL replay restores are dropped from every SECONDARY ART index by the first
    IMPLICIT checkpoint after that replay - the one on close, or the automatic
    one at wal_autocheckpoint. The rows stay in the table, the broken index is
    persisted, and the next DELETE or delete+insert UPDATE of such a row fails
    with "Failed to delete all rows from index" and invalidates the database.
    One SIGKILL with a non-empty WAL was enough: the next start opened the file
    (replay), migrated it and closed it - and that close was the checkpoint.
    An explicit CHECKPOINT first does not lose them: 0 stale of 8 kill runs,
    against 8 of 8 without it. Busy or failing is fine - it only has to come
    before an implicit one.
    """
    with suppress(Exception):
        conn.execute("CHECKPOINT")


def _cursor(db_path: Path, tracked: bool = False) -> duckdb.DuckDBPyConnection:
    db = _database(db_path)
    key = str(db_path)
    with _databases_lock:
        try:
            cursor = db.cursor()
        except duckdb.ConnectionException:
            # Closed under us by a release on another thread; open it again.
            if _databases.get(key) is db:
                _databases.pop(key, None)
            cursor = None
        else:
            if tracked:
                # Counted in the same critical section that made it, so a
                # drain can never miss a cursor that exists.
                _live[key] = _live.get(key, 0) + 1
            return cursor
    return _cursor(db_path, tracked)


def release_database(path: str | Path) -> bool:
    """Close this process's handle on a database file. True if one was open.

    For a file about to be copied, moved or replaced: closing the last handle
    checkpoints the WAL into the file, so a copy taken after this is complete.
    Cursors still open elsewhere keep the instance alive until they close; the
    next operation opens the file afresh.
    """
    key = str(Path(path))
    with _databases_lock:
        if key not in _databases:
            return False
    if not _quiesce_and_close([key], CLOSE_DRAIN_SECONDS, leave_busy=True):
        # Never closed under a running query: the caller (a portable move)
        # must not copy a file that is still being written either.
        raise RuntimeError(
            f"{Path(path).name} is busy - operations are still running on it; try again")
    return True


def release_project_database(slug: str) -> bool:
    """`release_database` for a project's current file."""
    with _path_cache_lock:
        cached = _path_cache.get(slug)
    return release_database(cached or _compute_db_path(slug))


def close_all_databases() -> int:
    """Close every handle this process holds. Called by tests and portable ops;
    the process exit goes through `shutdown_databases`."""
    with _databases_lock:
        keys = list(_databases)
    return _quiesce_and_close(keys, CLOSE_DRAIN_SECONDS)


#: How long a close waits for the operations still running on a handle.
CLOSE_DRAIN_SECONDS = 4.0
_shut_down = False


def _quiesce_and_close(
    keys: list[str], timeout: float, leave_busy: bool = False,
) -> int:
    """Close the handles on `keys`: new operations wait, running ones finish,
    then each handle is CHECKPOINTed and closed. Returns how many were closed.

    Closing a handle while another thread is inside DuckDB on a cursor of it
    is what wedged the daemon at exit (tests/test_db_crash_stress.py): a thread
    blocked in `duckdb.connect` holding `_databases_lock`, the rest blocked in
    `execute` on the destroyed instance, the process never exiting - so launchd
    SIGKILLed it with a WAL full of work, which is the input the stale-index
    bug needs. The CHECKPOINT first leaves an empty WAL even if what follows
    the close is a kill. `leave_busy` (the exit path) leaves a handle that did
    not drain open rather than tear it down under a running query; the next
    start replays its WAL, and `_checkpoint_after_replay` keeps that safe.
    """
    closed = 0
    with _databases_lock:
        paused = [key for key in keys if key not in _healing]
        _healing.update(paused)
        try:
            drained = _heal_done.wait_for(
                lambda: all(_live.get(key, 0) == 0 for key in keys), timeout,
            )
            for key in keys:
                busy = _live.get(key, 0) > 0
                if busy and leave_busy:
                    log.warning(
                        "memory-mcp: %s still has %d operation(s) running; left "
                        "open rather than closed under them", key, _live[key])
                    if _shut_down:
                        # Exiting anyway: FORCE aborts what is running, so the
                        # WAL is empty for whatever ends this process.
                        db = _databases.get(key)
                        if db is not None:
                            with suppress(Exception):
                                db.execute("FORCE CHECKPOINT")
                    continue
                db = _databases.pop(key, None)
                if db is None:
                    continue
                if drained or not busy:
                    with suppress(Exception):
                        db.execute("CHECKPOINT")
                with suppress(Exception):
                    db.close()
                closed += 1
        finally:
            _healing.difference_update(paused)
            _heal_done.notify_all()
    return closed


def shutdown_databases(timeout: float = CLOSE_DRAIN_SECONDS) -> int:
    """The process is exiting: refuse new handles, drain, checkpoint, close.

    Called by the daemon's lifespan once its background threads are stopped,
    and by atexit for every other process. Idempotent."""
    global _shut_down
    with _databases_lock:
        _shut_down = True
        keys = list(_databases)
    return _quiesce_and_close(keys, timeout, leave_busy=True)


atexit.register(shutdown_databases)


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
            if not is_new:
                # Opening an existing file replays its WAL, and this
                # connection's close would be the first checkpoint after it.
                with suppress(Exception):
                    install_vss(conn)
                _checkpoint_after_replay(conn)
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


# ---------- self-healing after a fatal error ----------
#
# `_live` counts the cursors `connect` and `transaction` have open per file, so
# a heal can wait for every cursor on the dead instance to close: DuckDB keeps
# an instance alive while any connection to it is open, and `duckdb.connect`
# hands back that SAME invalidated instance from its cache until then. Guarded
# by `_databases_lock`; `_heal_done` is signalled whenever a count reaches zero
# or a heal ends.
_live: dict[str, int] = {}
_healing: set[str] = set()
# Invalidated handles a heal could not close yet (operations still on them),
# kept out of `_databases` so they are never served; the next heal closes them.
_graveyard: dict[str, duckdb.DuckDBPyConnection] = {}
_heal_done = threading.Condition(_databases_lock)
_heal_thread = threading.local()

#: How long a heal waits for open cursors on the dead handle to close, and how
#: long a new operation waits for a heal (the probe copies the file per table).
HEAL_DRAIN_SECONDS = 30.0
HEAL_WAIT_SECONDS = 300.0

_INVALIDATED = "database has been invalidated"


def is_fatal(exc: BaseException | None) -> bool:
    """True when `exc` (or anything in its cause/context chain) means DuckDB
    has invalidated the database: a FatalException, an InternalException (DuckDB
    invalidates the instance on every internal error), or the "invalidated"
    message a later statement gets. A wrapped one counts - PlanError chains it."""
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, (duckdb.FatalException, duckdb.InternalException)):
            return True
        # By message only for a DuckDB error, and only on its first line: a
        # constraint error echoes row values, and a stored text that happens
        # to contain the phrase must not set off a heal.
        if (
            isinstance(exc, duckdb.Error)
            and not isinstance(exc, duckdb.ConstraintException)
            and _INVALIDATED in (str(exc).splitlines() or [""])[0]
        ):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def _table_hint(message: str) -> str | None:
    """The index or table a DuckDB internal error names, for the log line:
    'PRIMARY_tasks_0' -> 'tasks', 'idx_task_plans_hash' -> that index."""
    match = re.search(r"PRIMARY_([A-Za-z0-9_]+?)_\d+", message)
    if match:
        return match.group(1)
    match = re.search(r"\b(idx_[A-Za-z0-9_]+)", message)
    return match.group(1) if match else None


def _track(key: str, delta: int) -> None:
    with _databases_lock:
        count = _live.get(key, 0) + delta
        if count <= 0:
            _live.pop(key, None)
            _heal_done.notify_all()
        else:
            _live[key] = count


def _wait_while_healing_locked(key: str) -> None:
    """Block while `key` is being healed. Caller holds `_databases_lock`."""
    if getattr(_heal_thread, "key", None) == key:
        return
    deadline = time.monotonic() + HEAL_WAIT_SECONDS
    while key in _healing:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.error("memory-mcp: gave up waiting for the heal of %s", key)
            return
        _heal_done.wait(remaining)


def _heal(slug: str, db_path: Path, exc: BaseException) -> dict | None:
    """Replace the invalidated handle on `db_path` and repair the file.

    Runs on the thread that saw the fatal error, after that thread closed its
    own cursor. A second thread failing on the same instance waits for the first
    heal instead of starting another; a fatal error DURING a heal is reported by
    the heal, never healed recursively. Never raises: the caller is already
    raising the error that got it here.
    """
    key = str(db_path)
    if getattr(_heal_thread, "key", None) is not None:
        return None
    with _databases_lock:
        if key in _healing:
            # Someone else is on it; wait for them, then let the caller retry.
            _heal_done.wait_for(lambda: key not in _healing, HEAL_WAIT_SECONDS)
            return None
        _healing.add(key)
        # A handle a busy heal set aside is the dead one, never served again.
        dead = _databases.pop(key, None) or _graveyard.pop(key, None)
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    hint = _table_hint(str(exc))
    log.critical(
        "memory-mcp: DATABASE INVALIDATED for project %r (%s)%s: %s: %s - "
        "reopening the file and repairing it",
        slug, db_path.name, f" on {hint}" if hint else "", type(exc).__name__, message,
    )
    _heal_thread.key = key
    result: dict | None = None
    try:
        with suppress(Exception):
            from memory_mcp.db.repair import record_fatal

            record_fatal(slug, f"{type(exc).__name__}: {message}", hint)
        with _databases_lock:
            drained = _heal_done.wait_for(
                lambda: _live.get(key, 0) == 0, HEAL_DRAIN_SECONDS,
            )
        if not drained:
            # Not reopened: the dead instance would come straight back from
            # DuckDB's cache. Operations fail fast until the last cursor on
            # it closes; the next fatal error they see heals again.
            log.critical(
                "memory-mcp: %d operation(s) on %s still running on the dead "
                "handle after %.0fs - NOT reopening yet; the next call retries the heal",
                _live.get(key, 0), db_path.name, HEAL_DRAIN_SECONDS,
            )
            if dead is not None:
                with _databases_lock:
                    _graveyard[key] = dead
            return {"project": slug, "status": "busy"}
        if dead is not None:
            with suppress(Exception):
                dead.close()
        try:
            from memory_mcp.db.repair import repair_project

            result = repair_project(slug)
        except Exception as repair_error:  # noqa: BLE001 - reported, not raised
            log.critical(
                "memory-mcp: repair of %r after the fatal error FAILED: %s: %s",
                slug, type(repair_error).__name__, repair_error,
            )
            result = {"project": slug, "status": "error", "error": str(repair_error)}
        rebuilt = [r["table"] for r in result.get("rebuilt") or []]
        stale = [f["table"] for f in result.get("stale") or []]
        log.critical(
            "memory-mcp: heal of %r finished: %s; stale tables %s; rebuilt %s",
            slug, result.get("status"), stale or "none", rebuilt or "none",
        )
        return result
    finally:
        _heal_thread.key = None
        with _databases_lock:
            _healing.discard(key)
            _heal_done.notify_all()


@contextmanager
def paused(db_path: Path, timeout: float = HEAL_DRAIN_SECONDS):
    """Hold every other operation on `db_path` off for the block: new cursors
    wait, running ones are drained first. The calling thread keeps working.

    For a table rebuild (db/repair.py): its copy-drop-rename is one transaction
    reading its own snapshot, so a row another connection committed meanwhile
    would be silently dropped - DuckDB raises no conflict for it. Already
    paused (inside a heal) on this thread, it just runs the block. Raises if
    the file does not drain in time; nothing has been touched then.
    """
    key = str(db_path)
    if getattr(_heal_thread, "key", None) == key:
        yield
        return
    with _databases_lock:
        _wait_while_healing_locked(key)
        _healing.add(key)
        drained = _heal_done.wait_for(lambda: _live.get(key, 0) == 0, timeout)
        if not drained:
            _healing.discard(key)
            _heal_done.notify_all()
            raise RuntimeError(
                f"{db_path.name}: {_live.get(key, 0)} operation(s) still running; "
                "not pausing it for a rebuild")
    _heal_thread.key = key
    try:
        yield
    finally:
        _heal_thread.key = None
        with _databases_lock:
            _healing.discard(key)
            _heal_done.notify_all()


def get_connection(
    slug: str, _tracked: bool = False, _db_path: Path | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open a fresh connection for a project. Caller MUST close it when done.

    For simple operations, prefer the `connect(slug)` context manager instead.

    This deliberately does NOT join an open `transaction(slug)`: it hands
    ownership to the caller, who closes it (portable_service, project_service),
    and closing the transaction's connection out from under it mid-flight would
    be a new bug. Only `connect` joins.
    """
    # `connect`/`transaction` pass the path they resolved, so the cursor they
    # count is counted against the file they will uncount.
    db_path = _db_path or _resolve_db_path(slug)

    try:
        conn = _cursor(db_path, _tracked)
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

    db_path = _resolve_db_path(slug)
    conn = get_connection(slug, _tracked=True, _db_path=db_path)
    key = str(db_path)
    fatal: BaseException | None = None
    try:
        yield conn
    except BaseException as e:
        if is_fatal(e):
            fatal = e
        raise
    finally:
        with suppress(Exception):
            conn.close()
        _track(key, -1)
        if fatal is not None:
            _heal(slug, db_path, fatal)


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

    db_path = _resolve_db_path(slug)
    conn = get_connection(slug, _tracked=True, _db_path=db_path)
    key = str(db_path)
    fatal: BaseException | None = None
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
        except BaseException as e:
            # Guarded: the error that got us here is the one worth raising, and
            # a ROLLBACK failure would otherwise replace it with noise. On an
            # invalidated database the ROLLBACK fails too, and that is fine:
            # nothing uncommitted ever reaches the WAL, so the reopen drops it.
            if is_fatal(e):
                fatal = e
            with suppress(Exception):
                conn.execute("ROLLBACK")
            raise
    except BaseException as e:
        # BEGIN itself can be the statement that finds the database invalidated.
        if is_fatal(e):
            fatal = e
        raise
    finally:
        _after_commit_hooks.reset(hooks_token)
        _ambient_conns.reset(ambient_token)
        with suppress(Exception):
            conn.close()
        _track(key, -1)
        if fatal is not None:
            _heal(slug, db_path, fatal)

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
