"""Task repository - all SQL for tasks, their comments, and their time entries.

Tasks live in the per-project DuckDB alongside memories but in their own tables:
they are not a MemoryCategory, so they never reach the git-committed
.claude-memory/ snapshot however long the list gets.
"""

from datetime import datetime

from memory_mcp.db.connection import connect
from memory_mcp.models import Task, TaskComment, TaskFilter, TaskTimeEntry

# Column order is load-bearing: every read uses this list and _row_to_task maps
# by position. Append new columns AT THE END so existing indices stay valid.
TASK_COLUMNS = (
    "id, title, description, state, priority, assignee, labels, due_at, "
    "begin_at, end_at, estimated_minutes, parent_id, position, source, triage, "
    "claimed_by, claimed_at, lease_expires_at, "
    "created_at, updated_at, done_at, archived_at, link_id"
)

# A claim is free if nobody holds it, or if the holder's lease has run out. The
# lease is checked lazily, right here, so no sweeper thread is needed: a crashed
# session's task simply becomes claimable again once its lease expires.
CLAIMABLE_SQL = (
    "(claimed_by IS NULL OR lease_expires_at IS NULL "
    "OR lease_expires_at < current_timestamp::TIMESTAMP)"
)

COMMENT_COLUMNS = "id, task_id, body, kind, author, created_at"
TIME_ENTRY_COLUMNS = "id, task_id, begin_at, end_at, manual"

# What is still waiting. Mirrors OPEN_TASK_STATES in models.py; kept as a SQL
# literal so the ordering below and this filter can never disagree.
OPEN_STATES_SQL = "('todo', 'in_progress', 'paused', 'blocked', 'blocker', 'incomplete')"

# Reading order for a queue: what is underway, then what is next, then what is
# stuck, and finished work last. Alphabetical state ordering would scatter these.
STATE_ORDER_SQL = """
    CASE state
        WHEN 'in_progress' THEN 0
        WHEN 'todo'        THEN 1
        WHEN 'blocker'     THEN 2
        WHEN 'blocked'     THEN 3
        WHEN 'paused'      THEN 4
        WHEN 'incomplete'  THEN 5
        ELSE 6
    END
"""

# Within a state, `position` wins: the list is drag-orderable, so the order the
# user put things in has to survive. Priority stays a column, not a sort key -
# otherwise dragging a task past a higher-priority one would silently snap back.
_ORDER_BY = f"ORDER BY {STATE_ORDER_SQL}, position ASC, created_at ASC"


def _row_to_task(row) -> Task:
    return Task(
        id=row[0],
        title=row[1],
        description=row[2],
        state=row[3],
        priority=row[4] if row[4] is not None else 0,
        assignee=row[5],
        labels=row[6] or [],
        due_at=row[7],
        begin_at=row[8],
        end_at=row[9],
        estimated_minutes=row[10],
        parent_id=row[11],
        position=row[12] if row[12] is not None else 0,
        source=row[13] or "user",
        triage=bool(row[14]) if row[14] is not None else False,
        claimed_by=row[15],
        claimed_at=row[16],
        lease_expires_at=row[17],
        created_at=row[18],
        updated_at=row[19],
        done_at=row[20],
        archived_at=row[21],
        link_id=row[22],
    )


def _row_to_comment(row) -> TaskComment:
    return TaskComment(
        id=row[0], task_id=row[1], body=row[2],
        kind=row[3] or "note", author=row[4], created_at=row[5],
    )


def _row_to_entry(row) -> TaskTimeEntry:
    return TaskTimeEntry(
        id=row[0], task_id=row[1], begin_at=row[2], end_at=row[3],
        manual=bool(row[4]) if row[4] is not None else False,
    )


