"""Find and rebuild tables whose indexes have gone stale.

The disease (v0.11.9 diagnosis, see db/connection.py): a database torn by the
old open/close race keeps rows whose primary-key index entry no longer matches.
Reads are fine. Plain inserts are fine. The first UPDATE DuckDB runs as a
DELETE plus INSERT of such a row - a list column, an indexed column - raises
"INTERNAL Error: Failed to append to PRIMARY_<table>: duplicate key" at commit,
and DuckDB makes that a FatalException that terminates the PROCESS. Python
cannot catch it. The daemon dies, launchd restarts it, the next write to that
row kills it again.

DuckDB 1.5 can also invalidate the database instead of aborting: on
2026-10-29 a DELETE on task_plans failed with "Failed to delete all rows from
index" on the SECONDARY index idx_task_plans_hash, and every call failed until a
restart. So every ART index counts - primary keys of any width, UNIQUE
constraints and plain CREATE INDEX - not only one-column primary keys.

So detection cannot run in the process that matters. `probe` copies the file
and, for each indexed table, commits `UPDATE t SET <every indexed col> = itself`
and `DELETE FROM t` in a throwaway subprocess: every row goes through
delete+insert in every index, a stale entry aborts the child, a healthy table
exits 0.
Validated against the copy of the kalagh database that crash-looped the daemon
57 times (it aborts on `tasks`) and against 47 healthy databases (none abort).

The repair is a rebuild of just that table from its own DDL: the CREATE TABLE
DuckDB reports for it (constraints, defaults and sequence-backed ids intact),
every row copied, the old table dropped, the new one renamed in, its indexes
recreated - in one transaction. A fresh table builds a fresh key index from the
rows themselves, which is the whole repair. Rows, ids and sequences are
untouched; rebuilding a healthy table is harmless.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from memory_mcp.config import settings
from memory_mcp.db.connection import _resolve_db_path, connect, paused, transaction

log = logging.getLogger(__name__)

# Runs in the child: argv is the path, then one JSON [table, [columns...]] per
# table. For each, every indexed column is rewritten to itself (DuckDB runs that
# as a delete plus an insert in every index over it), every row is deleted, and
# the lot committed - the commit is where a stale key entry aborts. "begin" and
# "ok" lines name the table, so the parent knows which one a death belongs to.
# vss is loaded so a table under an HNSW index can be written.
_PROBE = (
    "import duckdb, json, sys\n"
    "c = duckdb.connect(sys.argv[1])\n"
    "try:\n"
    "    c.execute('LOAD vss'); c.execute('SET hnsw_enable_experimental_persistence = true')\n"
    "except Exception:\n"
    "    pass\n"
    # The copy carries the WAL: checkpoint before anything else can, exactly as
    # the daemon does on open, or the child's own close would hit duckdb#26106.
    "c.execute('CHECKPOINT')\n"
    "for arg in sys.argv[2:]:\n"
    "    t, cols = json.loads(arg)\n"
    "    print('begin', t, flush=True)\n"
    "    sets = ', '.join(f'\"{col}\" = \"{col}\"' for col in cols)\n"
    "    c.execute('BEGIN TRANSACTION')\n"
    "    c.execute(f'UPDATE \"{t}\" SET {sets}')\n"
    "    c.execute(f'DELETE FROM \"{t}\"')\n"
    "    c.execute('COMMIT')\n"
    "    print('ok', t, flush=True)\n"
    "c.close()\n"
    "print('closed', flush=True)\n"
)


def _indexed_tables(conn: duckdb.DuckDBPyConnection) -> list[tuple[str, list[str], list[str]]]:
    """(table, indexed columns, index names) for every main-schema table that
    carries any ART index: a PRIMARY KEY of any width, a UNIQUE constraint, or a
    plain CREATE INDEX such as idx_task_plans_hash.

    v0.11.9 probed only one-column primary keys, so a stale SECONDARY entry went
    unseen until it took the database down ("Failed to delete all rows from
    index", 2026-10-29). HNSW indexes are skipped: they are not ART, and an
    index over an expression rather than a column cannot be rewritten in place.
    """
    columns: dict[str, list[str]] = {}
    names: dict[str, list[str]] = {}
    known = {
        (table, column)
        for table, column in conn.execute(
            "SELECT table_name, column_name FROM duckdb_columns() WHERE schema_name = 'main'"
        ).fetchall()
    }

    def add(table: str, name: str, cols: list[str]) -> None:
        cols = [col for col in cols if (table, col) in known]
        if not cols:
            return
        bucket = columns.setdefault(table, [])
        bucket.extend(col for col in cols if col not in bucket)
        names.setdefault(table, []).append(name)

    for table, kind, cols in conn.execute(
        "SELECT table_name, constraint_type, constraint_column_names FROM duckdb_constraints() "
        "WHERE constraint_type IN ('PRIMARY KEY', 'UNIQUE') AND schema_name = 'main' "
        "ORDER BY table_name, constraint_type"
    ).fetchall():
        add(table, f"{kind} ({', '.join(cols)})", list(cols))
    for table, name, expressions, sql in conn.execute(
        "SELECT table_name, index_name, expressions, sql FROM duckdb_indexes() "
        "WHERE schema_name = 'main' ORDER BY table_name, index_name"
    ).fetchall():
        if sql and "USING HNSW" in sql.upper():
            continue
        add(table, name, _expression_columns(expressions))
    return [(table, columns[table], names[table]) for table in sorted(columns)]


def _expression_columns(expressions) -> list[str]:
    """duckdb_indexes().expressions as plain column names: '[task_id, sha256]'
    (a string on duckdb 1.5) or a list. Anything that is not a bare identifier
    comes back as itself and is dropped by the caller's column check."""
    if expressions is None:
        return []
    if isinstance(expressions, str):
        expressions = expressions.strip().removeprefix("[").removesuffix("]").split(",")
    return [str(e).strip().strip('"') for e in expressions if str(e).strip()]


def probe(db_path: Path, timeout: float = 300.0) -> list[dict]:
    """Return the tables in `db_path` with an index that aborts a delete+insert.

    Never touches `db_path` itself beyond copying it (with its WAL, so writes
    not yet checkpointed are part of what is probed). Every index of a table is
    exercised at once - a stale entry in any of them means rebuilding the
    table, which rebuilds all of them. One child walks every table on one copy;
    a table that kills it is recorded and the walk resumes on a fresh copy from
    the next one, so a healthy file costs one copy, not one per table (the
    startup check runs this over every project). Each finding is
    `{"table", "key", "columns", "indexes", "returncode", "error"}`; `key` is the
    first indexed column, kept for older readers of this result.
    """
    findings: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="memory-mcp-probe-") as work:
        source = Path(work) / "source.duckdb"
        _copy_with_wal(db_path, source)
        conn = duckdb.connect(str(source), read_only=True)
        try:
            pending = _indexed_tables(conn)
        finally:
            conn.close()
        target = Path(work) / "probe.duckdb"
        while pending:
            _copy_with_wal(source, target)
            result = subprocess.run(
                [sys.executable, "-I", "-c", _PROBE, str(target),
                 *(json.dumps([table, cols]) for table, cols, _ in pending)],
                capture_output=True, text=True, timeout=timeout,
            )
            done = {
                line[3:] for line in result.stdout.splitlines() if line.startswith("ok ")
            }
            begun = [
                line[6:] for line in result.stdout.splitlines() if line.startswith("begin ")
            ]
            failed = next((t for t in reversed(begun) if t not in done), None)
            if failed is None and result.returncode == 0 and "closed" in result.stdout:
                break
            lines = (result.stderr or "").strip().splitlines()
            error = next((ln for ln in lines if "Error" in ln), lines[-1] if lines else "")
            if failed is None:
                # Died outside any table - opening, or closing after the last
                # one. Blaming a table would rebuild a healthy one, and the
                # next copy would die the same way: report it and stop.
                findings.append({
                    "table": None, "key": None, "columns": [], "indexes": [],
                    "returncode": result.returncode,
                    "error": _short(error) or "the probe died outside any table",
                })
                break
            table, cols, index_names = next(p for p in pending if p[0] == failed)
            findings.append({
                "table": table,
                "key": cols[0],
                "columns": cols,
                "indexes": index_names,
                "returncode": result.returncode,
                "error": _short(error),
            })
            position = [p[0] for p in pending].index(failed)
            pending = pending[position + 1:]
        for leftover in (target, Path(str(target) + ".wal")):
            leftover.unlink(missing_ok=True)
    return findings


