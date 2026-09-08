"""The committed memory snapshot: one DuckDB file per project, and its merge.

This is the git-committable handoff between machines and people that used to be
one JSON file per category under ``.claude-memory/``. That form was rewritten in
full every session and only grew - kalagh reached 1.1M, and this project's
``architecture.json`` alone was 55K - so every session added another large blob
to git history.

THREE THINGS DECIDE THE SHAPE OF THIS MODULE.

1. **One writer per DuckDB file, across processes.** The launchd daemon holds
   ``~/.claude-memory-mcp/projects/<slug>.duckdb`` open for every project it has
   touched. The snapshot is a SEPARATE file: every function here opens it, does
   its work and closes it. Nothing holds it, and nothing here ever opens the
   daemon's database - the CLI gets the rows over HTTP.

2. **A DuckDB file is binary to git.** Two clones that both add a rule produce a
   conflict git cannot resolve, and "take one side" silently discards the other
   side's memories. `merge_snapshots` is the SQL resolution for that, wired up as
   a git merge driver; the two must ship together.

3. **The snapshot carries a schema version.** `read_snapshot` refuses a snapshot
   stamped newer than SNAPSHOT_SCHEMA_VERSION with a message naming the version,
   rather than importing whichever columns it happens to recognise.

Everything is stored as a scalar: lists and dicts are JSON text, not DuckDB LIST
or STRUCT. That keeps the merge a plain ``UNION ALL`` over identically-typed
columns, makes "did this row change?" a string comparison, and leaves the file
readable by anything that speaks DuckDB rather than only by this codebase.
Embeddings are deliberately absent - they are regenerated on import, and they
would dominate the file size.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import duckdb

from memory_mcp.constants import (
    SNAPSHOT_BLOCK_SIZE,
    SNAPSHOT_DB_NAME,
    SNAPSHOT_SCHEMA_VERSION,
)

__all__ = [
    "SnapshotError",
    "SnapshotVersionError",
    "snapshot_db_path",
    "write_snapshot",
    "read_snapshot",
    "merge_snapshots",
    "SNAPSHOT_DB_NAME",
]


class SnapshotError(RuntimeError):
    """The snapshot file is missing, unreadable, or not a snapshot at all."""


class SnapshotVersionError(SnapshotError):
    """Written by a newer memory-mcp than this one can read.

    Raised instead of importing the subset of columns we recognise: a partial
    import of someone else's rules is worse than a clear refusal.
    """


# Columns carried for each memory, in one place so the writer, the reader and
# the merge agree. `embedding` (regenerated), `access_count` (device-local) and
# `pending` (never exported) are excluded on purpose.
_MEMORY_COLUMNS = (
    "id", "category", "title", "content", "summary", "tags", "metadata",
    "status", "priority", "source", "related_ids", "entities",
    "expires_at", "created_at", "updated_at",
    "created_by", "approval_status", "approved_by", "approved_at",
)

# Fields stored as JSON text rather than a DuckDB LIST/STRUCT (see module docs).
_JSON_FIELDS = ("tags", "metadata", "related_ids", "entities")
_TIMESTAMP_FIELDS = ("expires_at", "created_at", "updated_at", "approved_at")

# The snapshot's own tables. `{s}` is the attached alias, so the same statements
# create a fresh export and a merge output without string surgery at the call site.
_DDL = (
    # key/value rather than a one-row table: a reader that meets a key it does
    # not know ignores it, so adding one is not a schema break.
    """
    CREATE TABLE IF NOT EXISTS {s}.snapshot_meta (
        key   VARCHAR PRIMARY KEY,
        value VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {s}.memories (
        id              VARCHAR PRIMARY KEY,
        category        VARCHAR NOT NULL,
        title           VARCHAR NOT NULL,
        content         VARCHAR NOT NULL,
        summary         VARCHAR,
        tags            VARCHAR,
        metadata        VARCHAR,
        status          VARCHAR,
        priority        INTEGER,
        source          VARCHAR,
        related_ids     VARCHAR,
        entities        VARCHAR,
        expires_at      TIMESTAMP,
        created_at      TIMESTAMP,
        updated_at      TIMESTAMP,
        created_by      VARCHAR,
        approval_status VARCHAR,
        approved_by     VARCHAR,
        approved_at     TIMESTAMP
    )
    """,
    # No `id`: the central store's provenance id is an auto-increment integer,
    # which means two machines both call something id 5. The natural key is the
    # whole row, and the merge unions on exactly that.
    """
    CREATE TABLE IF NOT EXISTS {s}.provenance (
        memory_id  VARCHAR NOT NULL,
        operation  VARCHAR NOT NULL,
        details    VARCHAR,
        actor      VARCHAR,
        created_at TIMESTAMP
    )
    """,
    # The ONLY thing that means "deleted". A row missing from one side is a row
    # that side has not seen yet - treating absence as deletion would make every
    # `git pull` silent data loss. A soft delete needs no tombstone: it stays a
    # row with status='archived' and merges like any other edit.
    """
    CREATE TABLE IF NOT EXISTS {s}.tombstones (
        memory_id  VARCHAR PRIMARY KEY,
        category   VARCHAR,
        title      VARCHAR,
        deleted_at TIMESTAMP,
        actor      VARCHAR
    )
    """,
)

# No secondary indexes on purpose. Every reader here scans whole tables - export
# writes them, import reads them, the merge unions them - so an index buys
# nothing, and DuckDB charges roughly one 16KB block per index in a file that is
# committed to git on every session. The two PRIMARY KEYs stay because the JSON
# migration depends on INSERT OR REPLACE.


def snapshot_db_path(snapshot_dir: Path | str) -> Path:
    return Path(snapshot_dir) / SNAPSHOT_DB_NAME


# ---------- value coercion ----------


def _to_text(value) -> str | None:
    """A JSON field on the way in. Already-serialized text is passed through."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True) if value else None
    return json.dumps(value, sort_keys=True)


def _from_text(value, default):
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _to_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _from_dt(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _memory_row(mem: dict) -> list:
    row: list = []
    for col in _MEMORY_COLUMNS:
        value = mem.get(col)
        if col in _JSON_FIELDS:
            row.append(_to_text(value))
        elif col in _TIMESTAMP_FIELDS:
            row.append(_to_dt(value))
        elif col == "priority":
            row.append(int(value) if value is not None else 0)
        else:
            row.append(value)
    return row


def _memory_dict(row: tuple) -> dict:
    mem = dict(zip(_MEMORY_COLUMNS, row))
    for col in _JSON_FIELDS:
        mem[col] = _from_text(mem[col], {} if col == "metadata" else [])
    if not mem["metadata"]:
        mem["metadata"] = None
    for col in _TIMESTAMP_FIELDS:
        mem[col] = _from_dt(mem[col])
    return mem


# ---------- connection helpers ----------


def _fresh_db(path: Path) -> duckdb.DuckDBPyConnection:
    """Create an EMPTY snapshot database at `path` and return it attached as `s`.

    Small blocks on purpose: DuckDB's 256KB default gives a ~525KB floor for an
    empty file, which would make the committed snapshot larger than the JSON it
    replaces. 16KB is the minimum DuckDB accepts and puts a real project in the
    tens of KB. It is a create-time property; readers take it from the header.
    """
    for stale in (path, Path(str(path) + ".wal")):
        stale.unlink(missing_ok=True)
    conn = duckdb.connect()
    conn.execute(
        f"ATTACH '{_sql_path(path)}' AS s (BLOCK_SIZE {int(SNAPSHOT_BLOCK_SIZE)})"
    )
    for ddl in _DDL:
        conn.execute(ddl.format(s="s"))
    return conn


def _sql_path(path: Path | str) -> str:
    """A path safe to interpolate into ATTACH, which takes no parameters."""
    return str(path).replace("'", "''")


def _discard(path: Path) -> None:
    """Remove a half-built database and the write-ahead log beside it."""
    path.unlink(missing_ok=True)
    Path(str(path) + ".wal").unlink(missing_ok=True)


def _attach_read_only(conn: duckdb.DuckDBPyConnection, path: Path, alias: str) -> bool:
    """ATTACH `path` read-only as `alias`. False when there is nothing to attach.

    git hands the merge driver an empty file for the base when two branches added
    the snapshot independently, so "absent or zero bytes" is a normal state, not
    an error.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return False
    conn.execute(f"ATTACH '{_sql_path(path)}' AS {alias} (READ_ONLY)")
    return True


# ---------- write ----------


def write_snapshot(
    path: Path | str,
    *,
    project_id: str | None,
    slug: str,
    categories: dict[str, list[dict]],
    provenance: list[dict] | None = None,
    tombstones: list[dict] | None = None,
    exported_at: str | None = None,
) -> dict:
    """Write the whole snapshot to `path`, atomically. Returns row counts.

    Built beside the target and moved into place with os.replace, so a process
    killed mid-export leaves the previous snapshot intact rather than a truncated
    database that the next import would read as "the project has three rules".
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"

    memories: list[dict] = []
    for category, items in (categories or {}).items():
        for mem in items or []:
            entry = dict(mem)
            entry.setdefault("category", category)
            if entry.get("id"):
                memories.append(entry)

    conn = _fresh_db(tmp)
    try:
        if memories:
            placeholders = ",".join("?" * len(_MEMORY_COLUMNS))
            conn.executemany(
                f"INSERT OR REPLACE INTO s.memories "
                f"({','.join(_MEMORY_COLUMNS)}) VALUES ({placeholders})",
                [_memory_row(m) for m in memories],
            )
        if provenance:
            conn.executemany(
                "INSERT INTO s.provenance "
                "(memory_id, operation, details, actor, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    [
                        p.get("memory_id"),
                        p.get("operation"),
                        _to_text(p.get("details")),
                        p.get("actor"),
                        _to_dt(p.get("created_at")),
                    ]
                    for p in provenance
                    if p.get("memory_id") and p.get("operation")
                ],
            )
        if tombstones:
            conn.executemany(
                "INSERT OR REPLACE INTO s.tombstones "
                "(memory_id, category, title, deleted_at, actor) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    [
                        t.get("memory_id"),
                        t.get("category"),
                        t.get("title"),
                        _to_dt(t.get("deleted_at")),
                        t.get("actor"),
                    ]
                    for t in tombstones
                    if t.get("memory_id")
                ],
            )
        _write_meta(conn, {
            "schema_version": str(SNAPSHOT_SCHEMA_VERSION),
            "project_id": project_id,
            "slug": slug,
            "exported_at": exported_at or datetime.now().isoformat(timespec="seconds"),
        })
        conn.execute("CHECKPOINT s")
    except Exception:
        # Take the half-built file with us. Leaving it behind is not just
        # untidy: `.memory.duckdb.tmp` beside a good snapshot is a database
        # nothing owns, and the next export would have to decide whether it is
        # a crash or a concurrent writer.
        conn.close()
        _discard(tmp)
        raise
    else:
        conn.close()

    os.replace(tmp, path)
    Path(str(tmp) + ".wal").unlink(missing_ok=True)
    return {
        "memories": len(memories),
        "provenance": len(provenance or []),
        "tombstones": len(tombstones or []),
    }


