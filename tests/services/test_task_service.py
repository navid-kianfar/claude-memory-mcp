"""Unit tests for TaskService: the standalone task store.

The store must work with nothing else attached - no asoode, no org server - and
tasks must stay out of the git-committed snapshot no matter how many there are.
"""

import pytest

from memory_mcp.container import Container
from memory_mcp.db.connection import get_connection
from memory_mcp.exceptions import TaskNotFoundError, ValidationError
from memory_mcp.models import (
    CreateTaskRequest, MemoryCategory, StoreMemoryRequest, TaskCommentKind,
    TaskFilter, TaskSource, TaskState, UpdateTaskRequest,
)


@pytest.fixture
def container():
    return Container()


def _project(container, slug):
    container.project_repo.register(slug, slug)
    get_connection(slug).close()
    return slug


def _add(container, slug, title="Refactor the exporter", **kw):
    return container.task_service.create(
        CreateTaskRequest(project=slug, title=title, **kw)
    )


class TestCreate:
    def test_new_task_starts_queued(self, container):
        slug = _project(container, "t-create")
        task = _add(container, slug, description="the CSV path", priority=2)
        assert task.state == TaskState.TODO
        assert task.source == "user"
        assert task.description == "the CSV path"
        assert task.priority == 2
        assert task.done_at is None and task.archived_at is None

    def test_claude_can_queue_its_own(self, container):
        """Out-of-scope work Claude noticed is queued, not acted on."""
        slug = _project(container, "t-source")
        task = _add(container, slug, title="Tidy the docs", source=TaskSource.CLAUDE)
        assert task.source == "claude"

    def test_positions_append(self, container):
        slug = _project(container, "t-position")
        first = _add(container, slug, title="One")
        second = _add(container, slug, title="Two")
        assert (first.position, second.position) == (0, 1)

    def test_title_is_required(self, container):
        slug = _project(container, "t-title")
        with pytest.raises(ValueError):
            _add(container, slug, title="")

    def test_create_writes_provenance(self, container):
        slug = _project(container, "t-prov")
        task = _add(container, slug)
        ops = [
            p.operation
            for p in container.provenance_repo.for_memory(slug, task.id)
        ]
        assert ops == ["task_create"]


class TestUpdate:
    def test_updates_only_the_fields_passed(self, container):
        slug = _project(container, "t-update")
        task = _add(container, slug, description="keep me")
        updated, changed = container.task_service.update(
            UpdateTaskRequest(project=slug, task_id=task.id, title="New title")
        )
        assert changed == ["title"]
        assert updated.title == "New title"
        assert updated.description == "keep me"

    def test_state_transitions(self, container):
        slug = _project(container, "t-states")
        task = _add(container, slug)
        for state in (
            TaskState.IN_PROGRESS, TaskState.BLOCKED, TaskState.PAUSED,
            TaskState.INCOMPLETE, TaskState.CANCELLED, TaskState.DUPLICATE,
            TaskState.BLOCKER,
        ):
            assert container.task_service.set_state(slug, task.id, state).state == state

    def test_done_state_stamps_done_at_and_reopening_clears_it(self, container):
        slug = _project(container, "t-doneat")
        task = _add(container, slug)
        done = container.task_service.set_state(slug, task.id, TaskState.DONE)
        assert done.done_at is not None
        reopened = container.task_service.set_state(slug, task.id, TaskState.TODO)
        assert reopened.done_at is None

    def test_unknown_task_raises(self, container):
        slug = _project(container, "t-missing")
        with pytest.raises(TaskNotFoundError):
            container.task_service.get(slug, "nope")

    def test_update_writes_provenance(self, container):
        slug = _project(container, "t-prov-update")
        task = _add(container, slug)
        container.task_service.set_state(slug, task.id, TaskState.BLOCKED)
        entry = container.provenance_repo.for_memory(slug, task.id)[-1]
        assert entry.operation == "task_update"
        assert entry.details["state_to"] == "blocked"


