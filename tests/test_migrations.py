"""Backward-compatibility tests: legacy DuckDB files upgrade in place on open."""

import duckdb

from memory_mcp.db.schema import (
    CURRENT_SCHEMA_VERSION, create_schema, get_schema_version, run_migrations,
)


def _make_v1_db(path) -> None:
    """Create a DB shaped like the pre-v2 schema: no summary/entities/expires_at,
    no provenance table, no schema_version table."""
    conn = duckdb.connect(str(path))
    conn.execute("""
        CREATE TABLE memories (
            id VARCHAR PRIMARY KEY, category VARCHAR, title VARCHAR,
            content VARCHAR, embedding FLOAT[384], status VARCHAR,
            created_at TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO memories VALUES ('m1','decision','t','c',NULL,'active',current_timestamp)"
    )
    conn.close()


def test_legacy_db_detected_as_v1(tmp_path):
    db = tmp_path / "legacy.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        assert get_schema_version(conn) == 1
    finally:
        conn.close()


def test_migration_adds_v2_columns_and_tables(tmp_path):
    db = tmp_path / "legacy.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        version = run_migrations(conn)
        assert version == CURRENT_SCHEMA_VERSION
        cols = {r[1] for r in conn.execute("PRAGMA table_info('memories')").fetchall()}
        assert {"summary", "entities", "expires_at"} <= cols
        tables = {
            r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        assert "provenance" in tables
    finally:
        conn.close()


def test_migration_adds_v3_approval_columns(tmp_path):
    """A legacy DB gains the rule-approval columns, and every existing rule is
    backfilled to 'approved' so local-mode enforcement is unchanged."""
    db = tmp_path / "legacy.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        # An existing rule must remain enforced (approved) after upgrade.
        conn.execute(
            "INSERT INTO memories VALUES "
            "('rule1','mandatory_rules','r','c',NULL,'active',current_timestamp)"
        )
        run_migrations(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info('memories')").fetchall()}
        assert {"created_by", "approval_status", "approved_by", "approved_at"} <= cols
        pcols = {r[1] for r in conn.execute("PRAGMA table_info('provenance')").fetchall()}
        assert "actor" in pcols
        statuses = {
            r[0] for r in conn.execute("SELECT approval_status FROM memories").fetchall()
        }
        assert statuses == {"approved"}
        idx = conn.execute(
            "SELECT count(*) FROM duckdb_indexes() WHERE index_name='idx_memories_approval'"
        ).fetchone()[0]
        assert idx == 1
    finally:
        conn.close()


def test_migration_preserves_existing_rows(tmp_path):
    db = tmp_path / "legacy.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        run_migrations(conn)
        row = conn.execute("SELECT id, title FROM memories").fetchone()
        assert row == ("m1", "t")
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "legacy.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        assert run_migrations(conn) == CURRENT_SCHEMA_VERSION
        assert run_migrations(conn) == CURRENT_SCHEMA_VERSION
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_migration_adds_v4_pending_column_defaulting_to_false(tmp_path):
    """Everything already stored was written for the project it lives in, so it
    must stay in force: only later imports are pending."""
    db = tmp_path / "legacy.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        run_migrations(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info('memories')").fetchall()}
        assert "pending" in cols
        pending = conn.execute("SELECT pending FROM memories WHERE id = 'm1'").fetchone()
        assert pending[0] is False
    finally:
        conn.close()


def test_migration_adds_v5_task_tables(tmp_path):
    """The task store appears on an existing DB, memories are untouched, and a
    second run changes nothing - tasks live in their own tables so they never
    reach the committed .claude-memory snapshot."""
    db = tmp_path / "legacy.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        assert run_migrations(conn) == CURRENT_SCHEMA_VERSION
        tables = {
            r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        assert {"tasks", "task_comments", "task_time_entries"} <= tables

        cols = {r[1] for r in conn.execute("PRAGMA table_info('tasks')").fetchall()}
        assert {
            "id", "title", "description", "state", "priority", "assignee", "labels",
            "due_at", "begin_at", "end_at", "estimated_minutes", "parent_id",
            "position", "source", "triage", "claimed_by", "claimed_at",
            "lease_expires_at", "created_at", "updated_at", "done_at",
            "archived_at", "link_id", "role",
        } == cols

        # The pre-existing memory survived the upgrade untouched.
        assert conn.execute("SELECT id, title FROM memories").fetchone() == ("m1", "t")

        # Idempotent: re-running keeps the version and does not drop the rows.
        conn.execute(
            "INSERT INTO tasks (id, title, state, source) VALUES ('t1','keep me','todo','user')"
        )
        assert run_migrations(conn) == CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    finally:
        conn.close()


def test_fresh_schema_matches_migrated_schema(tmp_path):
    """create_schema and the migration path must produce the same task tables -
    a fresh install and an upgraded one cannot be allowed to drift."""
    fresh = duckdb.connect(str(tmp_path / "fresh.duckdb"))
    migrated_path = tmp_path / "migrated.duckdb"
    _make_v1_db(migrated_path)
    migrated = duckdb.connect(str(migrated_path))
    try:
        create_schema(fresh)
        run_migrations(migrated)
        for table in ("tasks", "task_comments", "task_time_entries"):
            a = [(r[1], r[2]) for r in fresh.execute(f"PRAGMA table_info('{table}')").fetchall()]
            b = [(r[1], r[2]) for r in migrated.execute(f"PRAGMA table_info('{table}')").fetchall()]
            assert a == b, table
    finally:
        fresh.close()
        migrated.close()


def test_migration_adds_last_seen_at_backfilled_from_started_at(tmp_path):
    """v5 alters the existing sessions table. A session with no timestamp at all
    would look infinitely stale to the claim's lease check, so the backfill is
    explicit rather than left to the column DEFAULT."""
    db = tmp_path / "legacy.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        conn.execute("""
            CREATE TABLE sessions (
                id VARCHAR PRIMARY KEY, started_at TIMESTAMP NOT NULL,
                ended_at TIMESTAMP, summary VARCHAR,
                memories_created INTEGER, memories_accessed INTEGER, metadata JSON
            )
        """)
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES ('s1', current_timestamp)"
        )
        run_migrations(conn)

        cols = {r[1] for r in conn.execute("PRAGMA table_info('sessions')").fetchall()}
        assert "last_seen_at" in cols
        started, last_seen = conn.execute(
            "SELECT started_at, last_seen_at FROM sessions WHERE id = 's1'"
        ).fetchone()
        assert last_seen == started

        # Idempotent, and a re-run must not stomp a fresher heartbeat.
        conn.execute(
            "UPDATE sessions SET last_seen_at = last_seen_at + INTERVAL 5 MINUTE"
        )
        run_migrations(conn)
        assert conn.execute(
            "SELECT last_seen_at > started_at FROM sessions WHERE id = 's1'"
        ).fetchone()[0] is True
    finally:
        conn.close()


def test_migration_adds_v6_bridge_tables(tmp_path):
    """v6 adds the bridge's durable half: task_sync and task_outbox.

    A local task mutation appends to the outbox and returns, so being unable to
    reach asoode is a normal state rather than a lost edit. task_sync remembers
    which remote task a local one became, so mirroring an edit does not have to
    re-POST a create just to recover the id.
    """
    db = tmp_path / "legacy.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        run_migrations(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()}
        assert {"task_sync", "task_outbox"} <= tables

        outbox_cols = {r[1] for r in conn.execute("PRAGMA table_info('task_outbox')").fetchall()}
        assert {"id", "task_id", "link_id", "op", "payload",
                "created_at", "attempts", "last_error"} == outbox_cols

        sync_cols = {r[1] for r in conn.execute("PRAGMA table_info('task_sync')").fetchall()}
        assert {"task_id", "link_id", "remote_task_id", "remote_updated_at",
                "last_pushed_state", "updated_at"} == sync_cols
    finally:
        conn.close()


def test_v6_tables_match_between_fresh_and_migrated(tmp_path):
    """Same drift guarantee as the v5 tables: create_schema and the migration
    path build the bridge tables identically."""
    fresh = duckdb.connect(str(tmp_path / "fresh6.duckdb"))
    migrated_path = tmp_path / "migrated6.duckdb"
    _make_v1_db(migrated_path)
    migrated = duckdb.connect(str(migrated_path))
    try:
        create_schema(fresh)
        run_migrations(migrated)
        for table in ("task_sync", "task_outbox"):
            a = [(r[1], r[2]) for r in fresh.execute(f"PRAGMA table_info('{table}')").fetchall()]
            b = [(r[1], r[2]) for r in migrated.execute(f"PRAGMA table_info('{table}')").fetchall()]
            assert a == b, table
    finally:
        fresh.close()
        migrated.close()


def test_v8_rebuilds_the_outbox_without_a_primary_key(tmp_path):
    """A queue row must always be deletable.

    A live outbox reached a state where a row could be SELECTed but never
    DELETEd - DuckDB's ART index reported "Failed to delete all rows from
    index" - so the flusher retried it forever, re-posting its side effect each
    time. The constraint bought nothing: the table is addressed by id through a
    plain index.
    """
    db = tmp_path / "legacy.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        run_migrations(conn)
        ddl = conn.execute(
            "SELECT sql FROM duckdb_tables() WHERE table_name = 'task_outbox'"
        ).fetchone()[0]
        assert "PRIMARY KEY" not in ddl.upper()

        # and a row inserted then deleted must actually go
        conn.execute(
            "INSERT INTO task_outbox (id, task_id, op) VALUES ('x1', 't1', 'create')"
        )
        conn.execute("DELETE FROM task_outbox WHERE id = 'x1'")
        assert conn.execute("SELECT count(*) FROM task_outbox").fetchone()[0] == 0
    finally:
        conn.close()


def test_v8_keeps_queued_rows_through_the_rebuild(tmp_path):
    """Rebuilding the table must not silently drop pending mirrors."""
    db = tmp_path / "legacy.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO task_outbox (id, task_id, op, attempts) "
            "VALUES ('keep', 't9', 'state', 2)"
        )
        # a second migration pass is a no-op, but the rebuild path is idempotent
        from memory_mcp.db.schema import migrate_v7_to_v8

        migrate_v7_to_v8(conn)
        row = conn.execute(
            "SELECT task_id, op, attempts FROM task_outbox WHERE id = 'keep'"
        ).fetchone()
        assert row == ("t9", "state", 2)
    finally:
        conn.close()


def test_v12_adds_the_role_column_to_an_existing_tasks_table(tmp_path):
    """An agent claim needs a role to filter on; old databases must gain it."""
    db = tmp_path / "v12.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        run_migrations(conn)
        columns = {r[1] for r in conn.execute("PRAGMA table_info('tasks')").fetchall()}
        assert "role" in columns
    finally:
        conn.close()


def test_v12_leaves_existing_tasks_unroled(tmp_path):
    """The fallback the whole design rests on.

    Every task predating the column has no role, and the claim treats NULL as
    claimable by anyone. If the migration backfilled a role instead, the queue
    would go invisible to every caller at once.
    """
    db = tmp_path / "v12-rows.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO tasks (id, title, state) VALUES ('t1', 'Old task', 'todo')"
        )
        assert conn.execute("SELECT role FROM tasks WHERE id = 't1'").fetchone()[0] is None
    finally:
        conn.close()


def test_tasks_table_matches_between_fresh_and_migrated_at_v12(tmp_path):
    """The drift trap: a column added to only one of the two paths is a bug that
    appears exclusively on a machine that upgraded rather than installed."""
    fresh = duckdb.connect(str(tmp_path / "fresh12.duckdb"))
    migrated_path = tmp_path / "migrated12.duckdb"
    _make_v1_db(migrated_path)
    migrated = duckdb.connect(str(migrated_path))
    try:
        create_schema(fresh)
        run_migrations(migrated)
        a = [(r[1], r[2]) for r in fresh.execute("PRAGMA table_info('tasks')").fetchall()]
        b = [(r[1], r[2]) for r in migrated.execute("PRAGMA table_info('tasks')").fetchall()]
        assert a == b
    finally:
        fresh.close()
        migrated.close()


def test_v13_adds_session_id_to_time_entries_and_the_tombstone_table(tmp_path):
    """A clock remembers who started it, so a session end can stop exactly its
    own; a deleted task leaves a tombstone so its card cannot re-import it."""
    db = tmp_path / "v13.duckdb"
    _make_v1_db(db)
    conn = duckdb.connect(str(db))
    try:
        run_migrations(conn)
        columns = {r[1] for r in conn.execute("PRAGMA table_info('task_time_entries')").fetchall()}
        assert "session_id" in columns
        tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
        assert "task_tombstones" in tables
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION >= 13
    finally:
        conn.close()


def test_v13_tables_match_between_fresh_and_migrated(tmp_path):
    fresh = duckdb.connect(str(tmp_path / "fresh13.duckdb"))
    migrated_path = tmp_path / "migrated13.duckdb"
    _make_v1_db(migrated_path)
    migrated = duckdb.connect(str(migrated_path))
    try:
        create_schema(fresh)
        run_migrations(migrated)
        for table in ("task_time_entries", "task_tombstones"):
            a = [(r[1], r[2]) for r in fresh.execute(f"PRAGMA table_info('{table}')").fetchall()]
            b = [(r[1], r[2]) for r in migrated.execute(f"PRAGMA table_info('{table}')").fetchall()]
            assert a == b, table
    finally:
        fresh.close()
        migrated.close()
