"""Backward-compatibility tests: legacy DuckDB files upgrade in place on open."""

import duckdb

from memory_mcp.db.schema import get_schema_version, run_migrations


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
        assert version == 4
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
        assert run_migrations(conn) == 4
        assert run_migrations(conn) == 4
        assert get_schema_version(conn) == 4
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
