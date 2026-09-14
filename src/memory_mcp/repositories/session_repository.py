"""Session repository - CRUD for session records in per-project DB."""

import json

from memory_mcp.db.connection import connect
from memory_mcp.models import SessionRecord


class SessionRepository:
    """Session CRUD."""

    def insert(
        self, project: str, session_id: str, metadata: dict | None = None,
    ) -> None:
        """Open a session, optionally stamping who it belongs to.

        `metadata` is the `sessions.metadata` JSON column, which existed unused
        until `{"agent": ...}` gave it a job: a dispatched agent names its own
        type, the lead passes nothing, and `open_lead_sessions` can then tell the
        two apart. Writing NULL rather than `{}` for no metadata keeps every row
        written before this indistinguishable from a lead's, which is what they
        were.
        """
        with connect(project) as conn:
            conn.execute(
                "INSERT INTO sessions (id, started_at, last_seen_at, metadata) "
                "VALUES (?, current_timestamp, current_timestamp, ?)",
                [session_id, json.dumps(metadata) if metadata else None],
            )

    def open_lead_sessions(self, project: str) -> list[str]:
        """Ids of unended sessions that did NOT name an agent - the leads'.

        This is self-reported, and deliberately so: there is no way to ask a
        session what dispatched it. A subagent that forgets to pass `agent` looks
        like a second lead, which makes a caller ambiguous rather than wrong -
        "two candidates, ask" instead of "bind it to the wrong one".
        """
        with connect(project) as conn:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NULL "
                "AND (metadata IS NULL "
                "     OR json_extract_string(metadata, '$.agent') IS NULL) "
                "ORDER BY started_at"
            ).fetchall()
        return [r[0] for r in rows]

    def metadata(self, project: str, session_id: str) -> dict | None:
        """The session's stored metadata, or None when it has none."""
        with connect(project) as conn:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", [session_id]
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            value = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def touch(self, project: str, session_id: str) -> None:
        """Stamp last_seen_at. The heartbeat behind the multi-session claim: it
        costs nothing, because every tool call already reaches the daemon."""
        with connect(project) as conn:
            conn.execute(
                "UPDATE sessions SET last_seen_at = current_timestamp WHERE id = ?",
                [session_id],
            )

    def end(
        self,
        project: str,
        session_id: str,
        summary: str,
        memories_created: int = 0,
        memories_accessed: int = 0,
    ) -> None:
        with connect(project) as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = current_timestamp, summary = ?, memories_created = ?, memories_accessed = ? WHERE id = ?",
                [summary, memories_created, memories_accessed, session_id],
            )

    def list_all(self, project: str, limit: int = 50) -> list[SessionRecord]:
        """Return recent sessions, newest first."""
        with connect(project) as conn:
            rows = conn.execute(
                "SELECT id, started_at, ended_at, summary, memories_created, memories_accessed "
                "FROM sessions ORDER BY started_at DESC LIMIT ?",
                [limit],
            ).fetchall()
        return [
            SessionRecord(
                id=r[0], started_at=r[1], ended_at=r[2], summary=r[3],
                memories_created=r[4], memories_accessed=r[5],
            )
            for r in rows
        ]

    def orphaned(self, project: str) -> list[str]:
        """Return IDs of sessions that never received an ended_at timestamp."""
        with connect(project) as conn:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NULL"
            ).fetchall()
        return [r[0] for r in rows]

    def last_with_summary(self, project: str, exclude_summary: str | None = None) -> SessionRecord | None:
        """Return the most recent ended session, optionally skipping a given summary."""
        sql = (
            "SELECT id, started_at, ended_at, summary, memories_created, memories_accessed "
            "FROM sessions WHERE ended_at IS NOT NULL"
        )
        params: list = []
        if exclude_summary is not None:
            sql += " AND summary != ?"
            params.append(exclude_summary)
        sql += " ORDER BY ended_at DESC LIMIT 1"

        with connect(project) as conn:
            row = conn.execute(sql, params).fetchone()

        if not row:
            return None
        return SessionRecord(
            id=row[0], started_at=row[1], ended_at=row[2], summary=row[3],
            memories_created=row[4], memories_accessed=row[5],
        )