def _copy_with_wal(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)
    wal = Path(str(src) + ".wal")
    dst_wal = Path(str(dst) + ".wal")
    if wal.exists():
        shutil.copyfile(wal, dst_wal)
    else:
        dst_wal.unlink(missing_ok=True)


def _short(line: str) -> str:
    marker = "INTERNAL Error: "
    if marker in line:
        line = line[line.index(marker):]
    return line[:240]


def rebuild_table(conn: duckdb.DuckDBPyConnection, table: str) -> int:
    """Rebuild `table` in place from its own DDL. Returns the row count.

    The caller owns the transaction. Raises if the rebuilt table does not hold
    exactly the rows the old one did, which rolls the whole thing back.
    """
    row = conn.execute(
        "SELECT sql FROM duckdb_tables() WHERE schema_name = 'main' AND table_name = ?",
        [table],
    ).fetchone()
    if row is None or not row[0]:
        raise ValueError(f"no table named {table!r}")
    ddl: str = row[0]
    prefix = f"CREATE TABLE {table}("
    if not ddl.startswith(prefix):
        raise ValueError(f"unexpected DDL for {table!r}: {ddl[:80]}")
    indexes = [
        r[0] for r in conn.execute(
            "SELECT sql FROM duckdb_indexes() "
            "WHERE schema_name = 'main' AND table_name = ? AND sql IS NOT NULL",
            [table],
        ).fetchall()
    ]
    scratch = f"{table}__rebuild"
    before = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    conn.execute(f'DROP TABLE IF EXISTS "{scratch}"')
    conn.execute(f'CREATE TABLE "{scratch}"(' + ddl[len(prefix):])
    conn.execute(f'INSERT INTO "{scratch}" SELECT * FROM "{table}"')
    after = conn.execute(f'SELECT count(*) FROM "{scratch}"').fetchone()[0]
    if after != before:
        raise RuntimeError(f"{table}: rebuilt {after} rows of {before}")
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{scratch}" RENAME TO "{table}"')
    for statement in indexes:
        conn.execute(statement)
    return after


