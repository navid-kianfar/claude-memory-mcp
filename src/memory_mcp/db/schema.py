"""Database schema creation and migration for per-project DuckDB files."""

import duckdb

CURRENT_SCHEMA_VERSION = 9


def install_vss(conn: duckdb.DuckDBPyConnection) -> None:
    """Install and load the VSS extension."""
    conn.execute("INSTALL vss;")
    conn.execute("LOAD vss;")
    conn.execute("SET hnsw_enable_experimental_persistence = true;")


# Task tables (v5). Deliberately NOT a MemoryCategory: SYNC_CATEGORIES is derived
# automatically from the category enum (constants.py), so a task category would
# immediately start writing every task into the committed .claude-memory/ JSON
# snapshot. Keeping tasks in their own tables is what keeps that snapshot small.
#
# claimed_by/claimed_at/lease_expires_at implement the multi-session claim: one
# daemon, many Claude sessions, and a task that must be picked up by exactly one
# of them. See TaskService.claim_next.
#
# `task_sync` and `task_outbox` below are the asoode bridge's durable half: a
# local mutation appends an outbox row and returns immediately, and a flusher
# drains it whenever the remote is reachable. Being unable to reach asoode is a
# normal state, not an error - the local store is always the source of truth and
# never waits on a network call.
_TASK_DDL = (
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id                VARCHAR PRIMARY KEY,
        title             VARCHAR NOT NULL,
        description       VARCHAR,
        state             VARCHAR NOT NULL DEFAULT 'todo',
        priority          INTEGER DEFAULT 0,
        assignee          VARCHAR,
        labels            VARCHAR[],
        due_at            TIMESTAMP,
        begin_at          TIMESTAMP,
        end_at            TIMESTAMP,
        estimated_minutes INTEGER,
        parent_id         VARCHAR,
        position          INTEGER DEFAULT 0,
        source            VARCHAR NOT NULL DEFAULT 'user',
        triage            BOOLEAN DEFAULT FALSE,
        claimed_by        VARCHAR,
        claimed_at        TIMESTAMP,
        lease_expires_at  TIMESTAMP,
        created_at        TIMESTAMP DEFAULT current_timestamp,
        updated_at        TIMESTAMP DEFAULT current_timestamp,
        done_at           TIMESTAMP,
        archived_at       TIMESTAMP,
        -- Which linked asoode board this task belongs to. NULL means "the
        -- project's default link" - a monorepo maps one memory project to many
        -- boards, and a task has to be able to say which app it is for.
        link_id           INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_comments (
        id         VARCHAR PRIMARY KEY,
        task_id    VARCHAR NOT NULL,
        body       VARCHAR NOT NULL,
        kind       VARCHAR NOT NULL DEFAULT 'note',
        author     VARCHAR,
        created_at TIMESTAMP DEFAULT current_timestamp
    )
    """,
    # `end` is a DuckDB reserved word and cannot be used as a column name even
    # quoted-free in a SELECT, so the clock columns are begin_at/end_at - also
    # matching the date columns on `tasks`. end_at IS NULL means "running".
    """
    CREATE TABLE IF NOT EXISTS task_time_entries (
        id       VARCHAR PRIMARY KEY,
        task_id  VARCHAR NOT NULL,
        begin_at TIMESTAMP NOT NULL,
        end_at   TIMESTAMP,
        manual   BOOLEAN DEFAULT FALSE,
        -- When this stretch was mirrored to the remote platform. A time entry
        -- has no externalRef to make the send idempotent, so "already sent" has
        -- to be remembered here or a retried flush double-counts the work.
        mirrored_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks (state)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks (source)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_triage ON tasks (triage)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_claimed ON tasks (claimed_by)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_task_comments_task ON task_comments (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_time_task ON task_time_entries (task_id)",
)


_SYNC_DDL = (
    """
    CREATE TABLE IF NOT EXISTS task_sync (
        task_id           VARCHAR NOT NULL,
        link_id           INTEGER NOT NULL,
        remote_task_id    VARCHAR,
        remote_updated_at TIMESTAMP,
        last_pushed_state VARCHAR,
        updated_at        TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (task_id, link_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_outbox (
        -- Deliberately NOT a PRIMARY KEY. This is a queue: rows are inserted and
        -- deleted constantly from more than one thread, and DuckDB's ART index
        -- got into a state where a row could be read but never deleted ("Failed
        -- to delete all rows from index"), so the flusher retried it forever and
        -- re-posted its side effect each time. The table is small and always
        -- addressed by id through the non-unique index below; the PK bought
        -- nothing and cost that.
        id         VARCHAR NOT NULL,
        task_id    VARCHAR NOT NULL,
        -- Null until a flush resolves which board the task routes to. The local
        -- store must not have to know about links to record that something
        -- changed, or every task mutation would depend on the registry.
        link_id    INTEGER,
        op         VARCHAR NOT NULL,
        payload    VARCHAR,
        created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
        attempts   INTEGER NOT NULL DEFAULT 0,
        last_error VARCHAR
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_outbox_id ON task_outbox(id)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_task ON task_outbox(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_created ON task_outbox(created_at)",
)


def create_task_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the task store. Shared by create_schema and the migrations so a
    fresh DB and a migrated one can never drift apart."""
    for ddl in (*_TASK_DDL, *_SYNC_DDL):
        try:
            conn.execute(ddl)
        except Exception:
            pass


def create_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the full schema for a project database."""
    install_vss(conn)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id              VARCHAR PRIMARY KEY,
            category        VARCHAR NOT NULL,
            title           VARCHAR NOT NULL,
            content         VARCHAR NOT NULL,
            summary         VARCHAR,
            tags            VARCHAR[],
            metadata        JSON,
            embedding       FLOAT[384],
            status          VARCHAR DEFAULT 'active',
            priority        INTEGER DEFAULT 0,
            source          VARCHAR,
            related_ids     VARCHAR[],
            entities        VARCHAR[],
            access_count    INTEGER DEFAULT 0,
            expires_at      TIMESTAMP,
            created_at      TIMESTAMP DEFAULT current_timestamp,
            updated_at      TIMESTAMP DEFAULT current_timestamp,
            created_by      VARCHAR,
            approval_status VARCHAR DEFAULT 'approved',
            approved_by     VARCHAR,
            approved_at     TIMESTAMP,
            pending         BOOLEAN DEFAULT FALSE
        )
    """)

    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_provenance_id START 1;
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS provenance (
            id              INTEGER PRIMARY KEY DEFAULT nextval('seq_provenance_id'),
            memory_id       VARCHAR NOT NULL,
            operation       VARCHAR NOT NULL,
            details         JSON,
            actor           VARCHAR,
            created_at      TIMESTAMP DEFAULT current_timestamp
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id                VARCHAR PRIMARY KEY,
            started_at        TIMESTAMP NOT NULL,
            ended_at          TIMESTAMP,
            summary           VARCHAR,
            memories_created  INTEGER DEFAULT 0,
            memories_accessed INTEGER DEFAULT 0,
            metadata          JSON,
            last_seen_at      TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    # Indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_category ON memories (category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_status ON memories (status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_approval ON memories (approval_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_pending ON memories (pending)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_created ON memories (created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_expires ON memories (expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_provenance_memory ON provenance (memory_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_provenance_op ON provenance (operation)")

    create_task_tables(conn)

    # Record schema version
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
        [CURRENT_SCHEMA_VERSION],
    )


def migrate_v1_to_v2(conn: duckdb.DuckDBPyConnection) -> None:
    """Migrate from schema v1 to v2: add summary, entities, expires_at, provenance."""
    try:
        conn.execute("ALTER TABLE memories ADD COLUMN summary VARCHAR")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE memories ADD COLUMN entities VARCHAR[]")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE memories ADD COLUMN expires_at TIMESTAMP")
    except Exception:
        pass
    try:
        conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_provenance_id START 1;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS provenance (
                id              INTEGER PRIMARY KEY DEFAULT nextval('seq_provenance_id'),
                memory_id       VARCHAR NOT NULL,
                operation       VARCHAR NOT NULL,
                details         JSON,
                created_at      TIMESTAMP DEFAULT current_timestamp
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_provenance_memory ON provenance (memory_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_provenance_op ON provenance (operation)")
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_expires ON memories (expires_at)")
    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (2)")


def migrate_v2_to_v3(conn: duckdb.DuckDBPyConnection) -> None:
    """Migrate v2 -> v3: rule approval lifecycle + provenance actor.

    Adds the columns the admin approval workflow needs. Existing rows must keep
    behaving exactly as before, so every rule is backfilled to 'approved' - in
    local mode (and for any pre-existing rule) 'approved' means "enforced as
    today". DuckDB's ADD COLUMN cannot be guarded with IF NOT EXISTS, so each
    ALTER is wrapped in try/except for idempotency, and we never rely on the
    column DEFAULT to backfill existing rows (that behavior is version-sensitive)
    - an explicit UPDATE guarantees it.
    """
    for ddl in (
        "ALTER TABLE memories ADD COLUMN created_by VARCHAR",
        "ALTER TABLE memories ADD COLUMN approval_status VARCHAR DEFAULT 'approved'",
        "ALTER TABLE memories ADD COLUMN approved_by VARCHAR",
        "ALTER TABLE memories ADD COLUMN approved_at TIMESTAMP",
        "ALTER TABLE provenance ADD COLUMN actor VARCHAR",
    ):
        try:
            conn.execute(ddl)
        except Exception:
            pass
    try:
        conn.execute(
            "UPDATE memories SET approval_status = 'approved' "
            "WHERE approval_status IS NULL"
        )
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_approval ON memories (approval_status)")
    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (3)")


def migrate_v3_to_v4(conn: duckdb.DuckDBPyConnection) -> None:
    """Migrate v3 -> v4: `pending`, the import-adaptation gate.

    A memory copied in from another project starts pending: it is stored, but
    kept out of the rule block, search, session context and the git snapshot
    until an agent has rewritten it for THIS project. Every pre-existing memory
    was written for the project it lives in, so all of them backfill to FALSE -
    behavior is unchanged for anything already stored.
    """
    try:
        conn.execute("ALTER TABLE memories ADD COLUMN pending BOOLEAN DEFAULT FALSE")
    except Exception:
        pass
    try:
        conn.execute("UPDATE memories SET pending = FALSE WHERE pending IS NULL")
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_pending ON memories (pending)")
    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (4)")


def migrate_v4_to_v5(conn: duckdb.DuckDBPyConnection) -> None:
    """Migrate v4 -> v5: the task store.

    Three new tables plus one column on `sessions`. `memories` is untouched, so
    every existing memory keeps behaving exactly as before. Tasks are queued
    requirements with their own lifecycle (state, comments, time entries); they
    are NOT memories and NOT a MemoryCategory, which is what keeps them out of
    the committed .claude-memory/ snapshot.

    `sessions.last_seen_at` is the free heartbeat behind the multi-session claim
    - every tool call already reaches the daemon. Existing sessions are
    backfilled explicitly from started_at rather than left to the column
    DEFAULT, which is version-sensitive: a session with no timestamp at all
    would look infinitely stale.
    """
    create_task_tables(conn)
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_seen_at TIMESTAMP")
    except Exception:
        pass
    try:
        conn.execute(
            "UPDATE sessions SET last_seen_at = started_at WHERE last_seen_at IS NULL"
        )
    except Exception:
        pass
    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (5)")


def migrate_v5_to_v6(conn: duckdb.DuckDBPyConnection) -> None:
    """Migrate v5 -> v6: the bridge's outbox and remote-id map.

    `task_sync` remembers which remote task a local one became, so mirroring an
    edit does not have to re-POST a create just to recover the id. `task_outbox`
    holds mutations not yet mirrored, so a task edited while asoode is unreachable
    is mirrored later rather than lost.

    Both are created by create_task_tables, so a fresh v6 DB and a migrated v5 one
    are identical. Existing tasks get no task_sync rows: the first flush after
    this creates them, and a create with the task's externalRef returns whatever
    already exists rather than duplicating it.
    """
    create_task_tables(conn)
    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (6)")


def migrate_v6_to_v7(conn: duckdb.DuckDBPyConnection) -> None:
    """Migrate v6 -> v7: a task can name the board it belongs to.

    One memory project maps to MANY asoode work packages - a monorepo has one per
    app - so a task needs to say which. Nullable on purpose: every task that
    existed before this column routes to the project's default link, which is
    what keeps single-board projects working unchanged.
    """
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN link_id INTEGER")
    except Exception:
        pass
    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (7)")


def migrate_v7_to_v8(conn: duckdb.DuckDBPyConnection) -> None:
    """Migrate v7 -> v8: rebuild task_outbox without its PRIMARY KEY.

    A live outbox reached a state where a row could be SELECTed but never
    DELETEd - DuckDB's ART index reported "Failed to delete all rows from index.
    Only deleted 0 out of 1 rows." The flusher therefore retried that row on
    every pass, re-posting its comment each time, because the remote call
    succeeded and only the local cleanup failed.

    Rebuilding drops the index that could not be repaired and takes the
    constraint off for good: this is a queue addressed by id through a plain
    index, so uniqueness was never enforcing anything the code relied on. Rows
    that survive the copy keep their place in the queue.
    """
    try:
        existing = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()}
        if "task_outbox" not in existing:
            return
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_outbox_v8 (
                id         VARCHAR NOT NULL,
                task_id    VARCHAR NOT NULL,
                link_id    INTEGER,
                op         VARCHAR NOT NULL,
                payload    VARCHAR,
                created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
                attempts   INTEGER NOT NULL DEFAULT 0,
                last_error VARCHAR
            )
        """)
        conn.execute("""
            INSERT INTO task_outbox_v8
            SELECT id, task_id, link_id, op, payload, created_at, attempts, last_error
            FROM task_outbox
        """)
        conn.execute("DROP TABLE task_outbox")
        conn.execute("ALTER TABLE task_outbox_v8 RENAME TO task_outbox")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_id ON task_outbox(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_task ON task_outbox(task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_created ON task_outbox(created_at)")
    except Exception:
        # A rebuild that fails must not stop the DB opening: the outbox is a
        # queue, not the record. create_task_tables recreates it if it vanished.
        pass
    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (8)")


def migrate_v8_to_v9(conn: duckdb.DuckDBPyConnection) -> None:
    """Migrate v8 -> v9: remember which time entries have been mirrored.

    Time spent was recorded locally and never sent, so every task on the board
    read 0 minutes. Sending it needs a guard the other mirrors get for free: a
    task carries an externalRef, so re-sending it returns the existing row, but a
    time entry has no such key and a retried flush would add the hours twice.
    `mirrored_at` is that guard. NULL on every existing row, which is correct -
    none of them has been sent.
    """
    try:
        conn.execute("ALTER TABLE task_time_entries ADD COLUMN mirrored_at TIMESTAMP")
    except Exception:
        pass
    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (9)")


def get_schema_version(conn: duckdb.DuckDBPyConnection) -> int:
    """Return the schema version of this DB. A missing table means a legacy v1 DB."""
    try:
        row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        pass
    return 1


def run_migrations(conn: duckdb.DuckDBPyConnection) -> int:
    """Apply pending migrations to an existing DB. Returns the resulting version.

    Idempotent: safe to call on every connection open. Legacy DBs created before
    the schema_version table existed are treated as v1 and upgraded in place.
    """
    install_vss(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT current_timestamp
        )
    """)
    version = get_schema_version(conn)
    if version < 2:
        migrate_v1_to_v2(conn)
        version = 2
    if version < 3:
        migrate_v2_to_v3(conn)
        version = 3
    if version < 4:
        migrate_v3_to_v4(conn)
        version = 4
    if version < 5:
        migrate_v4_to_v5(conn)
        version = 5
    if version < 6:
        migrate_v5_to_v6(conn)
        version = 6
    if version < 7:
        migrate_v6_to_v7(conn)
        version = 7
    if version < 8:
        migrate_v7_to_v8(conn)
        version = 8
    if version < 9:
        migrate_v8_to_v9(conn)
        version = 9
    return version


def create_hnsw_index(conn: duckdb.DuckDBPyConnection) -> None:
    """Create or recreate HNSW vector index with cosine metric."""
    try:
        # Check if index exists
        indexes = conn.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'memories' AND index_name = 'idx_memories_embedding'"
        ).fetchall()

        if indexes:
            # Drop and recreate to ensure correct metric
            conn.execute("DROP INDEX IF EXISTS idx_memories_embedding")

        # Check if there are any rows with embeddings
        count = conn.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL").fetchone()[0]
        if count > 0:
            conn.execute("""
                CREATE INDEX idx_memories_embedding
                ON memories USING HNSW (embedding)
                WITH (metric = 'cosine')
            """)
    except Exception:
        # HNSW index is optional - search works without it (brute force)
        pass