def _write_meta(conn: duckdb.DuckDBPyConnection, meta: dict) -> None:
    rows = [[k, None if v is None else str(v)] for k, v in meta.items()]
    conn.executemany(
        "INSERT OR REPLACE INTO s.snapshot_meta (key, value) VALUES (?, ?)", rows,
    )


# ---------- read ----------


def read_snapshot(path: Path | str) -> dict:
    """Read a snapshot back into the shape `sync-import` posts to the daemon.

    Raises SnapshotVersionError when the file was written by a newer memory-mcp:
    the caller must refuse the whole import rather than apply the part it
    understands.
    """
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise SnapshotError(f"No memory snapshot at {path}")

    conn = duckdb.connect()
    try:
        try:
            conn.execute(f"ATTACH '{_sql_path(path)}' AS s (READ_ONLY)")
        except duckdb.Error as e:
            raise SnapshotError(f"{path} is not a readable memory snapshot: {e}") from e

        meta = _read_meta(conn, "s")
        version = _meta_version(meta)
        if version > SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotVersionError(
                f"{path.name} was written by a newer memory-mcp (snapshot schema "
                f"v{version}; this build reads v{SNAPSHOT_SCHEMA_VERSION}). "
                f"Update memory-mcp - nothing was imported."
            )

        rows = conn.execute(
            f"SELECT {','.join('s.memories.' + c for c in _MEMORY_COLUMNS)} "
            f"FROM s.memories ORDER BY id"
        ).fetchall()
        categories: dict[str, list[dict]] = {}
        for row in rows:
            mem = _memory_dict(row)
            categories.setdefault(mem["category"], []).append(mem)

        provenance = [
            {
                "memory_id": r[0], "operation": r[1],
                "details": _from_text(r[2], None), "actor": r[3],
                "created_at": _from_dt(r[4]),
            }
            for r in conn.execute(
                "SELECT memory_id, operation, details, actor, created_at "
                "FROM s.provenance ORDER BY created_at, memory_id"
            ).fetchall()
        ]
        tombstones = [
            {
                "memory_id": r[0], "category": r[1], "title": r[2],
                "deleted_at": _from_dt(r[3]), "actor": r[4],
            }
            for r in conn.execute(
                "SELECT memory_id, category, title, deleted_at, actor "
                "FROM s.tombstones ORDER BY memory_id"
            ).fetchall()
        ]
    finally:
        conn.close()

    return {
        "meta": meta,
        "schema_version": version,
        "categories": categories,
        "provenance": provenance,
        "tombstones": tombstones,
    }