def repair_project(slug: str, check_only: bool = False) -> dict:
    """Probe one project's database and rebuild whatever is stale.

    Safe to run against the database a live daemon holds, from inside that
    daemon: the probe works on a copy in child processes, and the rebuild is
    an ordinary transaction on this process's own handle.
    """
    db_path = _resolve_db_path(slug)
    if not db_path.exists():
        return {"project": slug, "status": "missing", "db_path": str(db_path)}
    _checkpoint(slug)
    findings = probe(db_path)
    result: dict = {
        "project": slug,
        "db_path": str(db_path),
        "stale": findings,
        "rebuilt": [],
    }
    stale = [f for f in findings if f["table"] is not None]
    if len(stale) < len(findings):
        result["status"] = "probe_error"
        result["error"] = next(f["error"] for f in findings if f["table"] is None)
        if not stale or check_only:
            return result
    if not findings or check_only:
        result["status"] = "stale" if findings else "ok"
        return result
    # Paused: the rebuild reads its own snapshot, so a row another thread
    # committed during it would vanish with the old table.
    with paused(db_path), transaction(slug) as conn:
        for finding in stale:
            rows = rebuild_table(conn, finding["table"])
            result["rebuilt"].append({"table": finding["table"], "rows": rows})
    _checkpoint(slug)
    still = probe(db_path)
    result["stale_after"] = still
    if result.get("status") != "probe_error":
        result["status"] = "repaired" if not still else "still_stale"
    return result


def _checkpoint(slug: str) -> None:
    """Fold the WAL into the file, so a copy of it is the state being served.
    Busy is fine: the probe copies the WAL as well."""
    with suppress(Exception), connect(slug) as conn:
        conn.execute("CHECKPOINT")


