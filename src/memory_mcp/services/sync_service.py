"""Project memory sync - mirror memory to a git-committable snapshot.

The snapshot is one DuckDB file, ``.claude-memory/memory.duckdb``, beside a
small ``manifest.json`` that carries the project's identity. It replaced one
JSON file per category, which was rewritten in full every session and only grew.
The ``session`` category is excluded (device-local) and embeddings are excluded
(regenerated on import).

This service only builds and applies snapshots in memory. The actual file I/O is
done by the ``memory-mcp sync`` CLI, which runs in Claude Code's context -
unlike the launchd daemon, it can reach project folders on the Desktop - and by
``db/snapshot.py``, which owns the file format and the merge.
"""

import json
from datetime import datetime

from memory_mcp.constants import SNAPSHOT_DIRNAME, SYNC_CATEGORIES
from memory_mcp.embeddings import embed_text
from memory_mcp.models import MemoryCategory
from memory_mcp.repositories import (
    MemoryRepository, ProjectRepository, ProvenanceRepository,
)
from memory_mcp.utils.text import prepare_embedding_text

__all__ = ["SyncService", "SNAPSHOT_DIRNAME", "SYNC_CATEGORIES"]

# Fields compared to decide whether a memory needs updating on import. Approval
# fields are included so an approve/revoke propagates through git sync; a rule
# arriving as 'proposed' is stored (and shows in the moderation queue) but is not
# enforced in server mode until approved - preserving "never delete / only newer".
_SYNC_FIELDS = (
    "title", "content", "summary", "status", "priority",
    "tags", "metadata", "related_ids", "entities",
    "approval_status", "created_by", "approved_by", "approved_at",
)


def _mem_to_dict(memory) -> dict:
    """Serialize a Memory for the snapshot - without the embedding or stats.

    `pending` is dropped too: it is device-local staging state, and a pending
    memory never reaches a snapshot in the first place (build_snapshot excludes
    it). Writing the flag would only add a constant `false` to every entry.
    """
    data = memory.model_dump(mode="json")
    data.pop("embedding", None)
    data.pop("access_count", None)
    data.pop("pending", None)
    return data


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _is_newer(candidate, reference) -> bool:
    """True only when `candidate` is strictly newer than `reference`.

    Naive comparison after stripping tzinfo: the store writes naive local
    timestamps, and a mixed-awareness comparison raises rather than answering.
    An unknown timestamp on either side is never "newer".
    """
    if candidate is None or reference is None:
        return False
    try:
        left = candidate.replace(tzinfo=None)
        right = reference.replace(tzinfo=None)
    except AttributeError:
        return False
    return left > right