class TestListing:
    def test_finished_work_is_hidden_by_default(self, container):
        slug = _project(container, "t-list")
        keep = _add(container, slug, title="Open one")
        closed = _add(container, slug, title="Closed one")
        container.task_service.set_state(slug, closed.id, TaskState.DONE)

        default = container.task_service.list_tasks(slug)
        assert [t.id for t in default.tasks] == [keep.id]
        assert default.total == 1 and default.open_count == 1

        everything = container.task_service.list_tasks(
            slug, TaskFilter(include_done=True)
        )
        assert everything.total == 2 and everything.open_count == 1

    def test_archived_tasks_leave_the_list(self, container):
        slug = _project(container, "t-archive-list")
        task = _add(container, slug)
        container.task_service.archive(slug, task.id)
        assert container.task_service.list_tasks(slug).total == 0
        assert container.task_service.count_open(slug) == 0
        shown = container.task_service.list_tasks(
            slug, TaskFilter(include_archived=True)
        )
        assert [t.id for t in shown.tasks] == [task.id]

    def test_list_reports_which_clocks_are_running(self, container):
        """Not derivable from `state`: stop() leaves a task in_progress."""
        slug = _project(container, "t-list-running")
        ticking = _add(container, slug, title="Ticking")
        idle = _add(container, slug, title="Idle")
        container.task_service.start(slug, ticking.id)
        container.task_service.start(slug, idle.id)
        container.task_service.stop(slug, idle.id)

        result = container.task_service.list_tasks(slug)
        assert result.running_ids == [ticking.id]
        # ...and the stopped one is still in_progress, which is the point.
        stopped = container.task_repo.get(slug, idle.id)
        assert stopped.state == TaskState.IN_PROGRESS

    def test_subtasks_are_listed_under_their_parent_not_beside_it(self, container):
        """A sub-task is part of its parent's work: it shows in the parent's
        detail and progress, not as another row in the queue."""
        slug = _project(container, "t-subtasks")
        parent = _add(container, slug, title="Parent")
        child = container.task_service.create(
            CreateTaskRequest(project=slug, title="Child", parent_id=parent.id)
        )

        top = container.task_service.list_tasks(slug)
        assert [t.id for t in top.tasks] == [parent.id]
        assert top.meta[parent.id].subtasks_total == 1
        assert top.meta[parent.id].subtasks_done == 0

        # ...and the count, the session brief and the claim agree with the list.
        assert container.task_service.count_open(slug) == 1
        assert [t.id for t in container.task_service.queued(slug)] == [parent.id]

        detail = container.task_service.detail(slug, parent.id)
        assert [t.id for t in detail.subtasks] == [child.id]

        explicit = container.task_service.list_tasks(
            slug, TaskFilter(parent_id=parent.id)
        )
        assert [t.id for t in explicit.tasks] == [child.id]

        flat = container.task_service.list_tasks(slug, TaskFilter(include_subtasks=True))
        assert {t.id for t in flat.tasks} == {parent.id, child.id}

    def test_finishing_a_subtask_moves_the_parent_progress(self, container):
        slug = _project(container, "t-subtask-progress")
        parent = _add(container, slug, title="Parent")
        child = container.task_service.create(
            CreateTaskRequest(project=slug, title="Child", parent_id=parent.id)
        )
        container.task_service.done(slug, child.id)
        meta = container.task_service.list_tasks(slug).meta[parent.id]
        assert (meta.subtasks_done, meta.subtasks_total) == (1, 1)

    def test_convert_promotes_a_subtask_to_a_task(self, container):
        slug = _project(container, "t-convert")
        parent = _add(container, slug, title="Parent")
        child = container.task_service.create(
            CreateTaskRequest(project=slug, title="Child", parent_id=parent.id)
        )
        promoted = container.task_service.convert_to_task(slug, child.id)
        assert promoted.parent_id is None

        top = container.task_service.list_tasks(slug)
        assert {t.title for t in top.tasks} == {"Parent", "Child"}
        assert top.meta[parent.id].subtasks_total == 0
        # It is now claimable work in its own right.
        assert container.task_service.count_open(slug) == 2

    def test_convert_on_a_top_level_task_is_a_no_op(self, container):
        slug = _project(container, "t-convert-noop")
        task = _add(container, slug)
        assert container.task_service.convert_to_task(slug, task.id).id == task.id

    def test_delete_removes_the_task_and_everything_hanging_off_it(self, container):
        slug = _project(container, "t-delete")
        task = _add(container, slug)
        container.task_service.comment(slug, task.id, "a note")
        container.task_service.start(slug, task.id)

        result = container.task_service.delete(slug, task.id)
        assert result["deleted"] == task.id
        with pytest.raises(TaskNotFoundError):
            container.task_service.get(slug, task.id)
        assert container.task_repo.comments_for(slug, task.id) == []
        assert container.task_repo.entries_for(slug, task.id) == []
        # The audit trail is written before the row goes, so it outlives it.
        assert "task_delete" in [
            p.operation for p in container.provenance_repo.for_memory(slug, task.id)
        ]

    def test_deleting_a_parent_promotes_its_subtasks(self, container):
        """Losing a parent must never silently take its work down with it."""
        slug = _project(container, "t-delete-parent")
        parent = _add(container, slug, title="Parent")
        child = container.task_service.create(
            CreateTaskRequest(project=slug, title="Child", parent_id=parent.id)
        )
        container.task_service.delete(slug, parent.id)
        survivor = container.task_service.get(slug, child.id)
        assert survivor.parent_id is None
        assert [t.id for t in container.task_service.list_tasks(slug).tasks] == [child.id]

    def test_the_claim_never_offers_a_subtask(self, container):
        slug = _project(container, "t-claim-subtask")
        parent = _add(container, slug, title="Parent")
        container.task_service.create(
            CreateTaskRequest(project=slug, title="Child", parent_id=parent.id)
        )
        claimed = container.task_service.claim_next(slug, "session-a")
        assert claimed is not None and claimed.id == parent.id
        assert container.task_service.claim_next(slug, "session-b") is None

    def test_reorder_sets_a_manual_order_that_the_list_respects(self, container):
        """Dragging is only meaningful if the order it writes is the order that
        comes back - which is why `position` outranks priority in the sort."""
        slug = _project(container, "t-reorder")
        first = _add(container, slug, title="First", priority=3)
        second = _add(container, slug, title="Second")
        third = _add(container, slug, title="Third")
        assert [t.title for t in container.task_service.list_tasks(slug).tasks] == [
            "First", "Second", "Third",
        ]

        moved = container.task_service.reorder(slug, [third.id, first.id, second.id])
        assert moved == 3
        assert [t.title for t in container.task_service.list_tasks(slug).tasks] == [
            "Third", "First", "Second",
        ]

    def test_reorder_ignores_unknown_ids(self, container):
        slug = _project(container, "t-reorder-unknown")
        task = _add(container, slug)
        assert container.task_service.reorder(slug, ["nope", task.id]) == 1

    def test_list_meta_carries_comment_and_time_counts(self, container):
        slug = _project(container, "t-meta")
        task = _add(container, slug)
        container.task_service.comment(slug, task.id, "one")
        container.task_service.comment(slug, task.id, "two", "decision")
        container.task_service.start(slug, task.id)
        meta = container.task_service.list_tasks(slug).meta[task.id]
        assert meta.comments == 2
        assert meta.running is True

    def test_activity_returns_the_audit_trail(self, container):
        slug = _project(container, "t-activity")
        task = _add(container, slug)
        container.task_service.set_state(slug, task.id, TaskState.BLOCKED)
        container.task_service.comment(slug, task.id, "why it is blocked")
        operations = [e.operation for e in container.task_service.activity(slug, task.id)]
        assert operations == ["task_create", "task_update", "task_comment"]

    def test_filter_by_source(self, container):
        slug = _project(container, "t-filter-source")
        _add(container, slug, title="Mine")
        _add(container, slug, title="Claude's", source=TaskSource.CLAUDE)
        result = container.task_service.list_tasks(slug, TaskFilter(source="claude"))
        assert [t.title for t in result.tasks] == ["Claude's"]

    def test_in_progress_sorts_above_todo(self, container):
        """Reading order for a queue: what is underway, then what is next."""
        slug = _project(container, "t-order")
        _add(container, slug, title="Waiting")
        running = _add(container, slug, title="Underway")
        container.task_service.set_state(slug, running.id, TaskState.IN_PROGRESS)
        assert [t.title for t in container.task_service.queued(slug)] == [
            "Underway", "Waiting",
        ]

    def test_queued_is_uncapped(self, container):
        """A parked requirement must never fall outside a top-N and vanish."""
        slug = _project(container, "t-uncapped")
        for i in range(60):
            _add(container, slug, title=f"Task {i}")
        assert len(container.task_service.queued(slug)) == 60


