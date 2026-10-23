"""Find and rebuild tables whose PRIMARY KEY index has gone stale.

The disease (v0.11.9 diagnosis, see db/connection.py): a database torn by the
old open/close race keeps rows whose primary-key index entry no longer matches.
Reads are fine. Plain inserts are fine. The first UPDATE DuckDB runs as a
DELETE plus INSERT of such a row - a list column, an indexed column - raises
"INTERNAL Error: Failed to append to PRIMARY_<table>: duplicate key" at commit,
and DuckDB makes that a FatalException that terminates the PROCESS. Python
cannot catch it. The daemon dies, launchd restarts it, the next write to that
row kills it again.

So detection cannot run in the process that matters. `probe` copies the file
and, for each table with a single-column primary key, commits
`UPDATE t SET pk = pk` in a throwaway subprocess: every row goes through
delete+insert, a stale entry aborts the child, a healthy table exits 0.
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

import shutil
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

import duckdb

from memory_mcp.db.connection import _resolve_db_path, connect, transaction

# Runs in the child. Its only output that matters is the exit code.
_PROBE = (
    "import duckdb, sys\n"
    "c = duckdb.connect(sys.argv[1])\n"
    "c.execute(f'UPDATE \"{sys.argv[2]}\" SET \"{sys.argv[3]}\" = \"{sys.argv[3]}\"')\n"
    "print('ok')\n"
)


def _keyed_tables(conn: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """(table, key column) for every main-schema table with a one-column key."""
    rows = conn.execute(
        "SELECT table_name, constraint_column_names FROM duckdb_constraints() "
        "WHERE constraint_type = 'PRIMARY KEY' AND schema_name = 'main' "
        "ORDER BY table_name"
    ).fetchall()
    return [(table, cols[0]) for table, cols in rows if len(cols) == 1]


def probe(db_path: Path, timeout: float = 300.0) -> list[dict]:
    """Return the tables in `db_path` whose key index aborts a delete+insert.

    Never touches `db_path` itself beyond copying it (with its WAL, so writes
    not yet checkpointed are part of what is probed). Each finding is
    `{"table", "key", "returncode", "error"}`.
    """
    findings: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="memory-mcp-probe-") as work:
        source = Path(work) / "source.duckdb"
        _copy_with_wal(db_path, source)
        conn = duckdb.connect(str(source), read_only=True)
        try:
            tables = _keyed_tables(conn)
        finally:
            conn.close()
        for table, key in tables:
            target = Path(work) / "probe.duckdb"
            _copy_with_wal(source, target)
            result = subprocess.run(
                [sys.executable, "-I", "-c", _PROBE, str(target), table, key],
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0 or "ok" not in result.stdout:
                lines = (result.stderr or result.stdout).strip().splitlines()
                findings.append({
                    "table": table,
                    "key": key,
                    "returncode": result.returncode,
                    "error": _short(lines[0] if lines else ""),
                })
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
    if not findings or check_only:
        result["status"] = "stale" if findings else "ok"
        return result
    with transaction(slug) as conn:
        for finding in findings:
            rows = rebuild_table(conn, finding["table"])
            result["rebuilt"].append({"table": finding["table"], "rows": rows})
    _checkpoint(slug)
    still = probe(db_path)
    result["stale_after"] = still
    result["status"] = "repaired" if not still else "still_stale"
    return result


def _checkpoint(slug: str) -> None:
    """Fold the WAL into the file, so a copy of it is the state being served.
    Busy is fine: the probe copies the WAL as well."""
    with suppress(Exception), connect(slug) as conn:
        conn.execute("CHECKPOINT")
