"""Staging: a home for short-lived working state.

Sessions kept progress, plans and intermediate findings in `.md` files and in
`CLAUDE.md`, because nothing else held working state. Those files are invisible
to the board, not project-scoped, and outlive the work they were for. A staging
memory is that working state, kept here instead (user decisions, 2026-11-20):

- tied to a task through `metadata.task_id`, it lives until the task is done;
  the close folds every durable note into the task as ONE comment and removes
  the staging rows;
- with no task it is allowed, and expires a week after it was written;
- search, listing and the digest leave staging out unless asked for, so
  working notes never crowd long-term recall.

The fold is called by TaskService through an injected callable, so the task
store never imports this module and still works with it absent.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from memory_mcp.exceptions import TaskNotFoundError
from memory_mcp.models import (
    Memory, MemoryCategory, STAGING_DURABLE_KEY, STAGING_TASK_KEY,
    StoreMemoryRequest, TaskState,
)
from memory_mcp.repositories import MemoryRepository, ProvenanceRepository, TaskRepository
from memory_mcp.services.memory_service import MemoryService

#: A task that is finished no longer collects working notes: nothing would
#: ever fold them.
CLOSED_TASK_STATES = frozenset({TaskState.DONE, TaskState.CANCELLED, TaskState.DUPLICATE})
#: The most notes one fold reads. Far above real use; a task past it has the
#: overflow named in the comment rather than silently dropped.
FOLD_READ_LIMIT = 500
#: The folded comment is capped so one runaway note cannot produce a comment
#: the board refuses. The cut is stated in the comment itself.
FOLD_COMMENT_MAX_CHARS = 16_000
#: The default and ceiling for a staging listing.
NOTES_DEFAULT_LIMIT = 50
NOTES_MAX_LIMIT = 200
#: The provenance operation recorded on the task when its notes are folded.
FOLD_OPERATION = "staging_folded"


@dataclass(frozen=True)
class FoldResult:
    """What a task close did with that task's staging notes."""

    folded: int
    removed: int


def is_durable(note: Memory) -> bool:
    """Notes are durable unless they were staged with durable=False."""
    return (note.metadata or {}).get(STAGING_DURABLE_KEY, True) is not False


def fold_comment(notes: Sequence[Memory], overflowed: bool) -> str | None:
    """The single task comment a close leaves behind, or None when nothing
    durable was staged."""
    if not notes:
        return None
    count = len(notes)
    lines = [f"**Working notes folded in when the task was closed** ({count})", ""]
    for note in notes:
        lines.append(f"**{note.title}**")
        lines.append(note.content.strip())
        lines.append("")
    if overflowed:
        lines.append(
            f"_Only the first {FOLD_READ_LIMIT} notes were read; the rest were "
            "removed with the task's other staging notes._"
        )
    body = "\n".join(lines).strip()
    if len(body) <= FOLD_COMMENT_MAX_CHARS:
        return body
    omitted = len(body) - FOLD_COMMENT_MAX_CHARS
    return (
        body[:FOLD_COMMENT_MAX_CHARS].rstrip()
        + f"\n\n_Shortened: {omitted} characters were left out._"
    )


class StagingService:
    """Stage working notes, list them, and fold them into a task at close."""

    def __init__(
        self,
        memory_service: MemoryService,
        memory_repo: MemoryRepository,
        task_repo: TaskRepository,
        provenance_repo: ProvenanceRepository,
    ):
        self._memory_service = memory_service
        self._memory_repo = memory_repo
        self._task_repo = task_repo
        self._provenance_repo = provenance_repo

    def stage(
        self,
        project: str,
        title: str,
        content: str,
        task_id: str | None = None,
        durable: bool = True,
        tags: Sequence[str] = (),
        source: str = "assistant",
    ) -> Memory:
        """Store one working note. A task, when named, must exist and be open."""
        if task_id:
            self._require_open_task(project, task_id)
        # Expired rows are already invisible; this keeps them from piling up.
        self._memory_repo.purge_expired_staging(project)
        metadata: dict = {STAGING_DURABLE_KEY: durable}
        if task_id:
            metadata[STAGING_TASK_KEY] = task_id
        request = StoreMemoryRequest(
            project=project,
            category=MemoryCategory.STAGING,
            title=title,
            content=content,
            tags=list(tags),
            metadata=metadata,
            source=source,
        )
        return self._memory_service.store(request)

    def notes(
        self, project: str, task_id: str | None = None, limit: int = NOTES_DEFAULT_LIMIT,
    ) -> tuple[Memory, ...]:
        """A task's notes (oldest first), or every live note (newest first)."""
        bounded = max(1, min(limit, NOTES_MAX_LIMIT))
        if task_id:
            found = self._memory_repo.staging_for_task(project, task_id, bounded)
        else:
            found = self._memory_repo.staging_live(project, bounded)
        return tuple(found)

    def fold(
        self, project: str, task_id: str, add_comment: Callable[[str], object],
    ) -> FoldResult:
        """Fold a closing task's durable notes into one comment, then remove
        every staging row the task had.

        The comment is written first: if it fails, the notes are still there
        and the caller's close fails with it, rather than losing both.
        """
        notes = self._memory_repo.staging_for_task(project, task_id, FOLD_READ_LIMIT)
        if not notes:
            return FoldResult(folded=0, removed=0)
        durable = tuple(n for n in notes if is_durable(n))
        overflowed = len(notes) >= FOLD_READ_LIMIT
        body = fold_comment(durable, overflowed)
        if body:
            add_comment(body)
        removed = self._memory_repo.delete_staging_for_task(project, task_id)
        self._provenance_repo.record(
            project, task_id, FOLD_OPERATION,
            {"folded": len(durable), "removed": removed,
             "memory_ids": [n.id for n in notes]},
        )
        return FoldResult(folded=len(durable), removed=removed)

    def _require_open_task(self, project: str, task_id: str) -> None:
        task = self._task_repo.get(project, task_id)
        if task is None:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        if task.state in CLOSED_TASK_STATES:
            raise ValueError(
                f"Task {task_id} is {task.state.value}; staging notes belong to "
                "open work. Stage without task_id, or reopen the task first."
            )