class TestComments:
    def test_comment_kinds(self, container):
        slug = _project(container, "t-comments")
        task = _add(container, slug)
        for kind in TaskCommentKind:
            container.task_service.comment(slug, task.id, f"a {kind.value}", kind.value)
        detail = container.task_service.detail(slug, task.id)
        assert [c.kind for c in detail.comments] == [k.value for k in TaskCommentKind]

    def test_unknown_kind_is_rejected(self, container):
        slug = _project(container, "t-comment-kind")
        task = _add(container, slug)
        with pytest.raises(ValueError):
            container.task_service.comment(slug, task.id, "body", "bogus")

    def test_empty_body_is_rejected(self, container):
        slug = _project(container, "t-comment-empty")
        task = _add(container, slug)
        with pytest.raises(ValueError):
            container.task_service.comment(slug, task.id, "   ")

    def test_comment_on_missing_task_raises(self, container):
        slug = _project(container, "t-comment-missing")
        with pytest.raises(TaskNotFoundError):
            container.task_service.comment(slug, "nope", "body")


class TestTimeTracking:
    def test_start_clocks_on_and_moves_to_in_progress(self, container):
        slug = _project(container, "t-start")
        task = _add(container, slug)
        detail = container.task_service.start(slug, task.id)
        assert detail.task.state == TaskState.IN_PROGRESS
        assert detail.running is True
        assert len(detail.time_entries) == 1

    def test_start_is_idempotent(self, container):
        """A second start must not open an overlapping entry."""
        slug = _project(container, "t-start-twice")
        task = _add(container, slug)
        container.task_service.start(slug, task.id)
        detail = container.task_service.start(slug, task.id)
        assert len(detail.time_entries) == 1

    def test_start_reopens_a_finished_task(self, container):
        slug = _project(container, "t-restart")
        task = _add(container, slug)
        container.task_service.done(slug, task.id)
        detail = container.task_service.start(slug, task.id)
        assert detail.task.state == TaskState.IN_PROGRESS
        assert detail.task.done_at is None

    def test_stop_closes_the_entry_and_leaves_state_alone(self, container):
        slug = _project(container, "t-stop")
        task = _add(container, slug)
        container.task_service.start(slug, task.id)
        detail = container.task_service.stop(slug, task.id)
        assert detail.running is False
        assert detail.time_entries[0].end_at is not None
        # Stopping the clock says nothing about whether the work is finished.
        assert detail.task.state == TaskState.IN_PROGRESS

    def test_stop_without_a_running_clock_is_harmless(self, container):
        slug = _project(container, "t-stop-idle")
        task = _add(container, slug)
        detail = container.task_service.stop(slug, task.id)
        assert detail.running is False and detail.time_entries == []

    def test_done_stops_a_running_clock(self, container):
        slug = _project(container, "t-done-clock")
        task = _add(container, slug)
        container.task_service.start(slug, task.id)
        detail = container.task_service.done(slug, task.id, note="shipped it")
        assert detail.task.state == TaskState.DONE
        assert detail.task.done_at is not None
        assert detail.running is False
        assert [c.body for c in detail.comments] == ["shipped it"]

    def test_archive_stops_a_running_clock(self, container):
        """Archiving with the clock running would leave an entry open forever."""
        slug = _project(container, "t-archive-clock")
        task = _add(container, slug)
        container.task_service.start(slug, task.id)
        container.task_service.archive(slug, task.id)
        assert container.task_repo.running_entry(slug, task.id) is None


