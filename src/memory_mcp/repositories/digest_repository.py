"""Digest repository - all SQL for the memory digest's proposal store.

A digest is a review pass over a project's memories: analysed, proposed,
decided on op by op, then applied. Every one of those stages is persisted
rather than carried in the agent's context, because the proposal the user
approved has to be the exact one that gets written.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from memory_mcp.db.connection import connect
from memory_mcp.models import Digest, DigestOp

DIGEST_COLUMNS = (
    "id, project, state, analysis, notes, created_at, proposed_at, "
    "applied_at, reverted_at"
)
OP_COLUMNS = (
    "id, digest_id, position, op, memory_ids, target_id, payload, "
    "before_json, decision, decided_at, applied_at, error"
)


def _loads(value) -> dict | None:
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _row_to_digest(row, ops: list[DigestOp] | None = None) -> Digest:
    return Digest(
        id=row[0],
        project=row[1],
        state=row[2],
        analysis=_loads(row[3]),
        notes=row[4],
        created_at=row[5],
        proposed_at=row[6],
        applied_at=row[7],
        reverted_at=row[8],
        ops=ops or [],
    )


def _row_to_op(row) -> DigestOp:
    return DigestOp(
        id=row[0],
        digest_id=row[1],
        position=row[2] or 0,
        op=row[3],
        memory_ids=list(row[4] or []),
        target_id=row[5],
        payload=_loads(row[6]) or {},
        before=_loads(row[7]),
        decision=row[8],
        decided_at=row[9],
        applied_at=row[10],
        error=row[11],
    )


class DigestRepository:
    """All digest-related SQL, centralized."""

    # ---------- Digests ----------

    def create(self, project: str, analysis: dict | None, notes: str | None = None) -> Digest:
        digest_id = str(uuid.uuid4())
        with connect(project) as conn:
            conn.execute(
                "INSERT INTO digests (id, project, state, analysis, notes) "
                "VALUES (?, ?, 'open', ?, ?)",
                [digest_id, project, json.dumps(analysis) if analysis else None, notes],
            )
            row = conn.execute(
                f"SELECT {DIGEST_COLUMNS} FROM digests WHERE id = ?", [digest_id]
            ).fetchone()
        return _row_to_digest(row)

    def get(self, project: str, digest_id: str) -> Digest | None:
        """A digest with its ops in proposal order."""
        with connect(project) as conn:
            row = conn.execute(
                f"SELECT {DIGEST_COLUMNS} FROM digests WHERE id = ?", [digest_id]
            ).fetchone()
            if row is None:
                return None
            op_rows = conn.execute(
                f"SELECT {OP_COLUMNS} FROM digest_ops WHERE digest_id = ? "
                "ORDER BY position, id",
                [digest_id],
            ).fetchall()
        return _row_to_digest(row, [_row_to_op(r) for r in op_rows])

    def list(self, project: str, limit: int = 20, state: str | None = None) -> list[Digest]:
        """Recent digests, newest first. Ops are not loaded - use `get`."""
        where, params = "", []
        if state:
            where, params = "WHERE state = ?", [state]
        with connect(project) as conn:
            rows = conn.execute(
                f"SELECT {DIGEST_COLUMNS} FROM digests {where} "
                "ORDER BY created_at DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            counts = dict(
                conn.execute(
                    "SELECT digest_id, COUNT(*) FROM digest_ops GROUP BY digest_id"
                ).fetchall()
            )
        digests = []
        for row in rows:
            digest = _row_to_digest(row)
            digest.op_count = int(counts.get(digest.id, 0))
            digests.append(digest)
        return digests

    def set_state(self, project: str, digest_id: str, state: str, stamp: str | None = None) -> None:
        """Move a digest's state, optionally stamping one of its timestamp columns.

        `stamp` is a column name from a closed set - never caller text - so the
        interpolation below cannot carry anything but one of those four names.
        """
        allowed = {"proposed_at", "applied_at", "reverted_at", None}
        if stamp not in allowed:
            raise ValueError(f"unknown digest timestamp column: {stamp!r}")
        sets = ["state = ?"]
        params: list = [state]
        if stamp:
            sets.append(f"{stamp} = ?")
            params.append(datetime.now(timezone.utc))
        params.append(digest_id)
        with connect(project) as conn:
            conn.execute(f"UPDATE digests SET {', '.join(sets)} WHERE id = ?", params)

    def set_analysis(self, project: str, digest_id: str, analysis: dict | None) -> None:
        """Replace a digest's analysis. Only ever called on an open digest - one
        that has no ops and no decisions, so nothing is lost by refreshing it."""
        with connect(project) as conn:
            conn.execute(
                "UPDATE digests SET analysis = ? WHERE id = ?",
                [json.dumps(analysis) if analysis else None, digest_id],
            )

    def set_notes(self, project: str, digest_id: str, notes: str | None) -> None:
        with connect(project) as conn:
            conn.execute("UPDATE digests SET notes = ? WHERE id = ?", [notes, digest_id])

    # ---------- Ops ----------

    def replace_ops(self, project: str, digest_id: str, ops: list[dict]) -> list[DigestOp]:
        """Install the proposed op set, replacing any earlier proposal wholesale.

        A re-propose is a new proposal, not an addition: the agent hands back the
        complete set it wants the user to see, and leaving half of a superseded
        one behind would put ops in the diff nobody proposed. Only ever called
        while the digest is still awaiting a decision - `DigestService` refuses to
        re-propose an applied digest.
        """
        with connect(project) as conn:
            conn.execute("DELETE FROM digest_ops WHERE digest_id = ?", [digest_id])
            for position, op in enumerate(ops):
                conn.execute(
                    """
                    INSERT INTO digest_ops
                        (id, digest_id, position, op, memory_ids, target_id,
                         payload, decision)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    [
                        op["id"], digest_id, position, op["op"],
                        list(op.get("memory_ids") or []),
                        op.get("target_id"),
                        json.dumps(op.get("payload") or {}),
                    ],
                )
            rows = conn.execute(
                f"SELECT {OP_COLUMNS} FROM digest_ops WHERE digest_id = ? "
                "ORDER BY position, id",
                [digest_id],
            ).fetchall()
        return [_row_to_op(r) for r in rows]

    def decide(self, project: str, op_ids: list[str], decision: str) -> int:
        """Record the user's verdict on a set of ops. Returns rows touched."""
        if not op_ids:
            return 0
        placeholders = ",".join("?" * len(op_ids))
        with connect(project) as conn:
            conn.execute(
                f"UPDATE digest_ops SET decision = ?, decided_at = ? "
                f"WHERE id IN ({placeholders})",
                [decision, datetime.now(timezone.utc), *op_ids],
            )
        return len(op_ids)

    def record_before(self, project: str, op_id: str, before: dict) -> None:
        """Store the pre-apply image of everything this op is about to overwrite."""
        with connect(project) as conn:
            conn.execute(
                "UPDATE digest_ops SET before_json = ? WHERE id = ?",
                [json.dumps(before), op_id],
            )

    def mark_applied(self, project: str, op_id: str, target_id: str | None = None) -> None:
        sets = ["applied_at = ?"]
        params: list = [datetime.now(timezone.utc)]
        if target_id:
            sets.append("target_id = ?")
            params.append(target_id)
        params.append(op_id)
        with connect(project) as conn:
            conn.execute(f"UPDATE digest_ops SET {', '.join(sets)} WHERE id = ?", params)

    def mark_error(self, project: str, op_id: str, error: str) -> None:
        with connect(project) as conn:
            conn.execute(
                "UPDATE digest_ops SET error = ? WHERE id = ?", [error[:2000], op_id]
            )