class SyncService:
    """Build per-category snapshots and reconcile a project DB back from them."""

    def __init__(
        self,
        memory_repo: MemoryRepository,
        project_repo: ProjectRepository,
        provenance_repo: ProvenanceRepository | None = None,
    ):
        self._memory_repo = memory_repo
        self._project_repo = project_repo
        self._provenance_repo = provenance_repo or ProvenanceRepository()

    # ---------- export ----------

    def build_snapshot(self, project: str) -> dict[str, list]:
        """Return {category: [memory-dict, ...]} for synced, non-empty categories."""
        snapshot: dict[str, list] = {}
        for category in SYNC_CATEGORIES:
            memories = self._memory_repo.all_for_categories(project, [category])
            if memories:
                snapshot[category] = [_mem_to_dict(m) for m in memories]
        return snapshot

    def build_full_snapshot(self, project: str) -> dict:
        """Everything the committed snapshot carries: memories, audit, tombstones.

        Separate from `build_snapshot` rather than replacing it: the categories
        are the part every caller wants, and a route that only needs those should
        not pay for the whole audit trail.
        """
        return {
            "categories": self.build_snapshot(project),
            "provenance": self._provenance_repo.all_for_project(project),
            "tombstones": self._provenance_repo.tombstones(project),
        }

    # ---------- import / reconcile ----------

    def apply_snapshot(
        self,
        project: str,
        categories: dict,
        reconcile: list[str],
        provenance: list[dict] | None = None,
        tombstones: list[dict] | None = None,
    ) -> dict:
        """Reconcile the project DB toward the snapshot - additively and safely.

        ABSENCE IS NEVER DELETION. A memory present locally but absent from the
        snapshot is kept: a stale snapshot (e.g. an export missed because a
        chat was deleted before its turn ended) must not be able to destroy
        rules, and on a machine that has not pulled yet, "absent" only means
        "not seen". An entry that differs is updated only when the snapshot's
        copy is strictly newer, so a stale snapshot also cannot revert a local
        edit.

        An explicit TOMBSTONE is the one exception, and it is a different thing
        from absence: it is the record of somebody running a hard delete, it
        names the memory and says when, and without it a hard delete never
        reaches the other clones - the next machine to export simply puts the
        memory back. See `_apply_tombstones` for the guard that keeps a newer
        local edit alive.

        Only categories in `reconcile` are considered - a category whose
        snapshot file failed to parse is skipped entirely.
        """
        added = updated = kept_local = 0
        for category in reconcile:
            if category not in SYNC_CATEGORIES:
                continue
            wanted = {
                m["id"]: m for m in categories.get(category, []) if m.get("id")
            }
            current = {
                m.id: m
                for m in self._memory_repo.all_for_categories(project, [category])
            }
            for mid, md in wanted.items():
                if mid not in current:
                    self._insert(project, md)
                    added += 1
                elif self._differs(md, current[mid]) and self._snapshot_newer(
                    md, current[mid]
                ):
                    self._update(project, md, current[mid])
                    updated += 1
            kept_local += sum(1 for mid in current if mid not in wanted)

        result = {"added": added, "updated": updated, "kept_local_only": kept_local}
        result.update(self._apply_tombstones(project, tombstones or []))
        result["provenance_added"] = self._apply_provenance(project, provenance or [])
        return result

    def _apply_tombstones(self, project: str, tombstones: list[dict]) -> dict:
        """Apply hard deletes recorded on another machine.

        The guard: the local row is removed only when it has NOT been edited
        after the tombstone was written. An edit that is strictly newer is a
        deliberate resurrection - somebody re-added the rule knowing it had been
        removed - and it wins, exactly as `_snapshot_newer` decides every other
        conflict here.

        The removal records its own `hard_delete` provenance entry first, so the
        audit trail keeps the fact even though the row is gone. That is the same
        order `MemoryService.delete` uses, and it is what lets the next export
        re-derive this tombstone instead of losing it.
        """
        deleted = resurrected = 0
        for tomb in tombstones:
            mid = tomb.get("memory_id")
            if not mid:
                continue
            existing = self._memory_repo.get_by_id(project, mid)
            if existing is None:
                continue
            deleted_at = _parse_dt(tomb.get("deleted_at"))
            if _is_newer(existing.updated_at, deleted_at):
                resurrected += 1
                continue
            self._provenance_repo.record_at(
                project, mid, "hard_delete", deleted_at,
                {"source": "sync", "actor": tomb.get("actor")},
            )
            self._memory_repo.hard_delete(project, mid)
            deleted += 1
        return {"tombstoned": deleted, "kept_newer_than_tombstone": resurrected}

    def _apply_provenance(self, project: str, entries: list[dict]) -> int:
        """Union the incoming audit trail in. Append-only: nothing is replaced.

        Deduped on (memory_id, operation, created_at) - the id column is a local
        auto-increment integer and means nothing across clones, so it cannot be
        the key.
        """
        if not entries:
            return 0
        known = self._provenance_repo.known_keys(project)
        added = 0
        for entry in entries:
            mid, op = entry.get("memory_id"), entry.get("operation")
            if not mid or not op:
                continue
            created = _parse_dt(entry.get("created_at"))
            key = (mid, op, created.isoformat() if created else None)
            if key in known:
                continue
            self._provenance_repo.record_at(
                project, mid, op, created, entry.get("details"), entry.get("actor"),
            )
            known.add(key)
            added += 1
        return added

    def _differs(self, md: dict, memory) -> bool:
        current = _mem_to_dict(memory)
        return any(md.get(f) != current.get(f) for f in _SYNC_FIELDS)

    def _snapshot_newer(self, md: dict, memory) -> bool:
        """True only when the snapshot's copy is strictly newer than the DB's.

        Prevents a stale snapshot from overwriting a more recent local edit.
        """
        snap = _parse_dt(md.get("updated_at"))
        if snap is None:
            return False
        current = memory.updated_at
        if current is None:
            return True
        try:
            snap = snap.replace(tzinfo=None)
            current = current.replace(tzinfo=None)
            return snap > current
        except Exception:  # noqa: BLE001
            return False

    def _insert(self, project: str, md: dict) -> None:
        embedding = embed_text(prepare_embedding_text(md["title"], md["content"]))
        self._memory_repo.insert(
            project=project,
            memory_id=md["id"],
            category=md["category"],
            title=md["title"],
            content=md["content"],
            summary=md.get("summary"),
            tags=md.get("tags") or [],
            metadata=md.get("metadata"),
            embedding=embedding,
            priority=md.get("priority", 0),
            source=md.get("source") or "sync",
            related_ids=md.get("related_ids") or [],
            entities=md.get("entities") or [],
            expires_at=_parse_dt(md.get("expires_at")),
            status=md.get("status") or "active",
            created_by=md.get("created_by"),
            approval_status=md.get("approval_status") or "approved",
        )

    def _update(self, project: str, md: dict, current) -> None:
        fields: dict = {
            "title": md["title"],
            "content": md["content"],
            "summary": md.get("summary"),
            "status": md.get("status") or "active",
            "priority": md.get("priority", 0),
            "tags": md.get("tags") or [],
            "metadata": json.dumps(md["metadata"]) if md.get("metadata") else None,
            "related_ids": md.get("related_ids") or [],
            "entities": md.get("entities") or [],
            "approval_status": md.get("approval_status") or "approved",
            "created_by": md.get("created_by"),
            "approved_by": md.get("approved_by"),
            "approved_at": _parse_dt(md.get("approved_at")),
        }
        if md["title"] != current.title or md["content"] != current.content:
            fields["embedding"] = embed_text(
                prepare_embedding_text(md["title"], md["content"])
            )
        self._memory_repo.update(project, md["id"], fields)
