"""The committed memory snapshot: one DuckDB file per project, and its merge.

This is the git-committable handoff between machines and people that used to be
one JSON file per category under ``.agent-memory/``. That form was rewritten in
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

import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import duckdb

try:  # POSIX only. Without it the export lock is a no-op, as it was before.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from memory_mcp.config import settings
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
    "snapshot_lock",
    "review_is_empty",
    "SNAPSHOT_DB_NAME",
]

#: Where the snapshot write locks live: under the data dir, one file per
#: snapshot folder, so the repository never sees a lock file. A lock file
#: cannot be deleted after use (another process may be waiting on its inode),
#: so it must live somewhere it can stay.
_LOCK_DIRNAME = "locks"

#: How long a writer waits for another process's export before giving up. An
#: export takes well under a second; this only bounds a stuck holder.
_LOCK_WAIT_SECONDS = 15.0
_LOCK_POLL_SECONDS = 0.05


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

# The columns a merge review compares. A row whose only difference is a
# timestamp or its `source` is the same memory as far as anyone reading it is
# concerned; these are the fields SyncService.apply_snapshot also compares.
_COMPARED_COLUMNS = (
    "category", "title", "content", "summary", "status", "priority", "tags",
    "metadata", "related_ids", "entities", "approval_status", "created_by",
    "approved_by", "approved_at",
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


def _private_tmp(target: Path, suffix: str) -> Path:
    """A build path beside `target` that no other process will pick.

    It used to be one fixed name per target, and `_fresh_db` unlinks whatever
    sits at its path first - so two exports at once (several worktrees ending a
    turn together) deleted each other's half-built database mid-write. The pid
    and a random token make every writer's file its own; the `.tmp` ending
    keeps it matched by the snapshot folder's .gitignore.
    """
    token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return target.parent / f".{target.name}.{token}{suffix}"


@contextmanager
def snapshot_lock(snapshot_dir: Path | str) -> Iterator[None]:
    """Hold the snapshot folder's write lock across processes.

    Separate tmp files stop two writers corrupting each other; this stops them
    interleaving, so an export that read the store first cannot replace a
    snapshot written from a newer read. Waits up to _LOCK_WAIT_SECONDS, then
    raises SnapshotError rather than hang a hook.
    """
    folder = Path(snapshot_dir).resolve()
    if fcntl is None:  # pragma: no cover - Windows
        yield
        return
    lock_dir = settings.data_dir / _LOCK_DIRNAME
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(folder).encode()).hexdigest()[:24]
    lock_path = lock_dir / f"snapshot-{digest}.lock"
    with lock_path.open("a") as handle:
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SnapshotError(
                        f"another process has been writing the snapshot in "
                        f"{folder} for over {int(_LOCK_WAIT_SECONDS)}s; nothing "
                        f"was written"
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
    tmp = _private_tmp(path, ".tmp")

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
        # untidy: a `.memory.duckdb.<pid>-<token>.tmp` beside a good snapshot
        # is a database nothing owns.
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
    on_review: Callable[[dict], None] | None = None,
) -> dict:
    """Reconcile two snapshot databases with SQL. Returns a summary.

    THE MERGE IS NOT THE DECISION. `output` gets the lossless union below,
    because git needs a result and "take one side" loses memories. What the
    union would change on OUR side is also returned as `summary["review"]`
    (see `_review`), and `on_review` is called with it BEFORE `output` is
    replaced: a caller that cannot store the review raises, and the merge then
    fails as a conflict instead of landing unreviewed. The merging session
    decides from that review what main's memory takes (user decision,
    2026-11-20); the import holds those changes back until it has.

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
    tmp = _private_tmp(output, ".merge.tmp")

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
        review = _review(conn)
        review["project_id"] = pid_o or pid_t
        review["slug"] = meta_o.get("slug") or meta_t.get("slug")
        summary = {
            "review": review,
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

    if on_review is not None:
        try:
            on_review(review)
        except Exception:
            _discard(tmp)
            raise
    os.replace(tmp, output)
    Path(str(tmp) + ".wal").unlink(missing_ok=True)
    return summary


def _qualified(alias: str) -> str:
    return ",".join(f"{alias}.{col}" for col in _MEMORY_COLUMNS)


def _differs_sql() -> str:
    return " OR ".join(f"o.{col} IS DISTINCT FROM t.{col}" for col in _COMPARED_COLUMNS)


# Theirs wins the union exactly when the window in merge_snapshots ranks it
# first: newer updated_at, or a timestamp where ours has none. A tie is ours.
_THEIRS_WINS_SQL = (
    "(t.updated_at > o.updated_at "
    "OR (o.updated_at IS NULL AND t.updated_at IS NOT NULL))"
)


def _review(conn: duckdb.DuckDBPyConnection) -> dict:
    """What the merge in `s` changes on OUR side, read from `o`, `t` and `s`.

    - `added`: rows only theirs has, which survived into the union - including
      a re-add strictly newer than our tombstone.
    - `changed`: rows both have that differ, where theirs is newer and won.
    - `removed`: theirs' tombstones we did not have, still standing in the
      union, each with our row when we had one.
    - `kept_ours`: rows both have that differ, where ours won. Nothing changes
      on our side, but theirs' edit is not in the union, and the person
      merging should know that rather than find out later.

    Rows are the dicts `read_snapshot` returns, so they can be posted to the
    daemon's sync-import unchanged once the session applies them.
    """
    width = len(_MEMORY_COLUMNS)
    added = conn.execute(f"""
        SELECT {_qualified('t')} FROM t.memories t
        WHERE NOT EXISTS (SELECT 1 FROM o.memories o WHERE o.id = t.id)
          AND EXISTS (SELECT 1 FROM s.memories m WHERE m.id = t.id)
        ORDER BY t.id
    """).fetchall()
    pairs = conn.execute(f"""
        SELECT {_qualified('o')}, {_qualified('t')}, {_THEIRS_WINS_SQL} AS theirs_won
        FROM o.memories o JOIN t.memories t ON o.id = t.id
        WHERE {_differs_sql()}
        ORDER BY o.id
    """).fetchall()
    removed = conn.execute(f"""
        SELECT t.memory_id, t.category, t.title, t.deleted_at, t.actor, {_qualified('o')}
        FROM t.tombstones t
        LEFT JOIN o.memories o ON o.id = t.memory_id
        WHERE NOT EXISTS (
                SELECT 1 FROM o.tombstones ot
                WHERE ot.memory_id = t.memory_id
                  AND ot.deleted_at IS NOT DISTINCT FROM t.deleted_at
              )
          AND EXISTS (SELECT 1 FROM s.tombstones st WHERE st.memory_id = t.memory_id)
        ORDER BY t.memory_id
    """).fetchall()

    changed: list[dict] = []
    kept_ours: list[dict] = []
    for row in pairs:
        entry = {
            "ours": _memory_dict(row[:width]),
            "theirs": _memory_dict(row[width:2 * width]),
        }
        (changed if row[2 * width] else kept_ours).append(entry)
    return {
        "added": [_memory_dict(row) for row in added],
        "changed": changed,
        "removed": [
            {
                "tombstone": {
                    "memory_id": row[0], "category": row[1], "title": row[2],
                    "deleted_at": _from_dt(row[3]), "actor": row[4],
                },
                "ours": _memory_dict(row[5:]) if row[5] is not None else None,
            }
            for row in removed
        ],
        "kept_ours": kept_ours,
    }


def review_is_empty(review: dict) -> bool:
    """True when a merge changed nothing on our side and dropped no edit."""
    return not any(review.get(key) for key in ("added", "changed", "removed", "kept_ours"))


def _rowcount(result) -> int:
    """DuckDB returns the affected-row count as a single-cell result set."""
    try:
        return int(result[0][0])
    except (IndexError, TypeError, ValueError):
        return 0