class TaskRepository:
    """All task-related SQL operations, centralized."""

    # ---------- Insert ----------

    def insert(
        self,
        project: str,
        task_id: str,
        title: str,
        description: str | None,
        state: str,
        priority: int,
        assignee: str | None,
        labels: list[str],
        due_at: datetime | None,
        begin_at: datetime | None,
        end_at: datetime | None,
        estimated_minutes: int | None,
        parent_id: str | None,
        position: int,
        source: str,
        link_id: int | None = None,
    ) -> Task:
        with connect(project) as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, title, description, state, priority, assignee, labels, due_at, begin_at, end_at, estimated_minutes, parent_id, position, source, link_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    task_id, title, description, state, priority, assignee, labels,
                    due_at, begin_at, end_at, estimated_minutes, parent_id,
                    position, source, link_id,
                ],
            )
            row = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks WHERE id = ?", [task_id]
            ).fetchone()
        return _row_to_task(row)

    # ---------- Read ----------

    def get(self, project: str, task_id: str) -> Task | None:
        with connect(project) as conn:
            row = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks WHERE id = ?", [task_id]
            ).fetchone()
        return _row_to_task(row) if row else None

    def list_tasks(
        self, project: str, filters: TaskFilter, limit: int = 50, offset: int = 0,
    ) -> tuple[list[Task], int, int]:
        """Return (page, total_matching, open_matching) for the given filters."""
        conditions: list[str] = []
        params: list = []

        if filters.state is not None:
            conditions.append("state = ?")
            params.append(filters.state.value)
        elif not filters.include_done:
            # "Done" here means finished or withdrawn: done, cancelled, duplicate.
            conditions.append(f"state IN {OPEN_STATES_SQL}")

        if filters.source:
            conditions.append("source = ?")
            params.append(filters.source)

        if filters.parent_id is not None:
            conditions.append("parent_id = ?")
            params.append(filters.parent_id)
        elif not filters.include_subtasks:
            conditions.append("parent_id IS NULL")

        if not filters.include_archived:
            conditions.append("archived_at IS NULL")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        # Same filters, narrowed to what is still waiting - so a caller paging
        # through a filtered list still learns how much of it is outstanding.
        open_where = " AND ".join(
            conditions + [f"state IN {OPEN_STATES_SQL}", "archived_at IS NULL"]
        )

        with connect(project) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM tasks {where}", params
            ).fetchone()[0]
            open_count = conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE {open_where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks {where} {_ORDER_BY} LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()

        return [_row_to_task(r) for r in rows], total, open_count

    def open_tasks(self, project: str, limit: int | None = None) -> list[Task]:
        """Every top-level task still waiting, in reading order. Sub-tasks are
        part of their parent's work, so they are counted there rather than
        listed again beside it.

        `limit=None` returns them all, for the same reason rules and session
        context have no cap: a top-N sample silently drops requirements the user
        parked, and nothing downstream can tell that anything is missing.
        """
        clause = "LIMIT ?" if limit is not None else ""
        params: list = [limit] if limit is not None else []
        with connect(project) as conn:
            rows = conn.execute(
                f"""
                SELECT {TASK_COLUMNS} FROM tasks
                WHERE state IN {OPEN_STATES_SQL} AND archived_at IS NULL
                  AND parent_id IS NULL
                {_ORDER_BY}
                {clause}
                """,
                params,
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def children_of(self, project: str, parent_id: str) -> list[Task]:
        """Sub-tasks of a task, in reading order."""
        with connect(project) as conn:
            rows = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks "
                f"WHERE parent_id = ? AND archived_at IS NULL {_ORDER_BY}",
                [parent_id],
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def count_open(self, project: str) -> int:
        with connect(project) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM tasks "
                f"WHERE state IN {OPEN_STATES_SQL} AND archived_at IS NULL "
                f"AND parent_id IS NULL"
            ).fetchone()
        return int(row[0]) if row else 0

    def next_position(self, project: str, parent_id: str | None) -> int:
        """Append position, scoped to the parent so sub-task ordering is its own."""
        with connect(project) as conn:
            if parent_id is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(position), -1) FROM tasks WHERE parent_id IS NULL"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(MAX(position), -1) FROM tasks WHERE parent_id = ?",
                    [parent_id],
                ).fetchone()
        return int(row[0]) + 1

    # ---------- Update ----------

    def update(self, project: str, task_id: str, fields: dict) -> Task:
        """Apply field updates. Keys map to column names; updated_at is automatic."""
        if not fields:
            return self.get(project, task_id)

        set_parts = [f"{k} = ?" for k in fields]
        set_parts.append("updated_at = current_timestamp")
        values = list(fields.values()) + [task_id]

        with connect(project) as conn:
            conn.execute(
                f"UPDATE tasks SET {', '.join(set_parts)} WHERE id = ?", values,
            )
            row = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks WHERE id = ?", [task_id]
            ).fetchone()
        return _row_to_task(row)

    def set_positions(self, project: str, ordered_ids: list[str]) -> int:
        """Write a manual order: position becomes the index in `ordered_ids`.

        One connection for the whole batch, so a drag lands as a single unit
        rather than as a row-at-a-time sequence a reader could see halfway.
        """
        if not ordered_ids:
            return 0
        with connect(project) as conn:
            for index, task_id in enumerate(ordered_ids):
                conn.execute(
                    "UPDATE tasks SET position = ?, updated_at = current_timestamp "
                    "WHERE id = ?",
                    [index, task_id],
                )
        return len(ordered_ids)

    def mark_done(self, project: str, task_id: str, state: str) -> Task:
        """Close a task, stamping done_at from the DB clock."""
        with connect(project) as conn:
            conn.execute(
                "UPDATE tasks SET state = ?, done_at = current_timestamp, "
                "updated_at = current_timestamp WHERE id = ?",
                [state, task_id],
            )
            row = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks WHERE id = ?", [task_id]
            ).fetchone()
        return _row_to_task(row)

    def set_parent(self, project: str, task_id: str, parent_id: str | None) -> Task:
        """Re-parent a task. `None` promotes a sub-task to a task of its own."""
        with connect(project) as conn:
            conn.execute(
                "UPDATE tasks SET parent_id = ?, updated_at = current_timestamp "
                "WHERE id = ?",
                [parent_id, task_id],
            )
            row = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks WHERE id = ?", [task_id]
            ).fetchone()
        return _row_to_task(row)

    def hard_delete(self, project: str, task_id: str) -> None:
        """Remove a task for good, with its comments and time entries.

        Sub-tasks are promoted rather than deleted with it: losing a parent must
        never silently take work down with it. Archiving stays the reversible
        option; this is the one that actually forgets.
        """
        with connect(project) as conn:
            conn.execute(
                "UPDATE tasks SET parent_id = NULL, updated_at = current_timestamp "
                "WHERE parent_id = ?",
                [task_id],
            )
            conn.execute("DELETE FROM task_comments WHERE task_id = ?", [task_id])
            conn.execute("DELETE FROM task_time_entries WHERE task_id = ?", [task_id])
            conn.execute("DELETE FROM tasks WHERE id = ?", [task_id])

    def archive(self, project: str, task_id: str) -> Task:
        """Take a task out of the list without deleting it. Nothing is ever
        hard-deleted here, matching the memory store's soft-delete."""
        with connect(project) as conn:
            conn.execute(
                "UPDATE tasks SET archived_at = current_timestamp, "
                "updated_at = current_timestamp WHERE id = ?",
                [task_id],
            )
            row = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks WHERE id = ?", [task_id]
            ).fetchone()
        return _row_to_task(row)

    # ---------- claims ----------
    #
    # One daemon, many Claude sessions. Every method here decides ownership in a
    # single conditional UPDATE whose ROWCOUNT is the answer - 1 means you got
    # it, 0 means someone else did - so two sessions racing on the same row can
    # never both win. TaskService serializes callers per project on top of this,
    # because DuckDB is single-writer and this repo opens a connection per
    # operation.

    def claim(
        self, project: str, task_id: str, session_id: str, ttl_minutes: int,
    ) -> bool:
        """Try to take one task. True when this caller got it."""
        with connect(project) as conn:
            row = conn.execute(
                f"""
                UPDATE tasks
                SET claimed_by = ?,
                    claimed_at = current_timestamp::TIMESTAMP,
                    lease_expires_at = (current_timestamp + INTERVAL (?) MINUTE)::TIMESTAMP,
                    updated_at = current_timestamp
                WHERE id = ? AND {CLAIMABLE_SQL}
                """,
                [session_id, ttl_minutes, task_id],
            ).fetchone()
        return bool(row and row[0])

    def next_claimable(self, project: str) -> Task | None:
        """The task a session should be offered next: waiting, not archived, and
        either unclaimed or held on an expired lease. Reading order, so the most
        urgent thing comes first."""
        with connect(project) as conn:
            row = conn.execute(
                f"""
                SELECT {TASK_COLUMNS} FROM tasks
                WHERE state IN {OPEN_STATES_SQL} AND archived_at IS NULL
                  AND parent_id IS NULL AND {CLAIMABLE_SQL}
                {_ORDER_BY}
                LIMIT 1
                """
            ).fetchone()
        return _row_to_task(row) if row else None

    def release(self, project: str, task_id: str, session_id: str | None) -> bool:
        """Hand a claim back. With a session_id, only that session's own claim is
        released - one session can never drop another's."""
        clause = "" if session_id is None else " AND claimed_by = ?"
        params: list = [task_id] + ([] if session_id is None else [session_id])
        with connect(project) as conn:
            row = conn.execute(
                f"""
                UPDATE tasks
                SET claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL,
                    updated_at = current_timestamp
                WHERE id = ?{clause}
                """,
                params,
            ).fetchone()
        return bool(row and row[0])

    def release_session(self, project: str, session_id: str) -> int:
        """Release every claim a session holds. Called when it ends."""
        with connect(project) as conn:
            row = conn.execute(
                """
                UPDATE tasks
                SET claimed_by = NULL, claimed_at = NULL, lease_expires_at = NULL,
                    updated_at = current_timestamp
                WHERE claimed_by = ?
                """,
                [session_id],
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def claimed_by_session(self, project: str, session_id: str) -> list[Task]:
        with connect(project) as conn:
            rows = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks WHERE claimed_by = ? {_ORDER_BY}",
                [session_id],
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def extend_lease(self, project: str, task_id: str, ttl_minutes: int) -> None:
        """Push a held task's lease out. Called on any mutation of that task, so
        work in progress keeps its claim without a heartbeat protocol."""
        with connect(project) as conn:
            conn.execute(
                "UPDATE tasks SET lease_expires_at = "
                "(current_timestamp + INTERVAL (?) MINUTE)::TIMESTAMP "
                "WHERE id = ? AND claimed_by IS NOT NULL",
                [ttl_minutes, task_id],
            )

    # ---------- Comments ----------

    def add_comment(
        self,
        project: str,
        comment_id: str,
        task_id: str,
        body: str,
        kind: str,
        author: str | None,
    ) -> TaskComment:
        with connect(project) as conn:
            conn.execute(
                "INSERT INTO task_comments (id, task_id, body, kind, author) "
                "VALUES (?, ?, ?, ?, ?)",
                [comment_id, task_id, body, kind, author],
            )
            row = conn.execute(
                f"SELECT {COMMENT_COLUMNS} FROM task_comments WHERE id = ?",
                [comment_id],
            ).fetchone()
        return _row_to_comment(row)

    def comments_for(self, project: str, task_id: str) -> list[TaskComment]:
        with connect(project) as conn:
            rows = conn.execute(
                f"SELECT {COMMENT_COLUMNS} FROM task_comments "
                f"WHERE task_id = ? ORDER BY created_at ASC",
                [task_id],
            ).fetchall()
        return [_row_to_comment(r) for r in rows]

    # ---------- Time entries ----------

    def start_entry(self, project: str, entry_id: str, task_id: str) -> TaskTimeEntry:
        """Open a running entry (end_at NULL) from the DB clock. `manual` stays
        FALSE: it marks a stretch typed in by hand rather than clocked, which
        nothing does yet."""
        with connect(project) as conn:
            conn.execute(
                "INSERT INTO task_time_entries (id, task_id, begin_at, manual) "
                "VALUES (?, ?, current_timestamp, FALSE)",
                [entry_id, task_id],
            )
            row = conn.execute(
                f"SELECT {TIME_ENTRY_COLUMNS} FROM task_time_entries WHERE id = ?",
                [entry_id],
            ).fetchone()
        return _row_to_entry(row)

    def running_entry(self, project: str, task_id: str) -> TaskTimeEntry | None:
        with connect(project) as conn:
            row = conn.execute(
                f"SELECT {TIME_ENTRY_COLUMNS} FROM task_time_entries "
                f"WHERE task_id = ? AND end_at IS NULL ORDER BY begin_at DESC LIMIT 1",
                [task_id],
            ).fetchone()
        return _row_to_entry(row) if row else None

    def stop_entry(self, project: str, entry_id: str) -> TaskTimeEntry:
        with connect(project) as conn:
            conn.execute(
                "UPDATE task_time_entries SET end_at = current_timestamp "
                "WHERE id = ? AND end_at IS NULL",
                [entry_id],
            )
            row = conn.execute(
                f"SELECT {TIME_ENTRY_COLUMNS} FROM task_time_entries WHERE id = ?",
                [entry_id],
            ).fetchone()
        return _row_to_entry(row)

    def list_meta(self, project: str) -> dict[str, dict]:
        """Per-task row metadata for the list view, in four grouped queries.

        The list shows comment counts, sub-task progress, tracked time and
        whether a clock is running - none of which live on the `tasks` row, and
        none of which should cost a query per task.
        """
        meta: dict[str, dict] = {}

        def slot(task_id: str) -> dict:
            return meta.setdefault(
                task_id,
                {
                    "comments": 0, "subtasks_total": 0, "subtasks_done": 0,
                    "minutes_spent": 0, "running": False,
                },
            )

        with connect(project) as conn:
            for task_id, count in conn.execute(
                "SELECT task_id, COUNT(*) FROM task_comments GROUP BY task_id"
            ).fetchall():
                slot(task_id)["comments"] = int(count)

            for parent_id, total, done in conn.execute(
                """
                SELECT parent_id, COUNT(*),
                       SUM(CASE WHEN state = 'done' THEN 1 ELSE 0 END)
                FROM tasks WHERE parent_id IS NOT NULL AND archived_at IS NULL
                GROUP BY parent_id
                """
            ).fetchall():
                entry = slot(parent_id)
                entry["subtasks_total"] = int(total)
                entry["subtasks_done"] = int(done or 0)

            for task_id, seconds in conn.execute(
                """
                SELECT task_id, COALESCE(SUM(date_diff('second', begin_at,
                       COALESCE(end_at, current_timestamp::TIMESTAMP))), 0)
                FROM task_time_entries GROUP BY task_id
                """
            ).fetchall():
                slot(task_id)["minutes_spent"] = int(seconds or 0) // 60

            for (task_id,) in conn.execute(
                "SELECT DISTINCT task_id FROM task_time_entries WHERE end_at IS NULL"
            ).fetchall():
                slot(task_id)["running"] = True

        return meta

    def running_task_ids(self, project: str) -> list[str]:
        """Ids of every task with an open time entry.

        The list view needs this: whether a clock is running is not derivable
        from `state`, because stopping the clock deliberately leaves the state
        alone. Without it the Start/Stop button in the UI would have to guess.
        """
        with connect(project) as conn:
            rows = conn.execute(
                "SELECT DISTINCT task_id FROM task_time_entries WHERE end_at IS NULL"
            ).fetchall()
        return [r[0] for r in rows]

    def entries_for(self, project: str, task_id: str) -> list[TaskTimeEntry]:
        with connect(project) as conn:
            rows = conn.execute(
                f"SELECT {TIME_ENTRY_COLUMNS} FROM task_time_entries "
                f"WHERE task_id = ? ORDER BY begin_at ASC",
                [task_id],
            ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def seconds_spent(self, project: str, task_id: str) -> int:
        """Total clocked seconds, counting a running entry up to now."""
        with connect(project) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(date_diff('second', begin_at,
                                   COALESCE(end_at, current_timestamp::TIMESTAMP))), 0)
                FROM task_time_entries WHERE task_id = ?
                """,
                [task_id],
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0


# A row that keeps failing must eventually be given up on. The failure that
# forced this posted its comment remotely and then failed the local cleanup, so
# every retry duplicated the comment - an unbounded retry is not "eventually
# consistent", it is a loop with a side effect.
MAX_OUTBOX_ATTEMPTS = 5


class OutboxRepository:
    """The bridge's durable half: what changed locally and has not been mirrored.

    Deliberately separate from TaskRepository and deliberately ignorant of asoode:
    it records that a task changed, never where it should go. The bridge resolves
    the board at flush time, so a task mutation cannot depend on the registry, a
    network, or a credential being present.
    """

    def enqueue(self, project: str, task_id: str, op: str, payload: dict | None = None) -> str:
        """Record a mutation to mirror. Returns the outbox row id.

        Never raises into a caller's transaction: failing to record a mirror must
        not fail the local edit that is the actual source of truth.
        """
        import json
        import uuid

        row_id = str(uuid.uuid4())
        try:
            with connect(project) as conn:
                conn.execute(
                    "INSERT INTO task_outbox (id, task_id, op, payload) VALUES (?, ?, ?, ?)",
                    [row_id, task_id, op, json.dumps(payload or {})],
                )
        except Exception:
            return ""
        return row_id

    def pending(self, project: str, limit: int = 200) -> list[dict]:
        """Un-mirrored mutations, oldest first - order matters per task."""
        import json

        try:
            with connect(project) as conn:
                rows = conn.execute(
                    "SELECT id, task_id, op, payload, attempts, last_error "
                    "FROM task_outbox ORDER BY created_at ASC, rowid ASC LIMIT ?",
                    [limit],
                ).fetchall()
        except Exception:
            return []
        return [
            {
                "id": r[0], "task_id": r[1], "op": r[2],
                "payload": json.loads(r[3]) if r[3] else {},
                "attempts": r[4], "last_error": r[5],
            }
            for r in rows
        ]

    def resolve(self, project: str, row_id: str) -> None:
        """Mirrored successfully - drop the row."""
        with connect(project) as conn:
            conn.execute("DELETE FROM task_outbox WHERE id = ?", [row_id])

    def fail(self, project: str, row_id: str, error: str) -> bool:
        """Mirroring failed. Returns True if the row was given up on.

        The row stays so the next flush retries it - until MAX_OUTBOX_ATTEMPTS,
        after which it is dropped. Retrying forever is worse than losing one
        mirror: the call that failed may have already had its effect remotely,
        so each retry repeats it.
        """
        with connect(project) as conn:
            conn.execute(
                "UPDATE task_outbox SET attempts = attempts + 1, last_error = ? "
                "WHERE id = ?",
                [error[:500], row_id],
            )
            row = conn.execute(
                "SELECT attempts FROM task_outbox WHERE id = ?", [row_id]
            ).fetchone()
            if row and row[0] >= MAX_OUTBOX_ATTEMPTS:
                conn.execute("DELETE FROM task_outbox WHERE id = ?", [row_id])
                return True
        return False

    def depth(self, project: str) -> int:
        try:
            with connect(project) as conn:
                return conn.execute("SELECT count(*) FROM task_outbox").fetchone()[0]
        except Exception:
            return 0

    def unmirrored_comments(self, project: str, task_id: str) -> list[dict]:
        """Comments for a task that have not been sent yet, oldest first."""
        try:
            with connect(project) as conn:
                rows = conn.execute(
                    "SELECT id, body FROM task_comments "
                    "WHERE task_id = ? AND mirrored_at IS NULL "
                    "ORDER BY created_at ASC",
                    [task_id],
                ).fetchall()
        except Exception:
            return []
        return [{"id": r[0], "body": r[1]} for r in rows]

    def mark_comment_mirrored(self, project: str, comment_id: str) -> None:
        from datetime import datetime, timezone

        with connect(project) as conn:
            conn.execute(
                "UPDATE task_comments SET mirrored_at = ? WHERE id = ?",
                [datetime.now(timezone.utc), comment_id],
            )

    def unmirrored_time(self, project: str, task_id: str) -> list[dict]:
        """Closed time entries for a task that have not been sent yet.

        Closed only: an open stretch has no duration to report, and sending it
        would mean correcting the remote later. Unmirrored only: a time entry has
        no externalRef, so re-sending one double-counts the work.
        """
        try:
            with connect(project) as conn:
                rows = conn.execute(
                    "SELECT id, begin_at, end_at FROM task_time_entries "
                    "WHERE task_id = ? AND end_at IS NOT NULL AND mirrored_at IS NULL "
                    "ORDER BY begin_at ASC",
                    [task_id],
                ).fetchall()
        except Exception:
            return []
        return [{"id": r[0], "begin_at": r[1], "end_at": r[2]} for r in rows]

    def mark_time_mirrored(self, project: str, entry_id: str) -> None:
        from datetime import datetime, timezone

        with connect(project) as conn:
            conn.execute(
                "UPDATE task_time_entries SET mirrored_at = ? WHERE id = ?",
                [datetime.now(timezone.utc), entry_id],
            )

    # ---------- the local task -> remote task map ----------

    def remote_id(self, project: str, task_id: str, link_id: int) -> str | None:
        try:
            with connect(project) as conn:
                row = conn.execute(
                    "SELECT remote_task_id FROM task_sync WHERE task_id = ? AND link_id = ?",
                    [task_id, link_id],
                ).fetchone()
        except Exception:
            return None
        return row[0] if row else None

    def local_id_for_remote(self, project: str, link_id: int, remote_task_id: str) -> str | None:
        """The local task a remote one maps to, if it has been seen before.

        The import's identity key. asoode-native tasks carry no externalRef, so
        the remote id is the only stable handle - which is why the mapping has to
        be stored rather than derived.
        """
        try:
            with connect(project) as conn:
                row = conn.execute(
                    "SELECT task_id FROM task_sync WHERE link_id = ? AND remote_task_id = ?",
                    [link_id, remote_task_id],
                ).fetchone()
        except Exception:
            return None
        return row[0] if row else None

    def remember(
        self, project: str, task_id: str, link_id: int, remote_task_id: str,
        last_pushed_state: str | None = None,
    ) -> None:
        """Remember which remote task a local one became, so mirroring an edit
        never has to re-POST a create just to recover the id."""
        # The timestamp is a bound parameter, not the `current_timestamp` keyword:
        # inside an ON CONFLICT clause DuckDB binds a bare identifier against the
        # target table and fails with "no column named current_timestamp".
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        with connect(project) as conn:
            conn.execute(
                "INSERT INTO task_sync (task_id, link_id, remote_task_id, "
                "last_pushed_state, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (task_id, link_id) DO UPDATE SET "
                "remote_task_id = excluded.remote_task_id, "
                "last_pushed_state = excluded.last_pushed_state, "
                "updated_at = excluded.updated_at",
                [task_id, link_id, remote_task_id, last_pushed_state, now],
            )