# ---------- the startup check ----------
#
# Two markers in the data dir decide whether the daemon checks every project
# when it starts. FATAL_MARKER is written by connection._heal whenever DuckDB
# invalidates a database, and says which project and table. RUNNING_SENTINEL is
# written when the daemon starts and removed when it stops cleanly, so finding
# it at startup means the last process died - a segfault, an abort on a stale
# key, a kill -9 - and wrote nothing on the way out.

FATAL_MARKER = "fatal-errors.json"
RUNNING_SENTINEL = "daemon.running"


_marker_lock = threading.Lock()


def _read_events() -> list[dict]:
    path = settings.data_dir / FATAL_MARKER
    try:
        events = json.loads(path.read_text()) if path.exists() else []
    except (OSError, ValueError):
        return []
    return events if isinstance(events, list) else []


def _write_events(events: list[dict]) -> None:
    path = settings.data_dir / FATAL_MARKER
    if not events:
        path.unlink(missing_ok=True)
        return
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".tmp")
    scratch.write_text(json.dumps(events[-50:], indent=2))
    scratch.replace(path)


def record_fatal(slug: str, error: str, hint: str | None = None) -> None:
    """Note that `slug`'s database was invalidated, for the next startup."""
    with _marker_lock:
        _record_fatal_locked(slug, error, hint)


def _record_fatal_locked(slug: str, error: str, hint: str | None) -> None:
    events = _read_events()
    events.append({
        "project": slug,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": error[:400],
        "where": hint,
    })
    _write_events(events)


def startup_reason() -> str | None:
    """Why this start must check every project, or None for a clean restart.

    Also writes the sentinel for THIS process; `mark_clean_shutdown` removes it.
    """
    sentinel = settings.data_dir / RUNNING_SENTINEL
    marker = settings.data_dir / FATAL_MARKER
    reasons = []
    if marker.exists():
        reasons.append("a database was invalidated by a fatal error")
    if sentinel.exists():
        reasons.append("the previous daemon did not shut down cleanly")
    with suppress(OSError):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return " and ".join(reasons) or None


def mark_clean_shutdown() -> None:
    with suppress(OSError):
        (settings.data_dir / RUNNING_SENTINEL).unlink(missing_ok=True)


def startup_check(slugs: list[str] | None = None) -> list[dict]:
    """Probe every project and rebuild whatever is stale, logging each table.

    Clears the fatal marker only once every project came back healthy or
    repaired, so a check that could not finish runs again next start.
    """
    with _marker_lock:
        seen = len(_read_events())
    if slugs is None:
        from memory_mcp.repositories import ProjectRepository

        slugs = [p.slug for p in ProjectRepository().list_all()]
    results = []
    clean = True
    for slug in slugs:
        try:
            result = repair_project(slug)
        except Exception as e:  # noqa: BLE001 - one project must not stop the rest
            result = {"project": slug, "status": "error", "error": str(e)}
        results.append(result)
        status = result.get("status")
        rebuilt = [r["table"] for r in result.get("rebuilt") or []]
        if rebuilt or status not in ("ok", "missing"):
            log.warning(
                "memory-mcp startup check: %s: %s; rebuilt %s%s",
                slug, status, rebuilt or "nothing",
                f" - {result['error']}" if result.get("error") else "",
            )
        else:
            log.info("memory-mcp startup check: %s: %s", slug, status)
        if status not in ("ok", "repaired", "missing"):
            clean = False
    if clean:
        # Only the events this check answered: one recorded while it ran is a
        # database invalidated since, and the next start must look at it.
        with suppress(OSError), _marker_lock:
            _write_events(_read_events()[seen:])
    return results


def start_startup_check() -> threading.Thread | None:
    """At daemon start: if the last process died or a database was invalidated,
    check every project in the background. Never blocks the daemon starting -
    the probe copies each file, and a call meanwhile heals itself anyway."""
    reason = startup_reason()
    if reason is None:
        return None
    log.warning("memory-mcp: %s - checking every project's indexes", reason)
    thread = threading.Thread(
        target=startup_check, name="memory-mcp-startup-repair", daemon=True,
    )
    thread.start()
    return thread
