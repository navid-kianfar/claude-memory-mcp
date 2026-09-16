"""Task repository - all SQL for tasks, their comments, and their time entries.

Tasks live in the per-project DuckDB alongside memories but in their own tables:
they are not a MemoryCategory, so they never reach the git-committed
.claude-memory/ snapshot however long the list gets.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from memory_mcp.db.connection import connect
from memory_mcp.models import (
    PendingAttachment, Task, TaskAttachment, TaskComment, TaskFilter, TaskTimeEntry,
)

# Column order is load-bearing: every read uses this list and _row_to_task maps
# by position. Append new columns AT THE END so existing indices stay valid.
TASK_COLUMNS = (
    "id, title, description, state, priority, assignee, labels, due_at, "
    "begin_at, end_at, estimated_minutes, parent_id, position, source, triage, "
    "claimed_by, claimed_at, lease_expires_at, "
    "created_at, updated_at, done_at, archived_at, link_id, role"
)

# A claim is free if nobody holds it, or if the holder's lease has run out. The
# lease is checked lazily, right here, so no sweeper thread is needed: a crashed
# session's task simply becomes claimable again once its lease expires.
CLAIMABLE_SQL = (
    "(claimed_by IS NULL OR lease_expires_at IS NULL "
    "OR lease_expires_at < current_timestamp::TIMESTAMP)"
)

# Role routing, applied to BOTH the candidate search and the conditional UPDATE
# that actually takes the task - if only the search filtered, a task could change
# role between picking it and claiming it and the claim would still win.
#
# A caller with no role is unconstrained: that is every session that existed
# before the agent team, and the main session orchestrating by hand. A caller
# WITH a role gets its own work plus unroled work, never another role's.
ROLE_SQL = "(role IS NULL OR role = ?)"


def _role_clause(role: str | None) -> tuple[str, list]:
    return (f" AND {ROLE_SQL}", [role]) if role else ("", [])

COMMENT_COLUMNS = "id, task_id, body, kind, author, created_at"
TIME_ENTRY_COLUMNS = "id, task_id, begin_at, end_at, manual, session_id"

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
        role=row[23],
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
        session_id=row[5] if len(row) > 5 else None,
    )


# The provenance operations that close a task. `task_update` only counts when its
# details say the state changed INTO a closed state.
_DONE_OPERATION = "task_done"
_UPDATE_OPERATION = "task_update"


@dataclass(frozen=True)
class CloseEvidence:
    """What the store knows about when work on a never-clocked task could have
    begun, read in one statement at the moment it is closed.

    Every field but `now` may be None: no session was named, the session never
    closed anything, the task has no comment of its own. `now` is the DB clock,
    so the stretch is measured on the same clock every stored timestamp uses.
    """

    now: datetime
    first_work_comment_at: datetime | None
    session_started_at: datetime | None
    session_last_stop_at: datetime | None
    session_last_close_at: datetime | None
    session_running_task_id: str | None


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
        role: str | None = None,
    ) -> Task:
        with connect(project) as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, title, description, state, priority, assignee, labels, due_at, begin_at, end_at, estimated_minutes, parent_id, position, source, link_id, role)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    task_id, title, description, state, priority, assignee, labels,
                    due_at, begin_at, end_at, estimated_minutes, parent_id,
                    position, source, link_id, role,
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
        """Append position, scoped to the parent so sub-task ordering is its own.

        DO NOT rewrite this as `SELECT COALESCE(MAX(position), -1) ... WHERE
        parent_id = ?`. That is what it used to be, and on DuckDB 1.5.1 it raises
        an INTERNAL assertion - "Attempted to access index 0 within vector of size
        0" - for an ungrouped MAX over a filtered scan of a PERSISTED table that
        matches no rows, when the filter value falls inside the column's on-disk
        statistics range. That is precisely the case that matters here: the FIRST
        sub-task of a parent, whose id sorts among the existing parent_ids.

        It took sub-task creation out entirely - memory_task_add with a parent_id,
        and every memory_task_plan carrying a parent_index - so the "decompose into
        sub-tasks" rule was unimplementable while it stood.

        Not a corrupt file and not a stale index: CHECKPOINT does not clear it and
        dropping idx_tasks_parent does not either. A plain SELECT of the same rows
        is unaffected, and folding them in Python costs nothing at these row counts.
        """
        if parent_id is None:
            sql, params = "SELECT position FROM tasks WHERE parent_id IS NULL", []
        else:
            sql, params = "SELECT position FROM tasks WHERE parent_id = ?", [parent_id]
        with connect(project) as conn:
            rows = conn.execute(sql, params).fetchall()
        return max((int(r[0]) for r in rows), default=-1) + 1

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
            # The mirror's bookkeeping goes too, or deleting a task leaves rows
            # pointing at a task that no longer exists. The flusher already drops
            # an outbox row whose task is gone, so those were self-healing, but
            # task_sync rows are not and simply accumulate.
            for table in ("task_outbox", "task_sync"):
                try:
                    conn.execute(f"DELETE FROM {table} WHERE task_id = ?", [task_id])
                except Exception:  # noqa: BLE001 - table absent on an old schema
                    pass
            # The tombstone outlives the row: it is how the inbound reconcile
            # tells this card from one created on another machine.
            try:
                title = conn.execute(
                    "SELECT title FROM tasks WHERE id = ?", [task_id],
                ).fetchone()
                conn.execute(
                    "INSERT OR IGNORE INTO task_tombstones (task_id, title) VALUES (?, ?)",
                    [task_id, title[0] if title else None],
                )
            except Exception:  # noqa: BLE001 - table absent on an old schema
                pass
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
        role: str | None = None,
    ) -> bool:
        """Try to take one task. True when this caller got it.

        Still ONE conditional UPDATE whose rowcount is the answer - the role is
        another predicate on it, not a separate check. Reading the role first and
        then updating would reopen exactly the race this design closes.
        """
        clause, params = _role_clause(role)
        with connect(project) as conn:
            row = conn.execute(
                f"""
                UPDATE tasks
                SET claimed_by = ?,
                    claimed_at = current_timestamp::TIMESTAMP,
                    lease_expires_at = (current_timestamp + INTERVAL (?) MINUTE)::TIMESTAMP,
                    updated_at = current_timestamp
                WHERE id = ? AND {CLAIMABLE_SQL}{clause}
                """,
                [session_id, ttl_minutes, task_id, *params],
            ).fetchone()
        return bool(row and row[0])

    def next_claimable(self, project: str, role: str | None = None) -> Task | None:
        """The task a session should be offered next: waiting, not archived, and
        either unclaimed or held on an expired lease. Reading order, so the most
        urgent thing comes first.

        `role` narrows it to work this caller should do - its own role's tasks
        plus unroled ones. Omitting it keeps the pre-agent-team behaviour exactly:
        the caller is offered anything.
        """
        clause, params = _role_clause(role)
        with connect(project) as conn:
            row = conn.execute(
                f"""
                SELECT {TASK_COLUMNS} FROM tasks
                WHERE state IN {OPEN_STATES_SQL} AND archived_at IS NULL
                  AND parent_id IS NULL AND {CLAIMABLE_SQL}{clause}
                {_ORDER_BY}
                LIMIT 1
                """,
                params,
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

    def start_entry(
        self, project: str, entry_id: str, task_id: str, session_id: str | None = None,
    ) -> TaskTimeEntry:
        """Open a running entry (end_at NULL) from the DB clock. `manual` stays
        FALSE: it marks a stretch typed in by hand rather than clocked, which
        nothing does yet. `session_id` says who clocked on, so that session's
        end can close it."""
        with connect(project) as conn:
            conn.execute(
                "INSERT INTO task_time_entries (id, task_id, begin_at, manual, session_id) "
                "VALUES (?, ?, current_timestamp, FALSE, ?)",
                [entry_id, task_id, session_id],
            )
            row = conn.execute(
                f"SELECT {TIME_ENTRY_COLUMNS} FROM task_time_entries WHERE id = ?",
                [entry_id],
            ).fetchone()
        return _row_to_entry(row)

    def add_manual_entry(
        self, project: str, entry_id: str, task_id: str, begin_at, end_at,
        session_id: str | None = None,
    ) -> TaskTimeEntry:
        """Record a CLOSED stretch with explicit bounds, marked `manual`.

        For reconstructing work that was done but never clocked - the task sat
        in in_progress and was closed with no running entry. `manual` is the
        honest label: this stretch was derived from the state history, not
        measured by the clock, and the flag is what lets anyone reading the
        table tell the two apart.

        `session_id` names the session an ESTIMATED stretch was credited to, so
        that session's next estimate starts after this one instead of overlapping
        it. A stretch recovered from state history names none.
        """
        with connect(project) as conn:
            conn.execute(
                "INSERT INTO task_time_entries (id, task_id, begin_at, end_at, manual, session_id) "
                "VALUES (?, ?, ?, ?, TRUE, ?)",
                [entry_id, task_id, begin_at, end_at, session_id],
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

    def stop_all_entries(self, project: str, task_id: str) -> list[TaskTimeEntry]:
        """Close EVERY open entry on a task, returning the ones closed.

        Every close path used to close one entry - the newest - and `start` has
        no lock, so two concurrent starts left the older entry open forever and
        counting. Closing all of them is both the fix and the repair: any entry
        already orphaned in a live database is closed the next time its task is
        stopped, finished, released or archived.
        """
        with connect(project) as conn:
            rows = conn.execute(
                f"SELECT {TIME_ENTRY_COLUMNS} FROM task_time_entries "
                f"WHERE task_id = ? AND end_at IS NULL",
                [task_id],
            ).fetchall()
            if not rows:
                return []
            conn.execute(
                "UPDATE task_time_entries SET end_at = current_timestamp "
                "WHERE task_id = ? AND end_at IS NULL",
                [task_id],
            )
            closed = conn.execute(
                f"SELECT {TIME_ENTRY_COLUMNS} FROM task_time_entries "
                f"WHERE id IN ({', '.join('?' for _ in rows)})",
                [r[0] for r in rows],
            ).fetchall()
        return [_row_to_entry(r) for r in closed]

    def running_task_ids_for_session(self, project: str, session_id: str) -> list[str]:
        """Tasks whose open clock was started by this session."""
        with connect(project) as conn:
            rows = conn.execute(
                "SELECT DISTINCT task_id FROM task_time_entries "
                "WHERE session_id = ? AND end_at IS NULL",
                [session_id],
            ).fetchall()
        return [r[0] for r in rows]

    def close_evidence(
        self,
        project: str,
        task_id: str,
        session_id: str | None,
        closed_states: Sequence[str],
        skip_comment_prefix: str,
    ) -> CloseEvidence:
        """The evidence an estimate for a never-clocked task is built from.

        One statement rather than one read per source, and it reaches into
        `sessions` and `provenance` because both live in this project's file: the
        alternative was four round trips on every close. With no `session_id`
        every session field comes back None - `= NULL` matches nothing.

        - the task's first comment that is not the one `skip_comment_prefix`
          starts (memory_task_plan's verbatim copy of the request);
        - when the session started;
        - the latest end of a stretch the session clocked on ANOTHER task;
        - the latest close of ANOTHER task the session recorded in provenance;
        - a task other than this one the session still has a clock running on.
        """
        closed_state_values = list(closed_states)
        with connect(project) as conn:
            row = conn.execute(
                """
                SELECT
                    current_timestamp::TIMESTAMP,
                    (SELECT min(c.created_at) FROM task_comments c
                      WHERE c.task_id = ? AND NOT starts_with(c.body, ?)),
                    (SELECT s.started_at FROM sessions s WHERE s.id = ?),
                    (SELECT max(e.end_at) FROM task_time_entries e
                      WHERE e.session_id = ? AND e.task_id <> ? AND e.end_at IS NOT NULL),
                    (SELECT max(p.created_at) FROM provenance p
                      WHERE p.memory_id <> ?
                        AND json_extract_string(p.details, '$.session_id') = ?
                        AND (p.operation = ?
                             OR (p.operation = ?
                                 AND list_contains(?, json_extract_string(p.details, '$.state_to'))
                                 AND json_extract_string(p.details, '$.state_from')
                                     IS DISTINCT FROM json_extract_string(p.details, '$.state_to')))),
                    (SELECT min(e.task_id) FROM task_time_entries e
                      WHERE e.session_id = ? AND e.task_id <> ? AND e.end_at IS NULL)
                """,
                [
                    task_id, skip_comment_prefix,
                    session_id,
                    session_id, task_id,
                    task_id, session_id, _DONE_OPERATION, _UPDATE_OPERATION,
                    closed_state_values,
                    session_id, task_id,
                ],
            ).fetchone()
        return CloseEvidence(
            now=row[0],
            first_work_comment_at=row[1],
            session_started_at=row[2],
            session_last_stop_at=row[3],
            session_last_close_at=row[4],
            session_running_task_id=row[5],
        )

    def expired_claims(self, project: str, grace_minutes: int = 0) -> list[Task]:
        """Tasks still marked as held by a session whose lease ran out more than
        `grace_minutes` ago.

        The claim itself is checked lazily by every claim attempt, so an expired
        holder never blocks anyone; what nothing checked until now is the clock
        that holder left running. Session start sweeps these, with a grace
        period so a holder that is merely quiet is not mistaken for one that is
        gone.
        """
        with connect(project) as conn:
            rows = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks WHERE claimed_by IS NOT NULL "
                f"AND lease_expires_at IS NOT NULL "
                f"AND lease_expires_at < (current_timestamp - INTERVAL (?) MINUTE)::TIMESTAMP",
                [int(grace_minutes)],
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    # ---------- tombstones ----------

    def is_tombstoned(self, project: str, task_id: str) -> bool:
        try:
            with connect(project) as conn:
                row = conn.execute(
                    "SELECT 1 FROM task_tombstones WHERE task_id = ?", [task_id],
                ).fetchone()
        except Exception:  # noqa: BLE001 - table absent on an old schema
            return False
        return row is not None

    # ---------- retry detection ----------
    #
    # A tool call that does its work and then loses its response - a daemon
    # restart, a dropped MCP session, a client timeout - is retried by the
    # caller. These reads are how a retry finds the first call's work instead of
    # doing it again. Every one is windowed in SQL against the database's own
    # clock, the same clock `created_at` was stamped with.

    def recent_identical(
        self, project: str, title: str, description: str | None,
        parent_id: str | None, window_seconds: int,
    ) -> Task | None:
        """The newest live task with exactly this title, description and parent,
        created within the last `window_seconds`; None when there is none.

        Exact equality on purpose: a near-miss is a different task, and
        silently folding it into an existing one would lose a requirement. The
        description is compared trimmed at its ends, the way the title already
        is, so a re-sent call that picked up a trailing newline still matches;
        `description` must be passed trimmed. DuckDB's one-argument trim() strips
        spaces only, so the whitespace set is spelled out.
        """
        with connect(project) as conn:
            row = conn.execute(
                f"""
                SELECT {TASK_COLUMNS} FROM tasks
                WHERE title = ?
                  AND trim(description, chr(32) || chr(9) || chr(10) || chr(13))
                      IS NOT DISTINCT FROM ?
                  AND parent_id IS NOT DISTINCT FROM ?
                  AND archived_at IS NULL
                  AND created_at >= (current_timestamp - INTERVAL (?) SECOND)::TIMESTAMP
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [title, description, parent_id, window_seconds],
            ).fetchone()
        return _row_to_task(row) if row else None

    def record_plan(
        self, project: str, plan_id: str, request_hash: str, task_ids: Sequence[str],
        retain_seconds: int,
    ) -> None:
        """Remember which tasks one plan created, in plan order, and forget every
        plan older than `retain_seconds` - past the retry window a record can
        never match, so keeping it only grows the table."""
        ordered_ids = list(task_ids)
        with connect(project) as conn:
            conn.execute(
                "DELETE FROM task_plans "
                "WHERE created_at < (current_timestamp - INTERVAL (?) SECOND)::TIMESTAMP",
                [retain_seconds],
            )
            conn.execute(
                "INSERT INTO task_plans (id, request_hash, task_ids) VALUES (?, ?, ?)",
                [plan_id, request_hash, ordered_ids],
            )

    def recent_plans(
        self, project: str, request_hash: str, window_seconds: int, limit: int,
    ) -> tuple[tuple[str, ...], ...]:
        """The task ids of each plan recorded under `request_hash` within the
        last `window_seconds`, newest plan first, each in its own plan order."""
        with connect(project) as conn:
            rows = conn.execute(
                """
                SELECT task_ids FROM task_plans
                WHERE request_hash = ?
                  AND created_at >= (current_timestamp - INTERVAL (?) SECOND)::TIMESTAMP
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [request_hash, window_seconds, limit],
            ).fetchall()
        return tuple(tuple(row[0]) for row in rows)

    def live_tasks(self, project: str, task_ids: Sequence[str]) -> tuple[Task, ...]:
        """The tasks among `task_ids` that still exist and are not archived, in
        no particular order. A deleted task has no row; an archived one is
        filtered out here."""
        if not task_ids:
            return ()
        placeholders = ", ".join("?" for _ in task_ids)
        params = list(task_ids)
        with connect(project) as conn:
            rows = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks "
                f"WHERE id IN ({placeholders}) AND archived_at IS NULL",
                params,
            ).fetchall()
        return tuple(_row_to_task(row) for row in rows)

    def list_meta(self, project: str) -> dict[str, dict]:
        """Per-task row metadata for the list view, in four grouped queries.

        The list shows comment counts, sub-task progress, tracked time, whether a
        clock is running and how many files are attached - none of which live on
        the `tasks` row, and none of which should cost a query per task.
        """
        meta: dict[str, dict] = {}

        def slot(task_id: str) -> dict:
            return meta.setdefault(
                task_id,
                {
                    "comments": 0, "subtasks_total": 0, "subtasks_done": 0,
                    "minutes_spent": 0, "running": False, "attachments": 0,
                },
            )

        with connect(project) as conn:
            for task_id, count in conn.execute(
                "SELECT task_id, COUNT(*) FROM task_comments GROUP BY task_id"
            ).fetchall():
                slot(task_id)["comments"] = int(count)

            try:
                for task_id, count in conn.execute(
                    "SELECT task_id, COUNT(*) FROM task_attachments GROUP BY task_id"
                ).fetchall():
                    slot(task_id)["attachments"] = int(count)
            except Exception:  # noqa: BLE001 - a DB older than v11
                pass

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


class AttachmentRepository:
    """Task attachments: metadata in DuckDB, bytes on disk.

    Content-addressed by sha256, so attaching the same screenshot to two tasks
    stores one file. Deleting a row therefore only removes the blob when nothing
    else references it - the cheap alternative, deleting eagerly, would break the
    other task silently.
    """

    def add(self, project: str, attachment: TaskAttachment, path: str) -> TaskAttachment:
        with connect(project) as conn:
            conn.execute(
                "INSERT INTO task_attachments (id, task_id, filename, content_type, "
                "size_bytes, sha256, path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [attachment.id, attachment.task_id, attachment.filename,
                 attachment.content_type, attachment.size_bytes, attachment.sha256, path],
            )
        return attachment

    def list_for(self, project: str, task_id: str) -> list[TaskAttachment]:
        try:
            with connect(project) as conn:
                rows = conn.execute(
                    "SELECT id, task_id, filename, content_type, size_bytes, sha256, "
                    "created_at, mirrored_at FROM task_attachments "
                    "WHERE task_id = ? ORDER BY created_at ASC",
                    [task_id],
                ).fetchall()
        except Exception:
            return []
        return [
            TaskAttachment(
                id=r[0], task_id=r[1], filename=r[2], content_type=r[3],
                size_bytes=r[4] or 0, sha256=r[5], created_at=r[6], mirrored_at=r[7],
            )
            for r in rows
        ]

    def get(self, project: str, attachment_id: str) -> tuple[TaskAttachment, str] | None:
        with connect(project) as conn:
            row = conn.execute(
                "SELECT id, task_id, filename, content_type, size_bytes, sha256, "
                "created_at, mirrored_at, path FROM task_attachments WHERE id = ?",
                [attachment_id],
            ).fetchone()
        if not row:
            return None
        return (
            TaskAttachment(
                id=row[0], task_id=row[1], filename=row[2], content_type=row[3],
                size_bytes=row[4] or 0, sha256=row[5], created_at=row[6],
                mirrored_at=row[7],
            ),
            row[8],
        )

    def find_by_hash(self, project: str, task_id: str, sha256: str) -> TaskAttachment | None:
        """The attachment already holding these bytes on this task, if any.

        The guard that makes attaching idempotent: without it the same file
        attached twice to one task became two rows, and the flusher uploaded
        both - two identical files on the card.
        """
        with connect(project) as conn:
            row = conn.execute(
                "SELECT id, task_id, filename, content_type, size_bytes, sha256, "
                "created_at, mirrored_at FROM task_attachments "
                "WHERE task_id = ? AND sha256 = ? ORDER BY created_at ASC LIMIT 1",
                [task_id, sha256],
            ).fetchone()
        if not row:
            return None
        return TaskAttachment(
            id=row[0], task_id=row[1], filename=row[2], content_type=row[3],
            size_bytes=row[4] or 0, sha256=row[5], created_at=row[6], mirrored_at=row[7],
        )

    def unmirrored(self, project: str, task_id: str) -> list[dict]:
        """Attachments not yet sent, oldest first."""
        try:
            with connect(project) as conn:
                rows = conn.execute(
                    "SELECT id, filename, content_type, path FROM task_attachments "
                    "WHERE task_id = ? AND mirrored_at IS NULL ORDER BY created_at ASC",
                    [task_id],
                ).fetchall()
        except Exception:
            return []
        return [{"id": r[0], "filename": r[1], "content_type": r[2], "path": r[3]}
                for r in rows]

    def mark_mirrored(self, project: str, attachment_id: str) -> None:
        from datetime import datetime, timezone

        with connect(project) as conn:
            conn.execute(
                "UPDATE task_attachments SET mirrored_at = ? WHERE id = ?",
                [datetime.now(timezone.utc), attachment_id],
            )

    def delete(self, project: str, attachment_id: str) -> str | None:
        """Remove the row. Returns the blob path only when no row still uses it,
        so a file shared by two tasks is never deleted out from under one."""
        found = self.get(project, attachment_id)
        if not found:
            return None
        attachment, path = found
        with connect(project) as conn:
            conn.execute("DELETE FROM task_attachments WHERE id = ?", [attachment_id])
            still_used = conn.execute(
                "SELECT count(*) FROM task_attachments WHERE sha256 = ?",
                [attachment.sha256],
            ).fetchone()[0]
        # A parked compose-box file points at the same blob until it is bound.
        # Deleting the blob out from under it would make the later bind fail on
        # "the bytes are gone" for a file the user did hand over.
        if not still_used:
            try:
                with connect(project) as conn:
                    still_used = conn.execute(
                        "SELECT count(*) FROM attachment_inbox "
                        "WHERE sha256 = ? AND bound_task_id IS NULL",
                        [attachment.sha256],
                    ).fetchone()[0]
            except Exception:  # noqa: BLE001 - a DB older than v15 has no inbox
                still_used = 0
        return None if still_used else path


_INBOX_COLUMNS = (
    "id, claude_session_id, sha256, filename, content_type, size_bytes, source, "
    "created_at, bound_task_id, bound_at, notice, notified_at, path"
)


def _row_to_pending(row) -> tuple[PendingAttachment, str]:
    return (
        PendingAttachment(
            id=row[0], claude_session_id=row[1], sha256=row[2], filename=row[3],
            content_type=row[4], size_bytes=row[5] or 0, source=row[6],
            created_at=row[7], bound_task_id=row[8], bound_at=row[9],
            notice=row[10], notified_at=row[11],
        ),
        row[12],
    )


class AttachmentInboxRepository:
    """Compose-box files parked until a session binds them to a task.

    One row per (Claude session, bytes): the same paste scanned twice - at
    UserPromptSubmit and again at Stop, or after a transcript was re-read from the
    top - is found here and skipped, so it is parked and announced once.
    """

    def find(
        self, project: str, claude_session_id: str, sha256: str,
    ) -> PendingAttachment | None:
        with connect(project) as conn:
            row = conn.execute(
                f"SELECT {_INBOX_COLUMNS} FROM attachment_inbox "
                f"WHERE claude_session_id = ? AND sha256 = ? LIMIT 1",
                [claude_session_id, sha256],
            ).fetchone()
        return _row_to_pending(row)[0] if row else None

    def get(self, project: str, pending_id: str) -> tuple[PendingAttachment, str] | None:
        """The parked row and its blob path."""
        with connect(project) as conn:
            row = conn.execute(
                f"SELECT {_INBOX_COLUMNS} FROM attachment_inbox WHERE id = ?",
                [pending_id],
            ).fetchone()
        return _row_to_pending(row) if row else None

    def add(self, project: str, pending: PendingAttachment, path: str) -> PendingAttachment:
        with connect(project) as conn:
            conn.execute(
                "INSERT INTO attachment_inbox (id, claude_session_id, sha256, filename, "
                "content_type, size_bytes, path, source, notice) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [pending.id, pending.claude_session_id, pending.sha256, pending.filename,
                 pending.content_type, pending.size_bytes, path, pending.source,
                 pending.notice],
            )
        return pending

    def set_notice(self, project: str, pending_id: str, notice: str) -> None:
        with connect(project) as conn:
            conn.execute(
                "UPDATE attachment_inbox SET notice = ?, notified_at = NULL WHERE id = ?",
                [notice, pending_id],
            )

    def mark_bound(self, project: str, pending_id: str, task_id: str) -> None:
        with connect(project) as conn:
            conn.execute(
                "UPDATE attachment_inbox SET bound_task_id = ?, "
                "bound_at = current_timestamp::TIMESTAMP WHERE id = ?",
                [task_id, pending_id],
            )

    def undelivered(self, project: str, claude_session_id: str) -> list[tuple[str, str]]:
        """`(pending_id, notice)` not yet put in this session's context, oldest first."""
        with connect(project) as conn:
            rows = conn.execute(
                "SELECT id, notice FROM attachment_inbox "
                "WHERE claude_session_id = ? AND notice IS NOT NULL "
                "AND notified_at IS NULL ORDER BY created_at ASC, id ASC",
                [claude_session_id],
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def mark_notified(self, project: str, pending_ids: list[str]) -> None:
        if not pending_ids:
            return
        placeholders = ", ".join("?" for _ in pending_ids)
        with connect(project) as conn:
            conn.execute(
                f"UPDATE attachment_inbox SET notified_at = current_timestamp::TIMESTAMP "
                f"WHERE id IN ({placeholders})",
                list(pending_ids),
            )

    def candidates(self, project: str) -> list[dict]:
        """Tasks this project is demonstrably working on right now.

        in_progress, not archived, claimed on a live lease, AND with a clock
        running - with the session that holds that clock, so the caller can keep
        only the leads'. Sub-tasks included on purpose: `list_tasks` hides them by
        default, and the work being done is usually on one.
        """
        with connect(project) as conn:
            rows = conn.execute(
                """
                SELECT t.id, t.title, e.session_id
                FROM tasks t
                JOIN task_time_entries e ON e.task_id = t.id AND e.end_at IS NULL
                WHERE t.state = 'in_progress'
                  AND t.archived_at IS NULL
                  AND t.claimed_by IS NOT NULL
                  AND (t.lease_expires_at IS NULL
                       OR t.lease_expires_at > current_timestamp::TIMESTAMP)
                ORDER BY e.begin_at DESC, t.id ASC
                """
            ).fetchall()
        return [{"id": r[0], "title": r[1], "clock_session": r[2]} for r in rows]


# Oldest first. Ordered by an EXPRESSION rather than the bare column so DuckDB's
# row-group pruner cannot use `created_at`'s statistics - statistics that still
# count deleted rows, which once made this read return nothing from a full
# outbox. See db.connection._disable_unsafe_optimizers.
OUTBOX_PENDING_SQL = (
    "SELECT id, task_id, op, payload, attempts, last_error "
    "FROM task_outbox ORDER BY epoch_us(created_at) ASC, rowid ASC LIMIT ?"
)

#: The outbox op that sends a task's closed, unsent time entries.
TIME_OP = "time"

#: How many tasks one catch-up pass queues per project. Far above anything seen
#: (seven entries over two projects when it was written); the NOT EXISTS below
#: means a pass that hits it leaves the rest for the next one, never a duplicate.
UNSENT_TIME_TASK_LIMIT = 1000

# One `time` op for every task that has closed, unsent work, a card on the board
# and no `time` op already waiting - as ONE statement, so the check and the
# insert cannot be split by a clock stopping in between and there is no query
# per task. It only queues: `TaskBridge._flush_time` still reads the entries
# and marks each one as it lands, which is what keeps an entry from being sent
# twice. A task with no remote card is skipped rather than queued, because
# flushing its op would CREATE the card as a side effect of reporting time.
QUEUE_UNSENT_TIME_SQL = """
    INSERT INTO task_outbox (id, task_id, op, payload)
    SELECT CAST(uuid() AS VARCHAR), unsent.task_id, ?, '{}'
    FROM (
        SELECT DISTINCT e.task_id
        FROM task_time_entries e
        JOIN tasks t ON t.id = e.task_id
        WHERE e.end_at IS NOT NULL
          AND e.mirrored_at IS NULL
          AND EXISTS (
              SELECT 1 FROM task_sync s
              WHERE s.task_id = e.task_id AND s.remote_task_id IS NOT NULL
          )
          AND NOT EXISTS (
              SELECT 1 FROM task_outbox o
              WHERE o.task_id = e.task_id AND o.op = ?
          )
        ORDER BY e.task_id
        LIMIT ?
    ) AS unsent
    RETURNING task_id
"""


#: How many closed local stretches one time import may compare against. Far past
#: any real card set; reaching it RAISES rather than matching against a partial
#: read, because a stretch missing from the read is one imported a second time.
TIME_MATCH_ENTRY_LIMIT = 20000

# Per card on one board: the local task it maps to, and the milliseconds of that
# task's CLOSED work the board should already hold - what was mirrored out plus
# what was imported, both of which carry mirrored_at. Milliseconds truncated per
# stretch, because that is the precision the platform keeps a stretch at; the
# bridge rounds the sum the way the board rounds its own total.
SYNCED_TIME_TOTALS_SQL = """
    SELECT s.remote_task_id, s.task_id,
           COALESCE(sum(epoch_ms(e.end_at) - epoch_ms(e.begin_at)), 0)
    FROM task_sync s
    LEFT JOIN task_time_entries e
           ON e.task_id = s.task_id
          AND e.end_at IS NOT NULL
          AND e.mirrored_at IS NOT NULL
    WHERE s.link_id = ? AND list_contains(?, s.remote_task_id)
    GROUP BY s.remote_task_id, s.task_id
    ORDER BY s.remote_task_id
    LIMIT ?
"""

LOCAL_CLOSED_ENTRIES_SQL = """
    SELECT id, task_id, begin_at, end_at, remote_id
    FROM task_time_entries
    WHERE list_contains(?, task_id) AND end_at IS NOT NULL
    ORDER BY task_id, begin_at
    LIMIT ?
"""

# Numbered parameters: DuckDB binds the FROM clause before the select list, so
# positional `?` markers would be matched to the wrong values.
#
# The NOT EXISTS is the idempotency guard at the last possible moment: even a
# plan built from a stale read cannot record a remote stretch twice.
INSERT_IMPORTED_TIME_SQL = """
    INSERT INTO task_time_entries
        (id, task_id, begin_at, end_at, manual, mirrored_at, remote_id)
    SELECT incoming.id, incoming.task_id, incoming.begin_at, incoming.end_at,
           incoming.manual, $1, incoming.remote_id
    FROM (
        SELECT unnest($2::VARCHAR[]) AS id,
               unnest($3::VARCHAR[]) AS task_id,
               unnest($4::TIMESTAMP[]) AS begin_at,
               unnest($5::TIMESTAMP[]) AS end_at,
               unnest($6::BOOLEAN[]) AS manual,
               unnest($7::VARCHAR[]) AS remote_id
    ) AS incoming
    WHERE NOT EXISTS (
        SELECT 1 FROM task_time_entries existing
        WHERE existing.remote_id = incoming.remote_id
    )
    RETURNING id
"""

# A stretch of ours the board was seen holding gets the board's id, and counts
# as sent: it is on the board, so a flush that sent it again would double it.
# COALESCE keeps the instant a flush already recorded.
STAMP_MATCHED_TIME_SQL = """
    UPDATE task_time_entries
    SET remote_id = matched.remote_id,
        mirrored_at = COALESCE(task_time_entries.mirrored_at, $1)
    FROM (
        SELECT unnest($2::VARCHAR[]) AS id, unnest($3::VARCHAR[]) AS remote_id
    ) AS matched
    WHERE task_time_entries.id = matched.id
      AND task_time_entries.remote_id IS NULL
"""


@dataclass(frozen=True)
class SyncedTimeTotal:
    """A card on one board, the local task it is, and the closed work (in
    milliseconds) of that task the board should already hold."""

    remote_task_id: str
    task_id: str
    mirrored_milliseconds: int


@dataclass(frozen=True)
class LocalTimeEntry:
    """A closed local stretch, as a time import matches the board's against it."""

    id: str
    task_id: str
    begin_at: datetime
    end_at: datetime
    remote_id: str | None


@dataclass(frozen=True)
class ImportedTimeEntry:
    """A stretch from the board to record here, in the store's naive local clock."""

    task_id: str
    remote_id: str
    begin_at: datetime
    end_at: datetime
    manual: bool


@dataclass(frozen=True)
class RemoteTimeMatch:
    """A local stretch the board holds too, and the board's id for it."""

    entry_id: str
    remote_id: str


def _insert_imported_time(conn, imported: Sequence[ImportedTimeEntry], now: datetime) -> int:
    """Insert the board's stretches in one statement; how many were new."""
    import uuid

    if not imported:
        return 0
    rows = conn.execute(INSERT_IMPORTED_TIME_SQL, [
        now,
        [str(uuid.uuid4()) for _ in imported],
        [entry.task_id for entry in imported],
        [entry.begin_at for entry in imported],
        [entry.end_at for entry in imported],
        [entry.manual for entry in imported],
        [entry.remote_id for entry in imported],
    ]).fetchall()
    return len(rows)


def _stamp_matched_time(conn, matched: Sequence[RemoteTimeMatch], now: datetime) -> None:
    """Give our stretches the board holds its ids, in one statement."""
    if not matched:
        return
    conn.execute(STAMP_MATCHED_TIME_SQL, [
        now,
        [match.entry_id for match in matched],
        [match.remote_id for match in matched],
    ])


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
                # OUTBOX_PENDING_SQL stays correct even on a connection without
                # the optimizer guard; the mirror depends on this one read.
                rows = conn.execute(OUTBOX_PENDING_SQL, [limit]).fetchall()
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

    def fail(self, project: str, row_id: str, error: str, *, count: bool = True) -> bool:
        """Mirroring failed. Returns True if the row was given up on.

        The row stays so the next flush retries it - until MAX_OUTBOX_ATTEMPTS,
        after which it is dropped. Retrying forever is worse than losing one
        mirror: the call that failed may have already had its effect remotely,
        so each retry repeats it.

        `count=False` records the error without spending an attempt. That is
        for an outage - unreachable, 5xx - where the call had no effect at all
        and retrying is the only right answer. Five mutations during one outage
        used to burn a row's five attempts and drop the pending change.
        """
        with connect(project) as conn:
            conn.execute(
                "UPDATE task_outbox SET attempts = attempts + ?, last_error = ? "
                "WHERE id = ?",
                [1 if count else 0, error[:500], row_id],
            )
            row = conn.execute(
                "SELECT attempts FROM task_outbox WHERE id = ?", [row_id]
            ).fetchone()
            if row and row[0] >= MAX_OUTBOX_ATTEMPTS:
                conn.execute("DELETE FROM task_outbox WHERE id = ?", [row_id])
                return True
        return False

    def last_failure(self, project: str) -> str | None:
        """The newest error still sitting in the outbox, if any.

        So a bad token or a 4xx surfaces on the NEXT task call instead of never:
        the mirror runs on a thread and swallows its exceptions, which is right
        for latency and wrong for silence.
        """
        try:
            with connect(project) as conn:
                row = conn.execute(
                    "SELECT last_error FROM task_outbox WHERE last_error IS NOT NULL "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                ).fetchone()
        except Exception:
            return None
        return row[0] if row else None

    def unreadable(self, project: str) -> int:
        """Rows the outbox holds that `pending()` cannot return. 0 when healthy.

        The failure this exists for was silent: `count(*)` said 558 while the
        flusher's read returned nothing, so the flusher reported an empty queue,
        recorded no error, and the board stopped updating for three days. A
        non-zero answer here means the read itself is broken - not asoode, not
        the network - and it is surfaced in the mirror report and the log.
        """
        depth = self.depth(project)
        if depth == 0:
            return 0
        return depth if not self.pending(project, 1) else 0

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
                    "SELECT id, body, kind, author FROM task_comments "
                    "WHERE task_id = ? AND mirrored_at IS NULL "
                    "ORDER BY created_at ASC",
                    [task_id],
                ).fetchall()
        except Exception:
            return []
        return [
            {"id": r[0], "body": r[1], "kind": r[2] or "note", "author": r[3]}
            for r in rows
        ]

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

    def queue_unsent_time(self, project: str) -> tuple[str, ...]:
        """Queue a `time` op for every task whose closed time was never sent.

        For entries no flush will ever look at: `_flush_time` runs only when a
        `time` op for that task drains, and entries closed before every closing
        path queued one have none. Returns the task ids queued, one per task.

        Unlike `enqueue` this RAISES: it is not inside anyone's local edit, and
        a catch-up that fails silently is the failure it exists to repair.
        """
        with connect(project) as conn:
            rows = conn.execute(
                QUEUE_UNSENT_TIME_SQL, [TIME_OP, TIME_OP, UNSENT_TIME_TASK_LIMIT],
            ).fetchall()
        return tuple(row[0] for row in rows)

    def mark_time_mirrored(self, project: str, entry_id: str) -> None:
        from datetime import datetime, timezone

        with connect(project) as conn:
            conn.execute(
                "UPDATE task_time_entries SET mirrored_at = ? WHERE id = ?",
                [datetime.now(timezone.utc), entry_id],
            )

    # ---------- time read back from the board ----------

    def synced_time_totals(
        self, project: str, link_id: int, remote_task_ids: Sequence[str],
    ) -> tuple[SyncedTimeTotal, ...]:
        """For each of these cards that maps to a local task on this link, the
        closed work the board should already hold. A card with no local task is
        left out - there is nowhere to put its time."""
        if not remote_task_ids:
            return ()
        wanted = list(remote_task_ids)
        with connect(project) as conn:
            rows = conn.execute(
                SYNCED_TIME_TOTALS_SQL, [link_id, wanted, len(wanted)],
            ).fetchall()
        return tuple(
            SyncedTimeTotal(remote_task_id=row[0], task_id=row[1], mirrored_milliseconds=row[2])
            for row in rows
        )

    def closed_entries_for(
        self, project: str, task_ids: Sequence[str],
    ) -> tuple[LocalTimeEntry, ...]:
        """Every closed stretch of these tasks, mirrored or not, oldest first.

        Raises when there are more than TIME_MATCH_ENTRY_LIMIT: matching the
        board against part of the local record would import the rest again.
        """
        if not task_ids:
            return ()
        wanted = list(task_ids)
        with connect(project) as conn:
            rows = conn.execute(
                LOCAL_CLOSED_ENTRIES_SQL, [wanted, TIME_MATCH_ENTRY_LIMIT + 1],
            ).fetchall()
        if len(rows) > TIME_MATCH_ENTRY_LIMIT:
            raise RuntimeError(
                f"more than {TIME_MATCH_ENTRY_LIMIT} closed time entries to match "
                f"in {project} - refusing to import against a partial read"
            )
        return tuple(
            LocalTimeEntry(id=row[0], task_id=row[1], begin_at=row[2], end_at=row[3], remote_id=row[4])
            for row in rows
        )

    def record_remote_time(
        self,
        project: str,
        imported: Sequence[ImportedTimeEntry],
        matched: Sequence[RemoteTimeMatch],
    ) -> int:
        """Record the board's stretches here and stamp ours it was seen holding.

        One transaction, two set-based statements. Every imported row carries
        `mirrored_at`, so no flush or catch-up ever sends it back. Returns how
        many stretches were actually inserted - fewer than passed when another
        import got there first.
        """
        from datetime import timezone

        from memory_mcp.db.connection import transaction

        if not imported and not matched:
            return 0
        now = datetime.now(timezone.utc)
        with transaction(project) as conn:
            inserted = _insert_imported_time(conn, imported, now)
            _stamp_matched_time(conn, matched, now)
        return inserted

    # ---------- the local task -> remote task map ----------

    def remote_ids_for(self, project: str, task_id: str) -> dict[int, str]:
        """Every remote task a local one maps to, keyed by link id.

        Read BEFORE a hard delete, which drops the task_sync rows: a delete op
        carries these in its payload because by the time the flusher runs there
        is no task row left to route from.
        """
        try:
            with connect(project) as conn:
                rows = conn.execute(
                    "SELECT link_id, remote_task_id FROM task_sync "
                    "WHERE task_id = ? AND remote_task_id IS NOT NULL",
                    [task_id],
                ).fetchall()
        except Exception:
            return {}
        return {int(r[0]): r[1] for r in rows}

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
