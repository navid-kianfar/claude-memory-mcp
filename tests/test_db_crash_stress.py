"""What actually leaves stale ART index entries behind: a kill, then a checkpoint.

v0.11.9 blamed the per-operation `duckdb.connect`/`close` race and moved to one
long-lived handle per file. The file went stale again after that, so the race was
not the whole story - and these tests say what is.

The mechanism is upstream, not ours (duckdb/duckdb#26106, #26828, #23788): rows
restored by WAL REPLAY are dropped from every SECONDARY ART index by the FIRST
checkpoint after that replay - the one on close, or the automatic one at
`wal_autocheckpoint`. The rows stay in the table and a scan returns them; the
index no longer knows them, the bad index is persisted, and the next delete or
delete+insert UPDATE of such a row raises "Failed to delete all rows from index"
and invalidates the database. An EXPLICIT `CHECKPOINT` run before anything else
can checkpoint persists the index correctly - that is the only known fix, and
`test_explicit_checkpoint_after_replay_keeps_indexes_consistent` is what pins it.

PRIMARY KEY indexes alone do not reproduce it, which matters for reading
`repair --check`: every table it flagged in production (memories, provenance,
task_comments, task_time_entries) carries a secondary `CREATE INDEX` as well,
and the probe names the table's first indexed column - the primary key - whoever
the stale entry actually belongs to.

So the daemon only needs to be killed once with a non-empty WAL: launchd's
SIGKILL after the SIGTERM grace, or a FatalException abort. The next process
replays the WAL, and the open/close inside `_ensure_initialized` (migrations on
an existing file) is itself the first checkpoint after that replay.

Two tests here FAIL until that checkpoint and a quiesced shutdown land - they
are the reproduction, and each says in its assertion what to change. The
`TestUpstreamMechanism` cases xfail on the upstream bug itself, so they turn
green the day DuckDB fixes it rather than failing forever.

The default run is a short one. `MEMORY_MCP_STRESS=1` turns it into a soak.
"""

import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import duckdb
import pytest

import memory_mcp.db.connection as conn_mod
from memory_mcp.config import settings
from memory_mcp.db.connection import close_all_databases, connect

SOAK = os.environ.get("MEMORY_MCP_STRESS") == "1"
KILL_RUNS = 8 if SOAK else 2
WRITE_SECONDS = 1.5 if SOAK else 0.6

# The tables the stress writes, and the only ones probed: a probe of every
# indexed table in the schema copies the file once per table and costs more than
# the rest of this module together.
PROBED = ("stress", "task_comments")


# ---------------------------------------------------------------- the probe
#
# A local copy of what db/repair.py's probe does, on purpose: this test must keep
# working while repair.py's own API is being rewritten. Every row of a table is
# rewritten to itself (DuckDB runs that as a delete plus an insert in every index
# over it) and then deleted, in one transaction - the commit is where a stale
# index entry aborts. In a throwaway child, on a throwaway copy, because the
# failure is a FatalException that takes the whole process with it.

_PROBE = (
    "import duckdb, sys\n"
    "c = duckdb.connect(sys.argv[1])\n"
    "try:\n"
    "    c.execute('LOAD vss')\n"
    "    c.execute('SET hnsw_enable_experimental_persistence = true')\n"
    "except Exception:\n"
    "    pass\n"
    "t = sys.argv[2]\n"
    "sets = ', '.join(f'\"{col}\" = \"{col}\"' for col in sys.argv[3:])\n"
    "c.execute('BEGIN TRANSACTION')\n"
    "c.execute(f'UPDATE \"{t}\" SET {sets}')\n"
    "c.execute(f'DELETE FROM \"{t}\"')\n"
    "c.execute('COMMIT')\n"
    "print('ok')\n"
)


def _copy_with_wal(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)
    wal = Path(f"{src}.wal")
    if wal.exists():
        shutil.copyfile(wal, Path(f"{dst}.wal"))
    else:
        Path(f"{dst}.wal").unlink(missing_ok=True)


