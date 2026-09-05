"""Task service - a standalone task store: capture, lifecycle, comments, time.

The point of this store is capture without interruption: the user drops a
requirement into the list at any moment, and it waits there until they ask for
it. A task is therefore a QUEUED REQUIREMENT, never an instruction - session
start surfaces the list, and nothing in it is picked up unless the user says so.
That prompt-contract is the feature; the storage is the easy part.

Everything here works with no external system attached. Phase 2 mirrors tasks to
an asoode board through an outbox, but a missing or unreachable asoode may only
ever mean "mirroring is paused" - never a degraded local task store.

THE MIRROR CONTRACT, stated once because it was missed one layer at a time: the
local write happens first and always; then, when the project is bound, EVERY
mutation the platform can represent enqueues an outbox op - state, fields,
labels, assignee, parent, comments, attachments, time, archive, delete. A
mutation that the board cannot see is a gap, not a simplification.

THE CLOCK CONTRACT, same reason: every path that ends a stretch of work closes
the open time entry and queues it for mirroring - stop, done, any state change
away from in_progress, release, session end, archive. A clock that only the
explicit `stop` could close was how a finished task kept ticking for hours.
"""

import contextlib
import contextvars
import threading
import uuid

from memory_mcp.config import settings
from memory_mcp.context import current_user
from memory_mcp.db.connection import after_commit
from memory_mcp.exceptions import MemoryMCPError, TaskNotFoundError
from memory_mcp.models import (
    CreateTaskRequest, Task, TaskComment, TaskCommentKind, TaskDetail, TaskFilter,
    TaskListResponse, TaskRowMeta, TaskState, TaskTimeEntry, UpdateTaskRequest,
)
from memory_mcp.repositories import (
    ProjectRepository, ProvenanceRepository, SessionRepository, TaskRepository,
)

# States a task can be resumed from. Starting work on a finished task reopens it.
_CLOSED_STATES = {TaskState.DONE, TaskState.CANCELLED, TaskState.DUPLICATE}

# How long a claim survives without the holding session touching the task. Long
# enough for a real stretch of work, short enough that a crashed session's task
# comes back on its own. Checked lazily on the next claim attempt - no sweeper.
CLAIM_LEASE_MINUTES = 30

# How many candidates claim_next will try before giving up for this call.
_CLAIM_ATTEMPTS = 10

# Fields a change to which is worth a remote call. Position, claim and lease are
# local bookkeeping; everything else here has a field on the board.
_MIRRORED_FIELDS = frozenset({
    "state", "title", "description", "priority", "assignee", "labels",
    "due_at", "begin_at", "end_at", "estimated_minutes",
})

# Set while an INBOUND write is being applied, so it is not mirrored straight
# back out. A contextvar rather than an attribute on the service: the service is
# a process-wide singleton, and blanking its outbox for the duration of an
# import silently dropped the mirror of every concurrent tool call on every
# project - and, when two imports interleaved, left mirroring dead until the
# daemon restarted.
_MIRROR_SUPPRESSED: contextvars.ContextVar[int] = contextvars.ContextVar(
    "mirror_suppressed", default=0,
)


