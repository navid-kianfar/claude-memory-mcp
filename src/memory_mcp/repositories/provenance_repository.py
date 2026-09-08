"""Provenance repository - audit trail for all memory operations."""

import json

from memory_mcp.db.connection import connect
from memory_mcp.models import ProvenanceEntry


def _loads(value):
    """`details` comes back as JSON text or already-decoded, depending on driver."""
    if not value:
        return None
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return None


class ProvenanceRepository:
    """Audit log CRUD."""

    def record(
        self,
        project: str,
        memory_id: str,
        operation: str,
        details: dict | None = None,
        actor: str | None = None,
    ) -> None:
        with connect(project) as conn:
            conn.execute(
                "INSERT INTO provenance (memory_id, operation, details, actor) "
                "VALUES (?, ?, ?, ?)",
                [memory_id, operation, json.dumps(details) if details else None, actor],
            )

    # ---------- snapshot support ----------
    #
    # The committed snapshot carries the audit trail as well as the memories, so
    # a teammate's clone can see WHY a rule says what it says. `id` is left out
    # on purpose: it is an auto-increment integer, so two machines both mint an
    # id 5 for different entries and it is worthless as a cross-clone identity.
    # The natural key is the whole row, which is what dedupes on the way back in.

    def all_for_project(self, project: str) -> list[dict]:
        """Every provenance row, oldest first, as plain JSON-safe dicts."""
        with connect(project) as conn:
            rows = conn.execute(
                "SELECT memory_id, operation, details, actor, created_at "
                "FROM provenance ORDER BY created_at, memory_id"
            ).fetchall()
        return [
            {
                "memory_id": r[0],
                "operation": r[1],
                "details": _loads(r[2]),
                "actor": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ]

    def tombstones(self, project: str) -> list[dict]:
        """Hard deletes whose memory row is really gone - the only "deleted".

        A soft delete needs no tombstone: the row survives as
        `status='archived'` and travels as an ordinary edit. A hard delete
        removes the row, so without this the next machine to export would put
        the memory straight back - and the merge driver would have nothing to
        distinguish "deleted here" from "not pulled yet".

        Derived from provenance rather than a new table: `MemoryService.delete`
        already records `hard_delete` BEFORE removing the row, so the record
        outlives it. That means no schema change to the central store, and every
        hard delete ever performed is already tombstoned.
        """
        with connect(project) as conn:
            rows = conn.execute(
                """
                SELECT p.memory_id, max(p.created_at) AS deleted_at,
                       any_value(p.actor) AS actor
                FROM provenance p
                WHERE p.operation = 'hard_delete'
                  AND p.memory_id NOT IN (SELECT id FROM memories)
                GROUP BY p.memory_id
                ORDER BY p.memory_id
                """
            ).fetchall()
        return [
            {
                "memory_id": r[0],
                "category": None,
                "title": None,
                "deleted_at": r[1].isoformat() if r[1] else None,
                "actor": r[2],
            }
            for r in rows
        ]

    def known_keys(self, project: str) -> set[tuple]:
        """The natural key of every stored row, for dedup on import."""
        with connect(project) as conn:
            rows = conn.execute(
                "SELECT memory_id, operation, created_at FROM provenance"
            ).fetchall()
        return {
            (r[0], r[1], r[2].isoformat() if r[2] else None) for r in rows
        }

    def record_at(
        self,
        project: str,
        memory_id: str,
        operation: str,
        created_at,
        details: dict | None = None,
        actor: str | None = None,
    ) -> None:
        """Insert an entry keeping the timestamp it was written with elsewhere.

        `record` stamps current_timestamp, which is right for something that
        happens here and wrong for an entry arriving from another machine: it
        would re-date the audit trail on every clone that imports it, and the
        dedup key would never match.
        """
        with connect(project) as conn:
            conn.execute(
                "INSERT INTO provenance (memory_id, operation, details, actor, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                [
                    memory_id, operation,
                    json.dumps(details) if details else None,
                    actor, created_at,
                ],
            )

    def for_memory(self, project: str, memory_id: str) -> list[ProvenanceEntry]:
        with connect(project) as conn:
            rows = conn.execute(
                """
                SELECT id, memory_id, operation, details, actor, created_at
                FROM provenance WHERE memory_id = ?
                ORDER BY created_at ASC
                """,
                [memory_id],
            ).fetchall()

        entries: list[ProvenanceEntry] = []
        for r in rows:
            details = _loads(r[3])
            entries.append(
                ProvenanceEntry(
                    id=r[0], memory_id=r[1], operation=r[2],
                    details=details, actor=r[4], created_at=r[5],
                )
            )
        return entries