class TestTasksStayOutOfTheSnapshot:
    """The reason tasks are separate tables and not a MemoryCategory."""

    def test_tasks_are_not_in_all_for_categories(self, container):
        from memory_mcp.constants import SYNC_CATEGORIES

        slug = _project(container, "t-snapshot")
        _add(container, slug, title="A queued requirement")
        container.memory_service.store(
            StoreMemoryRequest(
                project=slug, category=MemoryCategory.DECISION,
                title="A decision", content="content",
            )
        )
        memories = container.memory_repo.all_for_categories(slug, SYNC_CATEGORIES)
        assert [m.title for m in memories] == ["A decision"]

    def test_tasks_are_not_in_the_sync_snapshot(self, container):
        slug = _project(container, "t-snapshot-build")
        _add(container, slug, title="A queued requirement")
        snapshot = container.sync_service.build_snapshot(slug)
        serialized = str(snapshot)
        assert "A queued requirement" not in serialized
        assert "task" not in snapshot

    def test_task_is_not_a_memory_category(self, container):
        """SYNC_CATEGORIES is derived from the enum, so a `task` category would
        immediately start writing tasks into the committed snapshot."""
        from memory_mcp.constants import SYNC_CATEGORIES

        assert "task" not in SYNC_CATEGORIES
        assert "tasks" not in SYNC_CATEGORIES
        assert not any(c.value.startswith("task") for c in MemoryCategory)


