"""Project memory sync - mirror memory to a git-committable snapshot.

The snapshot is one DuckDB file, ``.agent-memory/memory.duckdb``, beside a
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
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import duckdb

from memory_mcp.constants import SNAPSHOT_DIRNAME, SYNC_CATEGORIES
from memory_mcp.db.connection import transaction
from memory_mcp.embeddings import embed_text
from memory_mcp.repositories import (
    MemoryRepository, ProjectRepository, ProvenanceRepository, SessionRepository,
    TaskRepository,
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

# One import per project at a time. The daemon serves HTTP on a thread pool, and
# every worktree's SessionStart hook posts an import: two of them planning
# against the same rows would both insert the same new id. The lock is
# per-process, which is enough - only the daemon ever writes the central store.
_IMPORT_LOCKS: dict[str, threading.Lock] = {}
_IMPORT_LOCKS_GUARD = threading.Lock()

# A write-write conflict with another writer (a memory_store landing on the same
# row mid-import) aborts the whole transaction, never half of it. One retry
# re-plans against what that writer committed; a second conflict is reported.
_CONFLICT_RETRIES = 1

# Upper bound on the branch-mark listing a session-start hook asks for.
BRANCH_MARK_LIMIT = 200
# A git ref name is far shorter in practice; this only bounds hostile input.
_BRANCH_NAME_MAX = 255
_PATH_MAX = 4096


def _import_lock(project: str) -> threading.Lock:
    with _IMPORT_LOCKS_GUARD:
        return _IMPORT_LOCKS.setdefault(project, threading.Lock())


@dataclass(frozen=True)
class _ImportPlan:
    """What an import will do, decided before the write transaction opens.

    Embeddings are computed here, outside the transaction, so it is held only
    for the writes themselves.
    """

    inserts: tuple[tuple[dict, list[float]], ...]
    updates: tuple[tuple[dict, list[float] | None], ...]
    kept_local: int
    kept_deleted: int


def _clean_text(value: object, limit: int, what: str) -> str:
    """A request string at the edge: required, bounded, single-line."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} is required")
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f"{what} is longer than {limit} characters")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError(f"{what} contains control characters")
    return text


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
        task_repo: TaskRepository | None = None,
        session_repo: SessionRepository | None = None,
    ):
        self._memory_repo = memory_repo
        self._project_repo = project_repo
        self._provenance_repo = provenance_repo or ProvenanceRepository()
        self._task_repo = task_repo or TaskRepository()
        self._session_repo = session_repo or SessionRepository()

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

        A STALE SNAPSHOT CANNOT RESURRECT EITHER. A memory this store hard
        deleted is absent here, and absent used to mean "insert it". It is now
        inserted only when the snapshot's row is strictly newer than the
        deletion: that is somebody re-adding it on purpose, and a newer re-add
        wins. An older row is the snapshot not having heard of the delete yet.

        An explicit TOMBSTONE is the one way a snapshot deletes, and it is a
        different thing from absence: it is the record of somebody running a
        hard delete, it names the memory and says when, and without it a hard
        delete never reaches the other clones. See `_apply_tombstones` for the
        guard that keeps a newer local edit alive.

        Serialised per project and applied in ONE transaction: several
        worktrees' SessionStart hooks post imports at once, and two of them
        planning against the same rows used to insert the same id twice. A
        failure now leaves the store exactly as it was.

        Only categories in `reconcile` are considered - a category whose
        snapshot file failed to parse is skipped entirely.
        """
        with _import_lock(project):
            for attempt in range(_CONFLICT_RETRIES + 1):
                try:
                    return self._apply_locked(
                        project, categories, reconcile,
                        provenance or [], tombstones or [],
                    )
                except duckdb.TransactionException:
                    if attempt == _CONFLICT_RETRIES:
                        raise
        raise AssertionError("unreachable")  # the loop returns or raises

    def _apply_locked(
        self,
        project: str,
        categories: dict,
        reconcile: list[str],
        provenance: list[dict],
        tombstones: list[dict],
    ) -> dict:
        plan = self._plan(project, categories, reconcile)
        added = updated = 0
        with transaction(project):
            for md, embedding in plan.inserts:
                # Re-checked inside the transaction: another writer may have
                # created the row since the plan was made.
                if self._memory_repo.get_by_id(project, md["id"]) is not None:
                    continue
                self._insert(project, md, embedding)
                added += 1
            for md, embedding in plan.updates:
                current = self._memory_repo.get_by_id(project, md["id"])
                if current is None or not self._differs(md, current):
                    continue
                if not self._snapshot_newer(md, current):
                    continue
                self._update(project, md, current, embedding)
                updated += 1
            result = {
                "added": added,
                "updated": updated,
                "kept_local_only": plan.kept_local,
                "kept_deleted": plan.kept_deleted,
            }
            result.update(self._apply_tombstones(project, tombstones))
            result["provenance_added"] = self._apply_provenance(project, provenance)
        return result

    def _plan(self, project: str, categories: dict, reconcile: list[str]) -> _ImportPlan:
        """Decide every insert and update, and compute their embeddings."""
        deleted_here = {
            t["memory_id"]: _parse_dt(t.get("deleted_at"))
            for t in self._provenance_repo.tombstones(project)
            if t.get("memory_id")
        }
        inserts: list[tuple[dict, list[float]]] = []
        updates: list[tuple[dict, list[float] | None]] = []
        kept_local = kept_deleted = 0
        for category in reconcile:
            if category not in SYNC_CATEGORIES:
                continue
            wanted = {
                m["id"]: m for m in categories.get(category, []) if m.get("id")
            }
            stored = self._memory_repo.all_for_categories(project, [category])
            current = {m.id: m for m in stored}
            for mid, md in wanted.items():
                existing = current.get(mid)
                if existing is None:
                    if mid in deleted_here and not _is_newer(
                        _parse_dt(md.get("updated_at")), deleted_here[mid]
                    ):
                        kept_deleted += 1
                        continue
                    text = prepare_embedding_text(md["title"], md["content"])
                    inserts.append((md, embed_text(text)))
                    continue
                if not (self._differs(md, existing) and self._snapshot_newer(md, existing)):
                    continue
                embedding = None
                if md["title"] != existing.title or md["content"] != existing.content:
                    text = prepare_embedding_text(md["title"], md["content"])
                    embedding = embed_text(text)
                updates.append((md, embedding))
            kept_local += sum(1 for mid in current if mid not in wanted)
        return _ImportPlan(
            inserts=tuple(inserts), updates=tuple(updates),
            kept_local=kept_local, kept_deleted=kept_deleted,
        )

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

    def _insert(self, project: str, md: dict, embedding: list[float]) -> None:
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

    def _update(
        self, project: str, md: dict, current, embedding: list[float] | None,
    ) -> None:
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
        title_or_content_changed = (
            md["title"] != current.title or md["content"] != current.content
        )
        if title_or_content_changed:
            if embedding is None:
                text = prepare_embedding_text(md["title"], md["content"])
                embedding = embed_text(text)
            fields["embedding"] = embedding
        self._memory_repo.update(project, md["id"], fields)

    # ---------- worktree branch marks ----------
    #
    # Every linked worktree writes into this one store. Without a record of
    # where a change came from, a task finished on an unmerged branch reads as
    # plainly done on main (the user's decision of 2026-11-20: visible, but
    # marked with its branch). The daemon cannot run git in a TCC-protected
    # folder, so the CLI inside each worktree reports its branch
    # (`register_worktree`) and, from the main checkout, which marked branches
    # git now sees as merged (`clear_merged_branches`).

    def register_worktree(self, project: str, path: str, branch: str) -> dict:
        """Record the branch a linked worktree folder has checked out."""
        clean_path = _clean_text(path, _PATH_MAX, "path")
        clean_branch = _clean_text(branch, _BRANCH_NAME_MAX, "branch")
        self._task_repo.register_worktree(project, clean_path, clean_branch)
        return {"path": clean_path, "branch": clean_branch}

    def branch_for_session(self, project: str, session_id: str | None) -> tuple[bool, str | None]:
        """(known, branch) for the folder a memory session started in.

        `known` is False when the session recorded no folder - an older build,
        or a write from the UI - and the caller must then leave a mark alone
        rather than clear it on a guess. A known folder outside every
        registered worktree is the main checkout: (True, None).
        """
        if not session_id:
            return False, None
        meta = self._session_repo.metadata(project, session_id) or {}
        cwd = meta.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return False, None
        return True, self._task_repo.worktree_branch(project, cwd.rstrip("/"))

    def stamp_task_branch(
        self, project: str, task_id: str, session_id: str | None,
    ) -> str | None:
        """Mark a task with the branch of the session that just set its state.

        Call after any state change made on behalf of a memory session. From a
        registered worktree the task is marked with its branch; from the main
        checkout the mark is cleared, because main has now decided the state
        itself. A session with no recorded folder changes nothing. Returns the
        task's mark after the call.
        """
        known, branch = self.branch_for_session(project, session_id)
        if known:
            self._task_repo.set_branch(project, task_id, branch)
            return branch
        task = self._task_repo.get(project, task_id)
        return task.branch if task else None

    def branch_marks(self, project: str, limit: int = BRANCH_MARK_LIMIT) -> dict:
        """Tasks whose state was set on a still-unmerged branch, by branch."""
        tasks = self._task_repo.branch_marked(project, limit + 1)
        truncated = len(tasks) > limit
        by_branch: dict[str, list[dict]] = {}
        for task in tasks[:limit]:
            entry = {"id": task.id, "title": task.title, "state": task.state.value}
            by_branch.setdefault(task.branch or "", []).append(entry)
        branches = tuple(
            {"branch": name, "tasks": entries} for name, entries in by_branch.items()
        )
        return {"branches": list(branches), "truncated": truncated}

    def clear_merged_branches(self, project: str, branches: Sequence[str]) -> dict:
        """Drop the marks of branches git reports as merged into main."""
        if not isinstance(branches, (list, tuple)):
            raise ValueError("branches must be a list")
        if len(branches) > BRANCH_MARK_LIMIT:
            raise ValueError(f"at most {BRANCH_MARK_LIMIT} branches at a time")
        clean = tuple(
            _clean_text(name, _BRANCH_NAME_MAX, "branch") for name in branches
        )
        return {"cleared": self._task_repo.clear_branches(project, clean)}
