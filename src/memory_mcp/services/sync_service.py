"""Project memory sync - mirror memory to a git-committable text snapshot.

The snapshot is one JSON file per category under ``<project>/.claude-memory/``
plus a manifest. The ``session`` category is excluded (device-local) and
embeddings are excluded (regenerated on import).

This service only builds and applies snapshots in memory. The actual file I/O
is done by the ``memory-mcp sync`` CLI, which runs in Claude Code's context -
unlike the launchd daemon, it can reach project folders on the Desktop.
"""

import json
from datetime import datetime

from memory_mcp.embeddings import embed_text
from memory_mcp.models import MemoryCategory
from memory_mcp.repositories import MemoryRepository, ProjectRepository
from memory_mcp.utils.text import prepare_embedding_text

SNAPSHOT_DIRNAME = ".claude-memory"
SYNC_CATEGORIES = [c.value for c in MemoryCategory if c.value != "session"]

# Fields compared to decide whether a memory needs updating on import.
_SYNC_FIELDS = (
    "title", "content", "summary", "status", "priority",
    "tags", "metadata", "related_ids", "entities",
)


def _mem_to_dict(memory) -> dict:
    """Serialize a Memory for the snapshot - without the embedding or stats."""
    data = memory.model_dump(mode="json")
    data.pop("embedding", None)
    data.pop("access_count", None)
    return data


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


class SyncService:
    """Build per-category snapshots and reconcile a project DB back from them."""

    def __init__(self, memory_repo: MemoryRepository, project_repo: ProjectRepository):
        self._memory_repo = memory_repo
        self._project_repo = project_repo

    # ---------- export ----------

    def build_snapshot(self, project: str) -> dict[str, list]:
        """Return {category: [memory-dict, ...]} for synced, non-empty categories."""
        snapshot: dict[str, list] = {}
        for category in SYNC_CATEGORIES:
            memories = self._memory_repo.all_for_categories(project, [category])
            if memories:
                snapshot[category] = [_mem_to_dict(m) for m in memories]
        return snapshot

    # ---------- import / reconcile ----------

    def apply_snapshot(
        self, project: str, categories: dict, reconcile: list[str],
    ) -> dict:
        """Reconcile the project DB to match the snapshot.

        Only categories listed in `reconcile` are touched - a category whose
        snapshot file failed to parse is left out so its DB rows are preserved.
        """
        added = updated = deleted = 0
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
                elif self._differs(md, current[mid]):
                    self._update(project, md, current[mid])
                    updated += 1
            for mid in current:
                if mid not in wanted:
                    self._memory_repo.hard_delete(project, mid)
                    deleted += 1
        return {"added": added, "updated": updated, "deleted": deleted}

    def _differs(self, md: dict, memory) -> bool:
        current = _mem_to_dict(memory)
        return any(md.get(f) != current.get(f) for f in _SYNC_FIELDS)

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
        }
        if md["title"] != current.title or md["content"] != current.content:
            fields["embedding"] = embed_text(
                prepare_embedding_text(md["title"], md["content"])
            )
        self._memory_repo.update(project, md["id"], fields)