class TestTheClaim:
    """Several Claude sessions, one project: a task goes to exactly one of them."""

    def test_claim_next_takes_the_first_waiting_task(self, container):
        slug = _project(container, "t-claim")
        _add(container, slug, title="First")
        _add(container, slug, title="Second")
        claimed = container.task_service.claim_next(slug, "session-a")
        assert claimed is not None
        assert claimed.title == "First"
        assert claimed.claimed_by == "session-a"
        assert claimed.claimed_at is not None
        assert claimed.lease_expires_at is not None

    def test_a_claimed_task_is_not_offered_again(self, container):
        slug = _project(container, "t-claim-once")
        _add(container, slug, title="Only one")
        assert container.task_service.claim_next(slug, "session-a") is not None
        assert container.task_service.claim_next(slug, "session-b") is None

    def test_second_session_gets_the_next_task(self, container):
        slug = _project(container, "t-claim-two")
        _add(container, slug, title="First")
        _add(container, slug, title="Second")
        a = container.task_service.claim_next(slug, "session-a")
        b = container.task_service.claim_next(slug, "session-b")
        assert {a.title, b.title} == {"First", "Second"}
        assert a.id != b.id

    def test_two_concurrent_claims_on_one_task_exactly_one_wins(self, container):
        """The race the whole design exists for: several sessions ask at once and
        only one may come away holding the task."""
        import threading

        slug = _project(container, "t-claim-race")
        _add(container, slug, title="Contested")

        barrier = threading.Barrier(8)
        results: list = []
        lock = threading.Lock()

        def grab(session_id: str) -> None:
            barrier.wait()
            task = container.task_service.claim_next(slug, session_id)
            with lock:
                results.append((session_id, task))

        threads = [
            threading.Thread(target=grab, args=(f"session-{i}",)) for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [(sid, t) for sid, t in results if t is not None]
        assert len(winners) == 1, f"expected one winner, got {len(winners)}"
        winner_session = winners[0][0]
        stored = container.task_repo.get(slug, winners[0][1].id)
        assert stored.claimed_by == winner_session

    def test_raw_claim_is_decided_by_rowcount(self, container):
        """The repository's conditional UPDATE is the actual arbiter: 1 means you
        got it, 0 means someone else did."""
        slug = _project(container, "t-claim-rowcount")
        task = _add(container, slug)
        assert container.task_repo.claim(slug, task.id, "session-a", 30) is True
        assert container.task_repo.claim(slug, task.id, "session-b", 30) is False

    def test_an_expired_lease_is_reclaimable(self, container):
        """A session that dies holds nothing forever - the lease is checked
        lazily on the next claim, so no sweeper thread is needed."""
        slug = _project(container, "t-claim-expired")
        task = _add(container, slug)
        # A lease that has already run out, as a crashed session would leave.
        assert container.task_repo.claim(slug, task.id, "dead-session", -1) is True
        reclaimed = container.task_service.claim_next(slug, "live-session")
        assert reclaimed is not None and reclaimed.id == task.id
        assert reclaimed.claimed_by == "live-session"

    def test_working_on_a_task_extends_its_lease(self, container):
        slug = _project(container, "t-claim-lease")
        task = _add(container, slug)
        claimed = container.task_service.claim_next(slug, "session-a")
        before = claimed.lease_expires_at
        container.task_service.comment(slug, task.id, "still going")
        after = container.task_repo.get(slug, task.id).lease_expires_at
        assert after >= before

    def test_release_frees_the_task(self, container):
        slug = _project(container, "t-release")
        task = _add(container, slug)
        container.task_service.claim_next(slug, "session-a")
        freed = container.task_service.release(slug, task.id, "session-a")
        assert freed.claimed_by is None and freed.lease_expires_at is None
        assert container.task_service.claim_next(slug, "session-b") is not None

    def test_a_session_cannot_release_another_sessions_claim(self, container):
        slug = _project(container, "t-release-other")
        task = _add(container, slug)
        container.task_service.claim_next(slug, "session-a")
        still_held = container.task_service.release(slug, task.id, "session-b")
        assert still_held.claimed_by == "session-a"

    def test_operator_release_without_a_session_id_always_frees(self, container):
        """The UI's escape hatch for a task left held by a session that is gone."""
        slug = _project(container, "t-release-force")
        task = _add(container, slug)
        container.task_service.claim_next(slug, "session-a")
        assert container.task_service.release(slug, task.id).claimed_by is None

    def test_finishing_a_task_drops_the_claim(self, container):
        slug = _project(container, "t-claim-done")
        task = _add(container, slug)
        container.task_service.claim_next(slug, "session-a")
        assert container.task_service.done(slug, task.id).task.claimed_by is None

    def test_session_end_releases_what_that_session_held(self, container):
        slug = _project(container, "t-claim-session-end")
        _add(container, slug, title="One")
        _add(container, slug, title="Two")
        ctx = container.session_service.start(slug)
        first = container.task_service.claim_next(slug, ctx.session_id)
        second = container.task_service.claim_next(slug, ctx.session_id)
        assert container.task_service.claim_next(slug, "someone-else") is None

        result = container.session_service.end(slug, ctx.session_id, "done for now")
        assert result["tasks_released"] == 2
        for task in (first, second):
            assert container.task_repo.get(slug, task.id).claimed_by is None

    def test_session_end_leaves_another_sessions_claims_alone(self, container):
        slug = _project(container, "t-claim-session-scope")
        _add(container, slug, title="One")
        _add(container, slug, title="Two")
        mine = container.task_service.claim_next(slug, "session-a")
        theirs = container.task_service.claim_next(slug, "session-b")
        container.session_service.end(slug, "session-a", "finished")
        assert container.task_repo.get(slug, mine.id).claimed_by is None
        assert container.task_repo.get(slug, theirs.id).claimed_by == "session-b"

    def test_claiming_stamps_the_session_heartbeat(self, container):
        slug = _project(container, "t-claim-heartbeat")
        _add(container, slug)
        ctx = container.session_service.start(slug)
        from memory_mcp.db.connection import connect

        with connect(slug) as conn:
            before = conn.execute(
                "SELECT last_seen_at FROM sessions WHERE id = ?", [ctx.session_id]
            ).fetchone()[0]
        assert before is not None
        container.task_service.claim_next(slug, ctx.session_id)
        with connect(slug) as conn:
            after = conn.execute(
                "SELECT last_seen_at FROM sessions WHERE id = ?", [ctx.session_id]
            ).fetchone()[0]
        assert after >= before

    def test_archived_and_finished_tasks_are_never_offered(self, container):
        slug = _project(container, "t-claim-closed")
        done = _add(container, slug, title="Finished")
        container.task_service.done(slug, done.id)
        archived = _add(container, slug, title="Archived")
        container.task_service.archive(slug, archived.id)
        assert container.task_service.claim_next(slug, "session-a") is None


class TestSessionIntegration:
    def test_session_start_surfaces_queued_tasks_with_a_brief(self, container):
        slug = _project(container, "t-session")
        _add(container, slug, title="Queued for later")
        ctx = container.session_service.start(slug)
        assert [t.title for t in ctx.queued_tasks] == ["Queued for later"]
        assert ctx.task_instructions is not None
        # The brief has one job: stop a queued task reading as an instruction.
        assert "NOT instructions" in ctx.task_instructions
        assert "Do NOT start any of them" in ctx.task_instructions
        # And claiming must not read as an invitation to grab work on sight.
        assert "FINISHED what you were doing" in ctx.task_instructions

    def test_no_tasks_means_no_brief(self, container):
        slug = _project(container, "t-session-empty")
        ctx = container.session_service.start(slug)
        assert ctx.queued_tasks == []
        assert ctx.task_instructions is None

    def test_finished_tasks_drop_out_of_the_session_brief(self, container):
        slug = _project(container, "t-session-done")
        task = _add(container, slug)
        container.task_service.done(slug, task.id)
        ctx = container.session_service.start(slug)
        assert ctx.queued_tasks == []

    def test_hook_intro_carries_the_count(self, container):
        from memory_mcp.enforcement import format_intro

        slug = _project(container, "t-intro")
        _add(container, slug)
        text = format_intro(slug)
        assert "1 task is waiting" in text
        assert "NOT instructions" in text


class TestSubTaskPositionQuery:
    """Guards the DuckDB crash that made sub-task creation impossible.

    On 2026-09-04 `memory_task_add(parent_id=...)` and every `memory_task_plan`
    carrying a `parent_index` failed with a DuckDB INTERNAL assertion,
    "Attempted to access index 0 within vector of size 0", raised by
    `next_position`'s `SELECT COALESCE(MAX(position), -1) ... WHERE parent_id = ?`
    when the parent had no children yet.

    THESE TESTS CANNOT REPRODUCE THE CRASH, and pretending otherwise would be
    worse than not having them. It survived a copy of the live database but not a
    `CREATE TABLE t2 AS SELECT * FROM tasks` of the very same 31 rows, so the
    trigger is the persisted table's storage metadata, not the data, the schema
    or the SQL text - and a table built fresh by a fixture never has it. That is
    exactly why 627 passing tests, sub-task coverage included, missed it.

    So the behavioural test below documents the intent, and the source guard is
    the one that actually bites: it fails if anyone reintroduces the aggregate.
    """

    def test_first_sub_task_of_a_parent_gets_position_zero(self, container):
        slug = _project(container, "t-subtask-position")
        parent = _add(container, slug, title="Parent")
        first = container.task_service.create(
            CreateTaskRequest(project=slug, title="First child", parent_id=parent.id)
        )
        second = container.task_service.create(
            CreateTaskRequest(project=slug, title="Second child", parent_id=parent.id)
        )
        # Sub-task ordering is its own sequence, not a continuation of the root one.
        assert (first.position, second.position) == (0, 1)

    def test_next_position_does_not_aggregate_over_a_filtered_scan(self):
        """The shape that crashed. Fold the rows in Python instead."""
        import ast
        import inspect
        import textwrap

        from memory_mcp.repositories.task_repository import TaskRepository

        # Inspect the SQL literals only. Not the whole source: the docstring
        # quotes the banned SQL to explain it, and the fix itself calls Python's
        # max() - either would make a naive substring check self-triggering.
        func = ast.parse(
            textwrap.dedent(inspect.getsource(TaskRepository.next_position))
        ).body[0]
        if isinstance(func.body[0], ast.Expr) and isinstance(
            func.body[0].value, ast.Constant
        ):
            func.body.pop(0)
        sql = " ".join(
            node.value.upper()
            for node in ast.walk(func)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        assert "SELECT" in sql, "expected next_position to still issue SQL"
        assert "MAX(" not in sql, (
            "next_position must not run an ungrouped MAX over a filtered scan - "
            "it raises a DuckDB INTERNAL assertion when the parent has no "
            "children yet, which is every parent's first sub-task."
        )


class TestSubTasksGoOnlyOneLevelDeep:
    """"A task with a parentId can not have sub tasks" - the user, 2026-09-05.

    It is not only a product preference: asoode nests one level too, so a
    grandchild is a shape the board cannot hold. It would arrive there as a
    sibling of its own parent, and the hierarchy would be a lie on one side.
    """

    def test_a_sub_task_cannot_take_a_sub_task(self, container):
        slug = _project(container, "t-depth")
        parent = _add(container, slug, title="Parent")
        child = container.task_service.create(
            CreateTaskRequest(project=slug, title="Child", parent_id=parent.id)
        )
        with pytest.raises(ValidationError) as caught:
            container.task_service.create(
                CreateTaskRequest(project=slug, title="Grandchild", parent_id=child.id)
            )
        message = str(caught.value)
        # The error names the offender, so the caller knows which id to fix.
        assert "Child" in message and child.id in message and parent.id in message

    def test_the_first_level_still_works(self, container):
        slug = _project(container, "t-depth-ok")
        parent = _add(container, slug, title="Parent")
        child = container.task_service.create(
            CreateTaskRequest(project=slug, title="Child", parent_id=parent.id)
        )
        assert child.parent_id == parent.id

    def test_an_unknown_parent_is_refused(self, container):
        """It used to succeed, quietly making a child of nothing."""
        slug = _project(container, "t-depth-missing")
        with pytest.raises(TaskNotFoundError):
            container.task_service.create(
                CreateTaskRequest(project=slug, title="Orphan", parent_id="no-such-id")
            )

    def test_a_promoted_sub_task_can_take_sub_tasks_again(self, container):
        """convert is the escape hatch the error message points at."""
        slug = _project(container, "t-depth-convert")
        parent = _add(container, slug, title="Parent")
        child = container.task_service.create(
            CreateTaskRequest(project=slug, title="Child", parent_id=parent.id)
        )
        container.task_service.convert_to_task(slug, child.id)
        grandchild = container.task_service.create(
            CreateTaskRequest(project=slug, title="Now legal", parent_id=child.id)
        )
        assert grandchild.parent_id == child.id

    def test_deleting_a_parent_promotes_its_children(self, container):
        """The other legal path out, and it must keep working."""
        slug = _project(container, "t-depth-delete")
        parent = _add(container, slug, title="Parent")
        child = container.task_service.create(
            CreateTaskRequest(project=slug, title="Child", parent_id=parent.id)
        )
        container.task_service.delete(slug, parent.id)
        assert container.task_service.get(slug, child.id).parent_id is None


class TestRoleAwareClaiming:
    """Five agents share one queue, so a claim must be able to say "work for me".

    The fallback is the load-bearing part: a task with NO role stays claimable by
    anyone. Every task that existed before the role column has none, and a claim
    that demanded one would empty the queue for every caller at once.
    """

    def test_a_role_task_is_not_offered_to_another_role(self, container):
        slug = _project(container, "t-role-mismatch")
        _add(container, slug, title="Migration", role="backend")

        assert container.task_service.claim_next(slug, "s1", role="frontend") is None

    def test_a_role_task_is_offered_to_its_own_role(self, container):
        slug = _project(container, "t-role-match")
        task = _add(container, slug, title="Migration", role="backend")

        claimed = container.task_service.claim_next(slug, "s1", role="backend")

        assert claimed is not None and claimed.id == task.id

    def test_an_unroled_task_is_claimable_by_any_role(self, container):
        slug = _project(container, "t-role-open")
        task = _add(container, slug, title="Anyone can do this")

        claimed = container.task_service.claim_next(slug, "s1", role="e2e")

        assert claimed is not None and claimed.id == task.id

    def test_a_caller_with_no_role_is_offered_anything(self, container):
        """Unchanged behaviour for every caller that predates the agent team."""
        slug = _project(container, "t-role-none")
        task = _add(container, slug, title="Migration", role="backend")

        claimed = container.task_service.claim_next(slug, "s1")

        assert claimed is not None and claimed.id == task.id

    def test_a_role_skips_past_another_role_to_its_own_work(self, container):
        slug = _project(container, "t-role-skip")
        _add(container, slug, title="Backend work", role="backend", priority=3)
        mine = _add(container, slug, title="Frontend work", role="frontend", priority=1)

        claimed = container.task_service.claim_next(slug, "s1", role="frontend")

        assert claimed is not None and claimed.id == mine.id

    def test_the_role_is_enforced_by_the_claiming_update_itself(self, container):
        """Not only by the candidate search.

        If the role were filtered when picking a candidate but not when taking
        it, a task whose role changed in between would still be claimed - the
        race this design closes by making the conditional UPDATE the decision.
        """
        slug = _project(container, "t-role-update-guard")
        task = _add(container, slug, title="Backend work", role="backend")

        assert not container.task_repo.claim(slug, task.id, "s1", 30, "frontend")
        assert container.task_repo.claim(slug, task.id, "s1", 30, "backend")

    def test_role_survives_a_round_trip_and_can_be_cleared(self, container):
        slug = _project(container, "t-role-update")
        task = _add(container, slug, title="Work", role="backend")
        assert container.task_service.get(slug, task.id).role == "backend"

        updated, _ = container.task_service.update(
            UpdateTaskRequest(project=slug, task_id=task.id, role="")
        )

        assert updated.role is None