class TaskService:
    """Business logic for the task store."""

    def __init__(
        self,
        task_repo: TaskRepository,
        provenance_repo: ProvenanceRepository,
        project_repo: ProjectRepository,
        session_repo: SessionRepository,
        link_resolver=None,
        outbox_repo=None,
        mirror=None,
        attachment_repo=None,
    ):
        self._task_repo = task_repo
        self._provenance_repo = provenance_repo
        self._project_repo = project_repo
        self._session_repo = session_repo
        # Turns a task's `target` (a board name) into a link id. Injected so
        # this service never imports the bridge: a task store must work with
        # no asoode configured at all.
        self._link_resolver = link_resolver
        # The bridge's durable half. Both optional: a task store with no asoode
        # configured must behave exactly as it always has.
        self._outbox = outbox_repo
        self._mirror = mirror
        self._attachments = attachment_repo
        # One lock per project, guarding the read-modify-write in claim_next.
        # DuckDB is single-writer and this repo opens a connection per
        # operation, so the "pick a task, then claim it" pair needs serializing.
        # A plain lock is a COMPLETE fix here and nothing heavier is warranted:
        # there is exactly one daemon (the 127.0.0.1:8765 bind plus launchd
        # KeepAlive make a second impossible) and _api runs handlers in that one
        # process's worker threads. If MEMORY_MCP_MODE=server ever runs several
        # daemons, this lock stops being enough and the claim must move to the
        # SQLite registry, where a cross-process UPDATE ... WHERE is atomic.
        self._claim_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _claim_lock(self, project: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._claim_locks.get(project)
            if lock is None:
                lock = threading.Lock()
                self._claim_locks[project] = lock
            return lock

    # ---------- helpers ----------

    def _actor(self) -> str | None:
        """Who performed this, for the audit trail. Unattributed in local mode,
        exactly like memory provenance."""
        if not settings.server_mode:
            return None
        return current_user().id

    def _record(self, project: str, task_id: str, operation: str, details: dict) -> None:
        """Audit every task mutation, the way MemoryService does for memories.

        The provenance table has no FK and its `memory_id` column is just an
        entity id, so task rows sit alongside memory rows and stay readable
        through the same repository.
        """
        self._provenance_repo.record(
            project, task_id, operation, details, actor=self._actor(),
        )

    def _require(self, project: str, task_id: str) -> Task:
        task = self._task_repo.get(project, task_id)
        if task is None:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        return task

    # ---------- create ----------

    def create(self, request: CreateTaskRequest) -> Task:
        task_id = str(uuid.uuid4())
        position = self._task_repo.next_position(request.project, request.parent_id)

        task = self._task_repo.insert(
            project=request.project,
            task_id=task_id,
            title=request.title.strip(),
            description=request.description,
            state=TaskState.TODO.value,
            priority=request.priority,
            assignee=request.assignee,
            labels=request.labels,
            due_at=request.due_at,
            begin_at=request.begin_at,
            end_at=request.end_at,
            estimated_minutes=request.estimated_minutes,
            parent_id=request.parent_id,
            position=position,
            source=request.source.value,
            link_id=self._resolve_target(request.project, request.target),
            role=request.role,
        )

        self._project_repo.touch(request.project)
        self._record(
            request.project, task_id, "task_create",
            {"title": task.title, "source": task.source, "priority": task.priority},
        )
        self._enqueue(request.project, task_id, "create", {"title": task.title})
        if task.role:
            # So the board says which agent the card is for. Queued, never
            # blocking: creating a task must not wait on the network.
            self._enqueue(request.project, task_id, "role", {"role": task.role})
        return task

    # ---------- read ----------

    def _enqueue(self, project: str, task_id: str, op: str, payload: dict | None = None) -> None:
        """Record that a task changed, and nudge a mirror.

        Both halves are best-effort by design: the local write has already
        happened and is the source of truth. An unreachable asoode, a missing
        credential and an unlinked project are all ordinary outcomes here, never
        a reason to fail the edit that just succeeded.

        Inside a transaction the two halves part company. The outbox row is
        local, so it stays IN the transaction and rolls back with everything
        else. The mirror nudge is not: it spawns a thread that POSTs to asoode,
        and a ROLLBACK cannot un-POST. A plan that fails on task 4 would
        otherwise leave three cards on the board with no local rows behind them
        - the exact shape that produced 54 duplicates once already. So the nudge
        waits for the commit.
        """
        if self._outbox is None or _MIRROR_SUPPRESSED.get():
            return
        try:
            self._outbox.enqueue(project, task_id, op, payload)
        except Exception:  # noqa: BLE001
            return
        if self._mirror is None:
            return

        def _nudge() -> None:
            try:
                self._mirror(project)
            except Exception:  # noqa: BLE001
                pass

        # False means there is no transaction to wait for - mirror now, as ever.
        if not after_commit(_nudge, key=("mirror", project)):
            _nudge()

    @contextlib.contextmanager
    def suppress_mirroring(self):
        """Apply inbound writes without mirroring them back out.

        Scoped to the calling context - the importer's own thread - so a tool
        call landing on another thread meanwhile keeps its mirror. Re-entrant.
        """
        token = _MIRROR_SUPPRESSED.set(_MIRROR_SUPPRESSED.get() + 1)
        try:
            yield
        finally:
            _MIRROR_SUPPRESSED.reset(token)

    def _resolve_target(self, project: str, target: str | None) -> int | None:
        """A task's board, or None for the project's default. Raises if the name
        is wrong - a task silently landing on the wrong board is worse than a
        rejected create."""
        if not target or self._link_resolver is None:
            return None
        return self._link_resolver(project, target)

    def get(self, project: str, task_id: str) -> Task:
        return self._require(project, task_id)

    def detail(self, project: str, task_id: str) -> TaskDetail:
        task = self._require(project, task_id)
        entries = self._task_repo.entries_for(project, task_id)
        return TaskDetail(
            attachments=self.attachments(project, task_id),
            task=task,
            comments=self._task_repo.comments_for(project, task_id),
            time_entries=entries,
            subtasks=self._task_repo.children_of(project, task_id),
            minutes_spent=self._task_repo.seconds_spent(project, task_id) // 60,
            running=any(e.end_at is None for e in entries),
        )

    def activity(self, project: str, task_id: str) -> list:
        """The task's audit trail. Every mutation writes a provenance row, so
        this is a real history rather than a reconstruction."""
        self._require(project, task_id)
        return self._provenance_repo.for_memory(project, task_id)

    def list_tasks(
        self,
        project: str,
        filters: TaskFilter | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> TaskListResponse:
        tasks, total, open_count = self._task_repo.list_tasks(
            project, filters or TaskFilter(), limit, offset,
        )
        counts = self._task_repo.list_meta(project)
        # Every returned task gets an entry, even an all-zero one: a caller
        # reading meta[task.id] should never have to guess whether a missing key
        # means "none" or "not loaded".
        meta = {
            task.id: TaskRowMeta(**counts.get(task.id, {})) for task in tasks
        }
        return TaskListResponse(
            tasks=tasks, total=total, open_count=open_count,
            running_ids=[tid for tid, row in meta.items() if row.running],
            meta=meta,
        )

    def queued(self, project: str) -> list[Task]:
        """Everything still waiting, for session start. Uncapped on purpose: a
        requirement the user parked must never be silently dropped from the
        brief because it fell outside a top-N."""
        return self._task_repo.open_tasks(project)

    def count_open(self, project: str) -> int:
        return self._task_repo.count_open(project)

    # ---------- update ----------

    def update(self, request: UpdateTaskRequest) -> tuple[Task, list[str]]:
        """Apply the given fields. Returns (task, names of fields that changed)."""
        before = self._require(request.project, request.task_id)

        role_changed = False
        fields: dict = {}
        if request.title is not None:
            fields["title"] = request.title.strip()
        if request.description is not None:
            fields["description"] = request.description
        if request.state is not None:
            fields["state"] = request.state.value
        if request.priority is not None:
            fields["priority"] = request.priority
        if request.assignee is not None:
            fields["assignee"] = request.assignee
        if request.labels is not None:
            fields["labels"] = request.labels
        if request.due_at is not None:
            fields["due_at"] = request.due_at
        if request.begin_at is not None:
            fields["begin_at"] = request.begin_at
        if request.end_at is not None:
            fields["end_at"] = request.end_at
        if request.estimated_minutes is not None:
            fields["estimated_minutes"] = request.estimated_minutes
        if request.position is not None:
            fields["position"] = request.position
        if request.role is not None:
            # "" clears it: a task re-opened to any agent has to be expressible,
            # and None already means "leave this field alone".
            fields["role"] = request.role or None
            role_changed = True

        if not fields:
            return before, []

        task = self._task_repo.update(request.project, request.task_id, fields)

        state_changed = request.state is not None and request.state != before.state
        closed: list = []
        # done_at follows the state, and is stamped from the DB clock so every
        # timestamp in the file comes from one source.
        if state_changed:
            if request.state == TaskState.DONE and task.done_at is None:
                task = self._task_repo.mark_done(
                    request.project, request.task_id, TaskState.DONE.value,
                )
            elif request.state != TaskState.DONE and task.done_at is not None:
                task = self._task_repo.update(
                    request.project, request.task_id, {"done_at": None},
                )
            # Leaving in_progress - to done, paused, blocked, anything - is the
            # work stopping, so the clock stops with it. This was the reported
            # bug: a task moved to done through update kept clocking forever,
            # because only `stop` and `done` knew about the clock.
            if before.state == TaskState.IN_PROGRESS:
                closed = self._stop_running(request.project, request.task_id)
            # A finished task is nobody's work any more, whichever verb finished it.
            if request.state == TaskState.DONE:
                self._task_repo.release(request.project, request.task_id, None)

        changed = sorted(fields.keys())
        self._touch_lease(request.project, request.task_id)
        self._record(
            request.project, request.task_id, "task_update",
            {
                "changed": changed,
                "state_from": before.state.value,
                "state_to": task.state.value,
                "clock_stopped": bool(closed),
            },
        )
        # Every field the board can hold. A local-only edit (position, claim,
        # lease) must not queue a mirror that would be a no-op round trip. The
        # payload carries the previous labels and assignee so the flusher can
        # take off what was removed without touching what a human added.
        if _MIRRORED_FIELDS & set(changed):
            payload: dict = {"state": task.state.value, "changed": changed}
            if "labels" in changed:
                payload["labels_before"] = list(before.labels)
            if "assignee" in changed:
                payload["assignee_before"] = before.assignee
            self._enqueue(request.project, request.task_id, "update", payload)
        if closed:
            self._enqueue(request.project, request.task_id, "time", {})
        if role_changed:
            # Its own op: the update op reconciles state, and the role label is a
            # separate remote call that must retry on its own if it fails.
            self._enqueue(request.project, request.task_id, "role", {
                "role": task.role,
            })
        return task, changed

    def reorder(self, project: str, ordered_ids: list[str]) -> int:
        """Apply a manual order to the given tasks, in the order supplied."""
        known = [tid for tid in ordered_ids if self._task_repo.get(project, tid)]
        count = self._task_repo.set_positions(project, known)
        for index, task_id in enumerate(known):
            self._touch_lease(project, task_id)
            self._record(project, task_id, "task_reorder", {"position": index})
        return count

    # ---------- attachments ----------
    #
    # A file the work produced - a screenshot proving a fix, a log, a diff -
    # belongs on the task, and on the remote task too. Bytes are copied into a
    # content-addressed store rather than referenced in place: the source is
    # usually a scratch file that will be gone by the time anyone looks.

    #: Above this a mirror would make the flusher look hung, and no task platform
    #: wants a 100 MB attachment anyway. Refused with the size named.
    MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

    def attach(
        self, project: str, task_id: str, source_path: str,
        filename: str | None = None, content_type: str | None = None,
    ) -> "TaskAttachment":
        """Attach a file that exists on disk to a task."""
        import hashlib
        import mimetypes
        import shutil
        import uuid
        from pathlib import Path as _Path

        from memory_mcp.config import settings
        from memory_mcp.models import TaskAttachment

        self._require(project, task_id)
        src = _Path(source_path).expanduser()
        if not src.is_file():
            raise MemoryMCPError(f"no file at {source_path}")
        size = src.stat().st_size
        if size > self.MAX_ATTACHMENT_BYTES:
            raise MemoryMCPError(
                f"{src.name} is {size / 1_048_576:.1f} MB, over the "
                f"{self.MAX_ATTACHMENT_BYTES // 1_048_576} MB attachment limit"
            )
        if size == 0:
            raise MemoryMCPError(f"{src.name} is empty")

        digest = hashlib.sha256()
        with src.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        sha = digest.hexdigest()

        # Content-addressed, so the same screenshot on two tasks is one blob.
        store = _Path(settings.data_dir) / "attachments" / project / sha[:2]
        store.mkdir(parents=True, exist_ok=True)
        blob = store / sha
        if not blob.exists():
            shutil.copyfile(src, blob)

        name = filename or src.name
        attachment = TaskAttachment(
            id=str(uuid.uuid4()), task_id=task_id, filename=name,
            content_type=content_type or mimetypes.guess_type(name)[0]
            or "application/octet-stream",
            size_bytes=size, sha256=sha,
        )
        if self._attachments is not None:
            self._attachments.add(project, attachment, str(blob))
        self._record(project, task_id, "task_attach",
                     {"filename": name, "size_bytes": size})
        self._enqueue(project, task_id, "attachment", {"attachment_id": attachment.id})
        return attachment

    def attachments(self, project: str, task_id: str) -> list:
        if self._attachments is None:
            return []
        return self._attachments.list_for(project, task_id)

    def detach(self, project: str, attachment_id: str) -> bool:
        """Remove an attachment. The blob goes only when nothing else uses it.

        The remote copy goes too: an attachment removed here and still on the
        board is the same divergence as one never sent.
        """
        if self._attachments is None:
            return False
        found = self._attachments.get(project, attachment_id)
        orphan = self._attachments.delete(project, attachment_id)
        if orphan:
            from pathlib import Path as _Path

            try:
                _Path(orphan).unlink(missing_ok=True)
            except OSError:
                pass
        if found is not None:
            attachment = found[0]
            self._record(project, attachment.task_id, "task_detach",
                         {"filename": attachment.filename})
            self._enqueue(project, attachment.task_id, "detach", {
                "filename": attachment.filename, "attachment_id": attachment.id,
            })
        return True

    def set_link(self, project: str, task_id: str, link_id: int | None) -> Task:
        """Point a task at a linked board. Used by the importer, which knows the
        board a task came from, and by a later re-route."""
        task = self._task_repo.update(project, task_id, {"link_id": link_id})
        self._record(project, task_id, "task_link", {"link_id": link_id})
        return task

    def set_state(self, project: str, task_id: str, state: TaskState) -> Task:
        task, _ = self.update(
            UpdateTaskRequest(project=project, task_id=task_id, state=state)
        )
        return task

    # ---------- claims ----------
    #
    # Several Claude Code sessions run against one project at once. A task must
    # be picked up by exactly one of them, and never by a session that is busy.
    #
    # The rule is PULL, NOT PUSH. The daemon cannot push to a session - sessions
    # only speak when they call a tool - so work is never routed TO a session;
    # a session asks FOR work when it has finished what it was doing. Only the
    # session knows whether it is mid-task, which makes "idle" definitionally
    # correct with no heartbeat protocol: a busy session simply never asks.

    def claim_next(
        self, project: str, session_id: str, ttl_minutes: int = CLAIM_LEASE_MINUTES,
        role: str | None = None,
    ) -> Task | None:
        """Take the next available task for this session, or None if there is none.

        Serialized per project: picking a candidate and claiming it is a
        read-modify-write, and the conditional UPDATE inside `claim` is what
        makes the race safe even if two callers slip past the lock - its
        rowcount decides the winner, so a loser simply moves to the next task.
        """
        self._session_repo.touch(project, session_id)
        with self._claim_lock(project):
            # Bounded rather than `while True`: losing the conditional UPDATE
            # means the row is now held, so the next pass sees a different
            # candidate and this terminates in one or two passes. The cap only
            # rules out a spin if that assumption is ever broken.
            for _ in range(_CLAIM_ATTEMPTS):
                candidate = self._task_repo.next_claimable(project, role)
                if candidate is None:
                    return None
                if self._task_repo.claim(
                    project, candidate.id, session_id, ttl_minutes, role,
                ):
                    self._record(
                        project, candidate.id, "task_claim",
                        {
                            "session_id": session_id,
                            "lease_minutes": ttl_minutes,
                            "role": role,
                        },
                    )
                    return self._task_repo.get(project, candidate.id)
            return None

    def release(self, project: str, task_id: str, session_id: str | None = None) -> Task:
        """Give a claim back so another session can take the task.

        Handing a task back means not working on it, so its clock stops and the
        stretch is mirrored. The state is left alone: an in_progress task with
        no holder and no running clock is exactly what "abandoned mid-way" looks
        like, and the next session picks it up as actionable.
        """
        self._require(project, task_id)
        if session_id:
            self._session_repo.touch(project, session_id)
        released = self._task_repo.release(project, task_id, session_id)
        if released:
            closed = self._stop_running(project, task_id)
            self._record(project, task_id, "task_release",
                         {"session_id": session_id, "clock_stopped": bool(closed)})
            if closed:
                self._enqueue(project, task_id, "time", {})
        return self._require(project, task_id)

    def release_session(self, project: str, session_id: str) -> int:
        """Release everything a session holds. Called when the session ends, so a
        session that stops without releasing does not park its tasks until the
        lease runs out.

        Stops that session's clocks too - the ones on tasks it holds AND the ones
        it started without claiming - so ending a session can never leave a task
        clocking. Each closed stretch is mirrored like any other."""
        return self.end_session(project, session_id)["released"]

    def end_session(self, project: str, session_id: str) -> dict:
        """release_session, reporting both halves: claims released and the task
        ids whose clocks were stopped."""
        held = self._task_repo.claimed_by_session(project, session_id)
        count = self._task_repo.release_session(project, session_id)
        for task in held:
            self._record(
                project, task.id, "task_release",
                {"session_id": session_id, "reason": "session_end"},
            )
        stopped = self.stop_session_clocks(project, session_id, extra=[t.id for t in held])
        return {"released": count, "clocks_stopped": stopped}

    def stop_session_clocks(
        self, project: str, session_id: str, extra: list[str] | None = None,
    ) -> list[str]:
        """Close every clock this session started. Returns the task ids stopped."""
        task_ids = list(dict.fromkeys(
            self._task_repo.running_task_ids_for_session(project, session_id)
            + list(extra or []),
        ))
        stopped = []
        for task_id in task_ids:
            closed = self._stop_running(project, task_id)
            if not closed:
                continue
            stopped.append(task_id)
            self._record(project, task_id, "task_stop",
                         {"session_id": session_id, "reason": "session_end",
                          "entries": [e.id for e in closed]})
            self._enqueue(project, task_id, "time", {})
        return stopped

    def sweep_expired(self, project: str) -> dict:
        """Clean up after sessions that never came back.

        A claim whose lease has run out is already ignored by the next claim,
        but the clock its holder left running was not - it kept counting, and
        was never mirrored because only a closed stretch is sent. Called at
        session start.

        Only leases expired by a full further lease period - an hour with no
        mutation at all, since any comment, update or start refreshes the lease.
        Expiry alone was too eager: an agent forty minutes into a build that
        comments nothing meanwhile would have had its clock closed and its task
        released by an unrelated session starting. An hour of silence is the
        signal that a session is gone, and an agent that works longer than that
        without a comment is violating its own brief.
        """
        released, clocks = [], []
        for task in self._task_repo.expired_claims(project, grace_minutes=CLAIM_LEASE_MINUTES):
            closed = self._stop_running(project, task.id)
            self._task_repo.release(project, task.id, None)
            self._record(project, task.id, "task_lease_expired",
                         {"session_id": task.claimed_by, "clock_stopped": bool(closed)})
            released.append(task.id)
            if closed:
                clocks.append(task.id)
                self._enqueue(project, task.id, "time", {})
        return {"released": released, "clocks_stopped": clocks}

    def _touch_lease(self, project: str, task_id: str) -> None:
        """Push out the lease of a claimed task whenever it is worked on.

        This is the lease refresh: any mutation of a task is proof the holder is
        alive, so no separate heartbeat is needed for the common case of a long
        stretch of work on one task.
        """
        try:
            self._task_repo.extend_lease(project, task_id, CLAIM_LEASE_MINUTES)
        except Exception:  # noqa: BLE001 - a lease refresh must never fail a write
            pass

    # ---------- comments ----------

    def comment(
        self,
        project: str,
        task_id: str,
        body: str,
        kind: str = "note",
        author: str | None = None,
    ) -> TaskComment:
        self._require(project, task_id)
        if not (body or "").strip():
            raise ValueError("Comment body cannot be empty")
        # Validate the kind: a rule or decision pinned to a task must be
        # distinguishable from chatter, which only works if the vocabulary holds.
        kind_value = TaskCommentKind(kind).value

        comment = self._task_repo.add_comment(
            project, str(uuid.uuid4()), task_id, body.strip(), kind_value, author,
        )
        self._touch_lease(project, task_id)
        self._record(project, task_id, "task_comment", {"kind": kind_value})
        self._enqueue(project, task_id, "comment", {"body": body, "kind": kind_value})
        return comment

    # ---------- time tracking ----------

    def start(
        self, project: str, task_id: str, session_id: str | None = None,
    ) -> TaskDetail:
        """Clock on. Moves the task to in_progress, reopening it if it was closed.

        Idempotent: a task that is already running keeps its existing entry
        rather than opening a second, overlapping one.

        With a `session_id`, starting IS claiming: the session that clocks on
        holds the task, so its end hands the task back and stops the clock.
        Never steals - a task held by another session on a live lease stays
        theirs, and this session's clock still runs against it.

        The state change is mirrored like any other. It was not, once: the board
        showed To Do for every task an agent was actively working, until done.
        """
        task = self._require(project, task_id)

        running = self._task_repo.running_entry(project, task_id)
        if running is None:
            self._task_repo.start_entry(project, str(uuid.uuid4()), task_id, session_id)

        if session_id:
            with contextlib.suppress(Exception):
                self._session_repo.touch(project, session_id)
            if task.claimed_by != session_id:
                self._task_repo.claim(project, task_id, session_id, CLAIM_LEASE_MINUTES)

        state_changed = task.state != TaskState.IN_PROGRESS
        if state_changed:
            fields: dict = {"state": TaskState.IN_PROGRESS.value}
            if task.state in _CLOSED_STATES:
                fields["done_at"] = None
            self._task_repo.update(project, task_id, fields)

        self._touch_lease(project, task_id)
        self._record(
            project, task_id, "task_start",
            {"state_from": task.state.value, "resumed": running is not None,
             "session_id": session_id},
        )
        if state_changed:
            self._enqueue(project, task_id, "state", {
                "state": TaskState.IN_PROGRESS.value,
            })
        return self.detail(project, task_id)

    def stop(self, project: str, task_id: str) -> TaskDetail:
        """Clock off. The state is left alone: stopping the timer says nothing
        about whether the work is finished, paused, or blocked - the caller sets
        that explicitly."""
        self._require(project, task_id)
        closed = self._stop_running(project, task_id)
        if closed:
            self._touch_lease(project, task_id)
            self._record(project, task_id, "task_stop",
                         {"entries": [e.id for e in closed]})
            # Inside the branch: stopping a clock that was never running closes
            # nothing, so there is no stretch to send. Only a CLOSED stretch is
            # worth sending - an open one has no duration and would have to be
            # corrected remotely later.
            self._enqueue(project, task_id, "time", {"entry_id": closed[0].id})
        return self.detail(project, task_id)

    def _stop_running(self, project: str, task_id: str) -> list[TaskTimeEntry]:
        """Close EVERY open entry, so a task can never be left clocking.

        All of them, not the newest: two starts racing past each other opened
        two, and closing one left the other counting for good.
        """
        return self._task_repo.stop_all_entries(project, task_id)

    # ---------- close ----------

    def done(self, project: str, task_id: str, note: str | None = None) -> TaskDetail:
        task = self._require(project, task_id)
        closed = self._stop_running(project, task_id)
        self._task_repo.mark_done(project, task_id, TaskState.DONE.value)
        # A finished task is nobody's work any more: drop the claim rather than
        # letting it sit held until the lease runs out.
        self._task_repo.release(project, task_id, None)
        self._record(
            project, task_id, "task_done",
            {"state_from": task.state.value, "note": bool(note),
             "clock_stopped": bool(closed)},
        )
        self._enqueue(project, task_id, "state", {"state": TaskState.DONE.value})
        if note and note.strip():
            # Through comment(), so the note reaches the board like any other
            # comment. Written straight to the table, it sat unmirrored until
            # some later comment on the same task happened to flush it.
            self.comment(project, task_id, note, kind=TaskCommentKind.NOTE.value)
        # done() stops the clock too, so the stretch it just closed needs sending.
        self._enqueue(project, task_id, "time", {})
        return self.detail(project, task_id)

    def convert_to_task(self, project: str, task_id: str) -> Task:
        """Promote a sub-task to a task of its own.

        It leaves its parent's progress bar and joins the top-level list, so it
        gets a fresh append position rather than keeping the one it held among
        its siblings.
        """
        task = self._require(project, task_id)
        if task.parent_id is None:
            return task
        position = self._task_repo.next_position(project, None)
        self._task_repo.set_parent(project, task_id, None)
        promoted = self._task_repo.update(project, task_id, {"position": position})
        self._record(
            project, task_id, "task_convert", {"was_child_of": task.parent_id},
        )
        self._enqueue(project, task_id, "parent", {"parent_id": None})
        return promoted

    def delete(self, project: str, task_id: str) -> dict:
        """Delete a task permanently, with its comments and time entries.

        Archiving is the reversible option and stays the default in the UI; this
        exists for the mistyped task nobody wants to see again. Provenance is
        written BEFORE the delete so the audit trail outlives the row.
        """
        task = self._require(project, task_id)
        self._record(
            project, task_id, "task_delete",
            {"title": task.title, "state": task.state.value},
        )
        # Where the card lives, read BEFORE the delete takes the mapping with
        # it. asoode has no delete route, so the card is archived - it must not
        # stay on the board as a live task nobody has locally.
        remotes = self._outbox.remote_ids_for(project, task_id) if self._outbox else {}
        children = self._task_repo.children_of(project, task_id)
        self._task_repo.hard_delete(project, task_id)
        if remotes:
            # After the delete: hard_delete clears the task's outbox rows.
            self._enqueue(project, task_id, "delete", {
                "remote": {str(link_id): rid for link_id, rid in remotes.items()},
                "title": task.title,
            })
        # hard_delete promoted the children locally; the board must see the same
        # or they stay nested under a card that has just been archived.
        for child in children:
            self._enqueue(project, child.id, "parent", {"parent_id": None})
        return {"status": "ok", "deleted": task_id, "title": task.title}

    def archive(self, project: str, task_id: str) -> Task:
        """Take a task out of the list. Never deleted, matching the memory
        store's soft delete, so an archived requirement can still be found."""
        self._require(project, task_id)
        closed = self._stop_running(project, task_id)
        self._task_repo.release(project, task_id, None)
        task = self._task_repo.archive(project, task_id)
        self._record(project, task_id, "task_archive",
                     {"title": task.title, "clock_stopped": bool(closed)})
        # The stretch just closed goes first: archive is terminal, so nothing
        # later would re-queue the minutes it left behind.
        if closed:
            self._enqueue(project, task_id, "time", {})
        # The board keeps showing it otherwise: archived tasks are hidden from
        # the local list, so the two sides would diverge where nobody looks.
        self._enqueue(project, task_id, "archive", {"archived": True})
        return task