def _read_meta(conn: duckdb.DuckDBPyConnection, alias: str) -> dict:
    try:
        rows = conn.execute(f"SELECT key, value FROM {alias}.snapshot_meta").fetchall()
    except duckdb.Error:
        return {}
    return {r[0]: r[1] for r in rows}


def _meta_version(meta: dict) -> int:
    try:
        return int(meta.get("schema_version") or 1)
    except (TypeError, ValueError):
        return 1


def count_memories(path: Path | str) -> int:
    """Row count only - the cheap half of "did the write actually land?"."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return 0
    conn = duckdb.connect()
    try:
        conn.execute(f"ATTACH '{_sql_path(path)}' AS s (READ_ONLY)")
        return int(conn.execute("SELECT count(*) FROM s.memories").fetchone()[0])
    except duckdb.Error:
        return 0
    finally:
        conn.close()


# ---------- merge ----------


def merge_snapshots(
    base: Path | str | None,
    ours: Path | str,
    theirs: Path | str,
    output: Path | str,
) -> dict:
    """Reconcile two snapshot databases with SQL. Returns a summary.

    THE POLICY, and why each half of it is what it is:

    - **Rows only one side has are kept.** Absence is NOT deletion: a row missing
      from one side is usually a row that side has not pulled yet. This is also
      why `base` is accepted but never used to decide a row's fate - "in the base
      and gone from one side" is exactly the case that looks like a delete and
      is not one.
    - **Rows both sides changed resolve to the newer `updated_at`**, the same
      last-write-wins the task bridge and `SyncService.apply_snapshot` already
      apply. A tie resolves to ours, so the merge is deterministic.
    - **A tombstone deletes**, unless a side's row was edited strictly after the
      tombstone was written - an edit that new is a deliberate resurrection, and
      the tombstone is then dropped so the next merge does not re-kill it.
    - **Provenance is unioned, never resolved.** It is an append-only audit
      trail; last-write-wins on an audit trail loses entries.

    Raises SnapshotError when either side is unreadable, when either is stamped
    newer than this build, or when the two describe DIFFERENT projects. The
    caller turns that into a non-zero exit so git leaves the conflict for a
    human instead of guessing.
    """
    ours, theirs, output = Path(ours), Path(theirs), Path(output)
    tmp = output.parent / f".{output.name}.merge.tmp"

    conn = _fresh_db(tmp)
    try:
        if not _attach_read_only(conn, ours, "o"):
            raise SnapshotError(f"'ours' snapshot is missing or empty: {ours}")
        if not _attach_read_only(conn, theirs, "t"):
            raise SnapshotError(f"'theirs' snapshot is missing or empty: {theirs}")
        # Attached and version-checked but deliberately unused for row decisions.
        has_base = bool(base) and _attach_read_only(conn, Path(base), "b")

        meta_o, meta_t = _read_meta(conn, "o"), _read_meta(conn, "t")
        for alias, meta in (("ours", meta_o), ("theirs", meta_t)):
            version = _meta_version(meta)
            if version > SNAPSHOT_SCHEMA_VERSION:
                raise SnapshotVersionError(
                    f"the '{alias}' snapshot is schema v{version}; this build "
                    f"merges up to v{SNAPSHOT_SCHEMA_VERSION}. Update memory-mcp "
                    f"and merge again - nothing was written."
                )
        pid_o, pid_t = meta_o.get("project_id"), meta_t.get("project_id")
        if pid_o and pid_t and pid_o != pid_t:
            # Two different projects' memories in one file is the one thing that
            # cannot be undone afterwards. Refuse, loudly.
            raise SnapshotError(
                f"these snapshots belong to different projects "
                f"({meta_o.get('slug') or pid_o} vs {meta_t.get('slug') or pid_t}). "
                f"Refusing to merge them."
            )

        cols = ",".join(_MEMORY_COLUMNS)
        conn.execute(f"""
            INSERT INTO s.memories ({cols})
            SELECT {cols} FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY id
                    ORDER BY updated_at DESC NULLS LAST, side ASC
                ) AS rn
                FROM (
                    SELECT {cols}, 'a_ours' AS side FROM o.memories
                    UNION ALL
                    SELECT {cols}, 'b_theirs' AS side FROM t.memories
                )
            ) WHERE rn = 1
        """)

        conn.execute("""
            INSERT INTO s.tombstones (memory_id, category, title, deleted_at, actor)
            SELECT memory_id, category, title, deleted_at, actor FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY memory_id ORDER BY deleted_at DESC NULLS LAST
                ) AS rn
                FROM (
                    SELECT memory_id, category, title, deleted_at, actor FROM o.tombstones
                    UNION ALL
                    SELECT memory_id, category, title, deleted_at, actor FROM t.tombstones
                )
            ) WHERE rn = 1
        """)

        conn.execute("""
            INSERT INTO s.provenance (memory_id, operation, details, actor, created_at)
            SELECT DISTINCT memory_id, operation, details, actor, created_at FROM (
                SELECT memory_id, operation, details, actor, created_at FROM o.provenance
                UNION ALL
                SELECT memory_id, operation, details, actor, created_at FROM t.provenance
            )
        """)

        # A tombstone kills its row unless the row carries a strictly newer edit.
        # NULL updated_at cannot be newer than anything, so it loses.
        deleted = conn.execute("""
            DELETE FROM s.memories WHERE id IN (
                SELECT m.id FROM s.memories m JOIN s.tombstones ts ON ts.memory_id = m.id
                WHERE m.updated_at IS NULL
                   OR ts.deleted_at IS NULL
                   OR m.updated_at <= ts.deleted_at
            )
        """).fetchall()
        # ...and a resurrection drops the tombstone, or the next merge re-kills it.
        resurrected = conn.execute("""
            DELETE FROM s.tombstones WHERE memory_id IN (SELECT id FROM s.memories)
        """).fetchall()

        _write_meta(conn, {
            "schema_version": str(SNAPSHOT_SCHEMA_VERSION),
            "project_id": pid_o or pid_t,
            "slug": meta_o.get("slug") or meta_t.get("slug"),
            "exported_at": max(
                filter(None, [meta_o.get("exported_at"), meta_t.get("exported_at")]),
                default=datetime.now().isoformat(timespec="seconds"),
            ),
            "merged_at": datetime.now().isoformat(timespec="seconds"),
        })
        summary = {
            "memories": int(
                conn.execute("SELECT count(*) FROM s.memories").fetchone()[0]
            ),
            "provenance": int(
                conn.execute("SELECT count(*) FROM s.provenance").fetchone()[0]
            ),
            "tombstones": int(
                conn.execute("SELECT count(*) FROM s.tombstones").fetchone()[0]
            ),
            "deleted_by_tombstone": _rowcount(deleted),
            "resurrected": _rowcount(resurrected),
            "had_base": has_base,
        }
        conn.execute("CHECKPOINT s")
    except Exception:
        conn.close()
        _discard(tmp)
        raise
    else:
        conn.close()

    os.replace(tmp, output)
    Path(str(tmp) + ".wal").unlink(missing_ok=True)
    return summary


def _rowcount(result) -> int:
    """DuckDB returns the affected-row count as a single-cell result set."""
    try:
        return int(result[0][0])
    except (IndexError, TypeError, ValueError):
        return 0