def _indexed_columns(db_path: Path, tables: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """Every column of `tables` that carries an ART index: a PRIMARY KEY or
    UNIQUE constraint of any width, or a plain CREATE INDEX. HNSW is skipped -
    it is not an ART index and cannot be rewritten in place."""
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        known = {
            (table, column)
            for table, column in conn.execute(
                "SELECT table_name, column_name FROM duckdb_columns() "
                "WHERE schema_name = 'main'"
            ).fetchall()
        }
        found: dict[str, list[str]] = {}

        def add(table: str, columns: list[str]) -> None:
            if table not in tables:
                return
            bucket = found.setdefault(table, [])
            for column in columns:
                if (table, column) in known and column not in bucket:
                    bucket.append(column)

        for table, columns in conn.execute(
            "SELECT table_name, constraint_column_names FROM duckdb_constraints() "
            "WHERE constraint_type IN ('PRIMARY KEY', 'UNIQUE') AND schema_name = 'main'"
        ).fetchall():
            add(table, list(columns))
        for table, expressions, sql in conn.execute(
            "SELECT table_name, expressions, sql FROM duckdb_indexes() "
            "WHERE schema_name = 'main'"
        ).fetchall():
            if sql and "USING HNSW" in sql.upper():
                continue
            add(table, _expression_columns(expressions))
    finally:
        conn.close()
    return {table: tuple(columns) for table, columns in found.items() if columns}


def _expression_columns(expressions) -> list[str]:
    """`duckdb_indexes().expressions` as bare column names. duckdb 1.5 reports
    it as the string '[task_id, sha256]'; anything that is not an identifier
    comes back as itself and the caller's column check drops it."""
    if expressions is None:
        return []
    if isinstance(expressions, str):
        expressions = (
            expressions.strip().removeprefix("[").removesuffix("]").split(",")
        )
    return [str(e).strip().strip('"') for e in expressions if str(e).strip()]


def stale_indexes(db_path: Path, tables: tuple[str, ...] = PROBED) -> list[dict]:
    """The tables in `db_path` whose indexes abort a delete+insert of every row.

    Never touches `db_path` beyond copying it (with its WAL, so writes that were
    never checkpointed are part of what is probed).
    """
    findings: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="crash-stress-probe-") as work:
        source = Path(work) / "source.duckdb"
        _copy_with_wal(db_path, source)
        for table, columns in _indexed_columns(source, tables).items():
            target = Path(work) / "probe.duckdb"
            _copy_with_wal(source, target)
            result = subprocess.run(
                [sys.executable, "-I", "-c", _PROBE, str(target), table, *columns],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0 or "ok" not in result.stdout:
                lines = (result.stderr or result.stdout).strip().splitlines()
                error = next(
                    (line for line in lines if "Error" in line),
                    lines[-1] if lines else "",
                )
                findings.append({
                    "table": table,
                    "columns": columns,
                    "returncode": result.returncode,
                    "error": error[:200],
                })
            for leftover in (target, Path(f"{target}.wal")):
                leftover.unlink(missing_ok=True)
    return findings


# ------------------------------------------------------------- the writers
#
# A child process, because the point is to kill it, and because a stale index
# aborts the process rather than raising. It writes the way the repositories do:
# `with connect(slug)` per operation - a cursor of the one handle this process
# holds - from several threads at once, inserting, deleting, and running the
# delete+insert UPDATEs (an indexed column, a list column) that a stale entry
# trips over. One thread does nothing but CHECKPOINT, so a kill lands inside one.

_WRITER = '''
import faulthandler, os, random, sys, threading, time, uuid

from memory_mcp.db.connection import close_all_databases, connect

slug, seconds, mode = sys.argv[1], float(sys.argv[2]), sys.argv[3]

# A table of our own with the shape that breaks - PRIMARY KEY, two secondary
# indexes, a list column - beside task_comments, which is the shipped schema's
# version of the same shape (id PRIMARY KEY, idx_task_comments_task).
with connect(slug) as setup:
    setup.execute(
        "CREATE TABLE IF NOT EXISTS stress ("
        "id VARCHAR PRIMARY KEY, bucket VARCHAR, n INTEGER, tags VARCHAR[])"
    )
    setup.execute("CREATE INDEX IF NOT EXISTS idx_stress_bucket ON stress (bucket)")
    setup.execute("CREATE INDEX IF NOT EXISTS idx_stress_n ON stress (n)")
    setup.execute("CHECKPOINT")

stop = threading.Event()


def writer(seed):
    rng = random.Random(seed)
    mine = []
    while not stop.is_set():
        try:
            with connect(slug) as conn:
                roll = rng.random()
                if roll < 0.4 or len(mine) < 4:
                    key = uuid.uuid4().hex
                    bucket = f"b{rng.randrange(8)}"
                    conn.execute(
                        "INSERT INTO stress VALUES (?, ?, ?, ?)",
                        [key, bucket, rng.randrange(64), [bucket]],
                    )
                    conn.execute(
                        "INSERT INTO task_comments (id, task_id, body) VALUES (?, ?, ?)",
                        [key, f"task{rng.randrange(8)}", "stress"],
                    )
                    mine.append(key)
                elif roll < 0.6:
                    conn.execute(
                        "UPDATE stress SET bucket = ?, n = ? WHERE id = ?",
                        [f"b{rng.randrange(8)}", rng.randrange(64), rng.choice(mine)],
                    )
                elif roll < 0.75:
                    conn.execute(
                        "UPDATE stress SET tags = list_append(tags, ?) WHERE id = ?",
                        [uuid.uuid4().hex[:4], rng.choice(mine)],
                    )
                elif roll < 0.85:
                    conn.execute(
                        "UPDATE task_comments SET task_id = ? WHERE id = ?",
                        [f"task{rng.randrange(8)}", rng.choice(mine)],
                    )
                else:
                    key = mine.pop(rng.randrange(len(mine)))
                    conn.execute("DELETE FROM stress WHERE id = ?", [key])
                    conn.execute("DELETE FROM task_comments WHERE id = ?", [key])
        except Exception:
            pass
        time.sleep(rng.random() * 0.002)


def checkpointer():
    while not stop.is_set():
        try:
            with connect(slug) as conn:
                conn.execute("CHECKPOINT")
        except Exception:
            pass
        time.sleep(0.02)


threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
if mode != "no-checkpoint":
    threads.append(threading.Thread(target=checkpointer))
for thread in threads:
    thread.start()

print("writing", flush=True)

if mode == "killed":
    # Wait to be killed. The parent picks the moment.
    time.sleep(seconds + 60)
elif mode == "close-under-threads":
    # The atexit path: close every handle while the writers are inside DuckDB.
    # The watchdog dumps every thread's stack and exits non-zero if this wedges,
    # so the parent gets the deadlock rather than a timeout with nothing in it.
    time.sleep(seconds)
    faulthandler.dump_traceback_later(8, exit=True)
    close_all_databases()
    time.sleep(0.5)
    stop.set()
    # One shared settling window rather than a timeout per thread: five stuck
    # threads joined in turn would outlast the watchdog.
    deadline = time.monotonic() + 2.5
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    stuck = [thread.name for thread in threads if thread.is_alive()]
    faulthandler.cancel_dump_traceback_later()
    if stuck:
        print(f"STUCK {stuck}", flush=True)
        os._exit(3)
    print("CLOSED-CLEAN", flush=True)
    os._exit(0)
else:
    time.sleep(seconds)
    stop.set()
    for thread in threads:
        thread.join(timeout=5)
    close_all_databases()
    print("EXITED-CLEAN", flush=True)
'''


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["MEMORY_MCP_DATA_DIR"] = str(settings.data_dir)
    env["MEMORY_MCP_ASOODE_AUTO_MIRROR"] = "0"
    return env


def _spawn_writers(slug: str, seconds: float, mode: str) -> subprocess.Popen:
    child = subprocess.Popen(
        [sys.executable, "-c", _WRITER, slug, str(seconds), mode],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_child_env(),
    )
    # "writing" means the schema exists and the threads are running, so the kill
    # lands on real work rather than on an import.
    line = child.stdout.readline()
    assert "writing" in line, (line, child.poll())
    return child


def _db_path(slug: str) -> Path:
    return settings.projects_dir / f"{slug}.duckdb"


def _reopen_as_the_daemon_does(slug: str) -> None:
    """Open the file the way a restarted daemon does, and close it the way exit
    does: `_ensure_initialized` connects to the existing file (replaying its
    WAL), runs migrations and closes - and that close is a checkpoint - then the
    long-lived handle opens, serves, and is closed by atexit."""
    conn_mod._initialized_dbs.clear()
    conn_mod.invalidate_path_cache()
    with connect(slug) as conn:
        conn.execute("SELECT count(*) FROM stress").fetchone()
    close_all_databases()


def _checkpoint_before_anything_else(slug: str) -> None:
    """The fix under test: an explicit CHECKPOINT on the freshly opened file,
    before any other checkpoint can happen."""
    conn = duckdb.connect(str(_db_path(slug)))
    try:
        conn.execute("CHECKPOINT")
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _no_handles_left_open():
    """The child is the single writer of its file while it runs, and DuckDB's
    file lock is per process - a handle left open here would lock it out."""
    close_all_databases()
    yield
    close_all_databases()


def _run_kill_cycle(slug: str, rng: random.Random, repair_first: bool) -> list[dict]:
    child = _spawn_writers(slug, WRITE_SECONDS, "killed")
    time.sleep(rng.uniform(0.2, WRITE_SECONDS))
    child.kill()  # SIGKILL: no checkpoint, a WAL left behind, possibly mid-write
    child.wait(timeout=30)
    assert child.returncode == -signal.SIGKILL, child.returncode
    if repair_first:
        _checkpoint_before_anything_else(slug)
    _reopen_as_the_daemon_does(slug)
    return stale_indexes(_db_path(slug))


class TestCrashThenCheckpoint:
    def test_kill_9_then_a_restart_leaves_indexes_consistent(self):
        """SIGKILL mid-write, then the daemon's own reopen. Every index of every
        table written must still be able to delete the rows it indexes.

        This is the production sequence, and it is the one that reproduces."""
        rng = random.Random(20261029)
        stale = []
        for run in range(KILL_RUNS):
            findings = _run_kill_cycle(f"crash{run}", rng, repair_first=False)
            if findings:
                stale.append({"run": run, "findings": findings})
        assert not stale, (
            f"{len(stale)} of {KILL_RUNS} runs left stale index entries. "
            "Fix: CHECKPOINT the file in db/connection.py the moment it is "
            "opened, before migrations and before any other checkpoint can run "
            f"- see the module docstring. {stale}"
        )

    def test_explicit_checkpoint_after_replay_keeps_indexes_consistent(self):
        """The same kill, with one explicit CHECKPOINT on the reopened file
        before anything else can checkpoint. duckdb/duckdb#26106: the explicit
        checkpoint persists the replayed rows' index entries correctly, the
        shutdown and automatic ones drop them."""
        rng = random.Random(20261030)
        stale = []
        for run in range(KILL_RUNS):
            findings = _run_kill_cycle(f"healed{run}", rng, repair_first=True)
            if findings:
                stale.append({"run": run, "findings": findings})
        assert not stale, (
            f"a checkpoint-first reopen still left stale entries in "
            f"{len(stale)} of {KILL_RUNS} runs: {stale}"
        )

    def test_concurrent_writers_without_a_kill_stay_consistent(self):
        """The control that rules out concurrency on its own: the same threads,
        the same checkpoint loop, a clean exit. Nothing goes stale, so it is the
        kill-then-checkpoint sequence that does it, not the writing."""
        child = _spawn_writers("clean", WRITE_SECONDS, "clean")
        stdout, stderr = child.communicate(timeout=120)
        assert child.returncode == 0 and "EXITED-CLEAN" in stdout, (
            child.returncode, stderr[-2000:])
        _reopen_as_the_daemon_does("clean")
        assert stale_indexes(_db_path("clean")) == []


class TestShutdownUnderLoad:
    def test_close_all_databases_under_live_writers_does_not_wedge(self):
        """The atexit path, and the reason the daemon gets SIGKILLed at all.

        `close_all_databases` tears the instance down while daemon threads (the
        asoode flusher, the outbox sweeper, the time backfill) are inside
        DuckDB on cursors of it. The threads that survive that then deadlock:
        one blocks in `duckdb.connect` reopening the file WHILE HOLDING
        `_databases_lock`, the rest queue behind it, and the process never
        exits. launchd then SIGKILLs it after the SIGTERM grace - with a WAL
        that was never checkpointed, which is precisely the input the
        stale-index bug needs.

        A child process, so a wedge or a segfault fails this test rather than
        hanging pytest; its stderr carries every thread's stack.
        """
        attempts = 5 if SOAK else 2
        wedged = []
        for run in range(attempts):
            child = _spawn_writers(f"closing{run}", 0.4, "close-under-threads")
            stdout, stderr = child.communicate(timeout=120)
            if child.returncode != 0 or "CLOSED-CLEAN" not in stdout:
                wedged.append({
                    "run": run,
                    "returncode": child.returncode,
                    "stdout": stdout.strip()[-200:],
                    "stacks": stderr.strip()[-1500:],
                })
                continue
            _reopen_as_the_daemon_does(f"closing{run}")
            assert stale_indexes(_db_path(f"closing{run}")) == []
        assert not wedged, (
            f"{len(wedged)} of {attempts} shutdowns never finished. Fix: quiesce "
            "the background threads and CHECKPOINT before closing a handle, and "
            "refuse to open one while a shutdown is in progress. "
            f"{wedged}"
        )


class TestUpstreamMechanism:
    """The bug on its own, in raw duckdb, with nothing of ours in the way. These
    are what to re-run against a new duckdb to find out whether it is fixed.
    """

    def test_a_secondary_index_loses_replayed_rows_at_the_first_checkpoint(self, tmp_path):
        path = tmp_path / "secondary.duckdb"
        self._crash_with_pending_wal(path, secondary_index=True)
        duckdb.connect(str(path)).close()  # the checkpoint on close
        conn = duckdb.connect(str(path))
        try:
            through_index = conn.execute(
                "SELECT count(*) FROM t WHERE k = 'k7'").fetchone()[0]
            through_scan = conn.execute(
                "SELECT count(*) FROM t WHERE k || '' = 'k7'").fetchone()[0]
        finally:
            conn.close()
        if through_index != through_scan:
            # Expected on every duckdb from 1.4.0 to at least 1.5.6. An xfail
            # rather than a failure, because no change of ours can fix it - and
            # the day it stops happening, this turns green and says so.
            pytest.xfail(
                f"duckdb {duckdb.__version__} has duckdb/duckdb#26106: the "
                "checkpoint after a WAL replay dropped the replayed rows from "
                f"the secondary index ({through_index} of {through_scan} found "
                "through it)"
            )

    def test_an_explicit_checkpoint_first_keeps_the_secondary_index(self, tmp_path):
        path = tmp_path / "healed.duckdb"
        self._crash_with_pending_wal(path, secondary_index=True)
        conn = duckdb.connect(str(path))
        conn.execute("CHECKPOINT")
        conn.close()
        conn = duckdb.connect(str(path))
        try:
            assert conn.execute(
                "SELECT count(*) FROM t WHERE k = 'k7'").fetchone()[0] == 1
            conn.execute("BEGIN TRANSACTION")
            conn.execute("DELETE FROM t")
            conn.execute("COMMIT")
        finally:
            conn.close()

    def test_a_primary_key_alone_survives_the_same_sequence(self, tmp_path):
        """Why `repair --check` naming a primary key is not evidence that the
        primary key is the stale one: a PK-only table comes through this clean.
        Every table it flagged in production also carries a CREATE INDEX."""
        path = tmp_path / "pk-only.duckdb"
        self._crash_with_pending_wal(path, secondary_index=False)
        duckdb.connect(str(path)).close()
        conn = duckdb.connect(str(path))
        try:
            conn.execute("BEGIN TRANSACTION")
            conn.execute("UPDATE t SET tags = tags")
            conn.execute("DELETE FROM t")
            conn.execute("COMMIT")
        finally:
            conn.close()

    @staticmethod
    def _crash_with_pending_wal(path: Path, secondary_index: bool) -> None:
        """Leave `path` with 500 committed rows that live only in its WAL, the
        way a SIGKILLed daemon does. A child, so the kill is real."""
        index = (
            "con.execute('CREATE INDEX idx_k ON t(k)')\n" if secondary_index else ""
        )
        script = (
            "import os, duckdb, sys\n"
            "con = duckdb.connect(sys.argv[1])\n"
            "con.execute('CREATE TABLE t(id VARCHAR PRIMARY KEY, k VARCHAR, "
            "tags VARCHAR[])')\n"
            f"{index}"
            "con.execute('CHECKPOINT')\n"
            "con.execute(\"INSERT INTO t SELECT 'id' || i, 'k' || i, ['t' || i] "
            "FROM range(500) r(i)\")\n"
            "os._exit(0)\n"  # no close, no checkpoint: the WAL is all there is
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, str(path)],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert Path(f"{path}.wal").exists(), "nothing was left in the WAL to replay"
