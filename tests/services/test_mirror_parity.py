"""Every local mutation reaches the board, and every stop stops the clock.

The gaps these close were all found on 2026-09-04, one layer at a time: a task
worked to completion whose card never left To Do (start() enqueued nothing), a
clock that ran for hours after the work ended (only `stop` and `done` knew about
it), a rename that resolved as "flushed" without a single request (the update
branch only sent state), and an import that blanked the shared outbox so every
concurrent write lost its mirror.
"""

import threading

import pytest

from memory_mcp.container import container
from memory_mcp.db.connection import connect
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.models import CreateTaskRequest, TaskFilter, TaskState, UpdateTaskRequest
from memory_mcp.providers import ProviderError, TransientProviderError
from memory_mcp.repositories import OutboxRepository
from memory_mcp.services.task_bridge import TaskBridge
from memory_mcp.services.task_service import TaskService
from tests.providers.fakes import FakeProvider

BOARD_LISTS = {"todo": "l-todo", "in_progress": "l-doing", "done": "l-done"}


def _provider(fail=None):
    p = FakeProvider(fail=fail)
    p.seed(container_id="wp1", title="Board", space_id="p1",
           groups=(("l-todo", "To Do"), ("l-doing", "In Progress"), ("l-done", "Done")))
    return p


@pytest.fixture
def project():
    slug = "parity-test"
    container.project_service.init_project(slug, "Parity Test")
    upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id="wp1", label="board", is_default=True,
        default_list_id="l-todo", state_list_map=BOARD_LISTS,
    )
    return slug


def _stack(slug, provider):
    """A task service that enqueues and a bridge that drains - no threads."""
    outbox = OutboxRepository()
    tasks = TaskService(
        container.task_repo, container.provenance_repo, container.project_repo,
        container.session_repo, outbox_repo=outbox,
        attachment_repo=container.attachment_repo,
    )
    bridge = TaskBridge(
        container.project_service, tasks, provider, outbox_repo=outbox,
        attachment_repo=container.attachment_repo,
    )
    return tasks, bridge, outbox


def _ops(outbox, slug):
    return [r["op"] for r in outbox.pending(slug)]


def _create(tasks, slug, title="Task", **kw):
    return tasks.create(CreateTaskRequest(project=slug, title=title, **kw))


class TestStartMirrors:
    def test_start_moves_the_card_to_in_progress(self, project):
        """The reported bug: every step happened locally, the card sat in To Do."""
        provider = _provider()
        tasks, bridge, outbox = _stack(project, provider)
        task = _create(tasks, project)
        bridge.flush(project)

        tasks.start(project, task.id)
        assert "state" in _ops(outbox, project)
        bridge.flush(project)

        assert ("r1", "in_progress") in provider.states
        assert ("r1", "l-doing") in provider.moves

    def test_start_claims_for_the_session_and_remembers_it_on_the_clock(self, project):
        tasks, _, _ = _stack(project, _provider())
        task = _create(tasks, project)

        detail = tasks.start(project, task.id, session_id="s1")

        assert detail.task.claimed_by == "s1"
        assert detail.time_entries[0].session_id == "s1"
        assert detail.running

    def test_start_never_steals_a_live_claim(self, project):
        tasks, _, _ = _stack(project, _provider())
        task = _create(tasks, project)
        assert tasks.claim_next(project, "other").id == task.id

        detail = tasks.start(project, task.id, session_id="s2")

        assert detail.task.claimed_by == "other", "a live lease belongs to its holder"
        assert detail.running, "the clock still runs for the session doing the work"


class TestFieldsMirror:
    def test_every_field_edit_reaches_the_board(self, project):
        """A rename used to resolve as flushed without a request."""
        provider = _provider()
        tasks, bridge, _ = _stack(project, provider)
        task = _create(tasks, project)
        bridge.flush(project)

        tasks.update(UpdateTaskRequest(
            project=project, task_id=task.id, title="Renamed", description="More",
            priority=3, estimated_minutes=90, due_at="2026-10-01T00:00:00",
        ))
        bridge.flush(project)

        remote_id, fields = provider.field_updates[-1]
        assert remote_id == "r1"
        assert fields["title"] == "Renamed"
        assert fields["description"] == "More"
        assert fields["priority"] == 3
        assert fields["estimated_minutes"] == 90
        assert fields["due_at"] is not None

    def test_labels_are_added_and_removed_never_replaced_wholesale(self, project):
        provider = _provider()
        tasks, bridge, _ = _stack(project, provider)
        task = _create(tasks, project, labels=["a", "b"])
        bridge.flush(project)
        assert provider.label_syncs[-1] == ("r1", "wp1", ["a", "b"], [])

        tasks.update(UpdateTaskRequest(project=project, task_id=task.id, labels=["b", "c"]))
        bridge.flush(project)

        assert provider.label_syncs[-1] == ("r1", "wp1", ["c"], ["a"])

    def test_an_assignee_change_carries_the_previous_one(self, project):
        provider = _provider()
        tasks, bridge, _ = _stack(project, provider)
        task = _create(tasks, project, assignee="ann")
        bridge.flush(project)
        assert provider.assignees[-1] == ("r1", "ann", None)

        tasks.update(UpdateTaskRequest(project=project, task_id=task.id, assignee="bob"))
        bridge.flush(project)

        assert provider.assignees[-1] == ("r1", "bob", "ann")

    def test_a_new_card_carries_every_field_the_task_has(self, project):
        """The create route takes a title and a description; the rest went nowhere."""
        provider = _provider()
        tasks, bridge, _ = _stack(project, provider)
        _create(tasks, project, priority=2, estimated_minutes=30, labels=["x"],
                assignee="ann", role="backend")
        bridge.flush(project)

        assert provider.field_updates[-1][1] == {"priority": 2, "estimated_minutes": 30}
        assert provider.label_syncs[-1] == ("r1", "wp1", ["x"], [])
        assert provider.assignees[-1] == ("r1", "ann", None)
        assert provider.role_labels[-1] == ("r1", "wp1", "backend")

    def test_a_sub_task_nests_under_its_parent_remotely(self, project):
        provider = _provider()
        tasks, bridge, _ = _stack(project, provider)
        parent = _create(tasks, project, "Parent")
        _create(tasks, project, "Child", parent_id=parent.id)
        bridge.flush(project)

        child = next(t for t in provider.created_tasks if t["title"] == "Child")
        assert child["parent_id"] == "r1"

    def test_converting_a_sub_task_promotes_it_remotely(self, project):
        provider = _provider()
        tasks, bridge, _ = _stack(project, provider)
        parent = _create(tasks, project, "Parent")
        child = _create(tasks, project, "Child", parent_id=parent.id)
        bridge.flush(project)

        tasks.convert_to_task(project, child.id)
        bridge.flush(project)

        assert provider.promoted == ["r2"]


class TestTheClock:
    def test_moving_to_done_through_update_stops_the_clock(self, project):
        """The other half of the reported bug: `update(state=done)` stamped
        done_at and left the clock running for good."""
        provider = _provider()
        tasks, bridge, outbox = _stack(project, provider)
        task = _create(tasks, project)
        tasks.start(project, task.id, session_id="s1")

        tasks.update(UpdateTaskRequest(project=project, task_id=task.id, state=TaskState.DONE))

        detail = tasks.detail(project, task.id)
        assert not detail.running
        assert detail.task.claimed_by is None, "a finished task is nobody's work"
        assert "time" in _ops(outbox, project)
        bridge.flush(project)
        assert provider.time_logs and provider.time_logs[-1][2] is not None

    @pytest.mark.parametrize("state", [
        TaskState.PAUSED, TaskState.BLOCKED, TaskState.CANCELLED, TaskState.TODO,
        TaskState.INCOMPLETE, TaskState.BLOCKER, TaskState.DUPLICATE,
    ])
    def test_leaving_in_progress_stops_the_clock(self, project, state):
        tasks, _, outbox = _stack(project, _provider())
        task = _create(tasks, project)
        tasks.start(project, task.id)

        tasks.update(UpdateTaskRequest(project=project, task_id=task.id, state=state))

        assert not tasks.detail(project, task.id).running
        assert "time" in _ops(outbox, project)

    def test_release_stops_the_clock_and_keeps_the_state(self, project):
        tasks, _, outbox = _stack(project, _provider())
        task = _create(tasks, project)
        tasks.start(project, task.id, session_id="s1")

        released = tasks.release(project, task.id, "s1")

        assert released.state == TaskState.IN_PROGRESS
        assert released.claimed_by is None
        assert not tasks.detail(project, task.id).running
        assert "time" in _ops(outbox, project)

    def test_a_session_end_stops_exactly_its_own_clocks(self, project):
        tasks, _, _ = _stack(project, _provider())
        mine = _create(tasks, project, "Mine")
        theirs = _create(tasks, project, "Theirs")
        tasks.start(project, mine.id, session_id="s1")
        tasks.start(project, theirs.id, session_id="s2")

        ended = tasks.end_session(project, "s1")

        assert ended["clocks_stopped"] == [mine.id]
        assert not tasks.detail(project, mine.id).running
        assert tasks.detail(project, theirs.id).running

    def test_an_unclaimed_clock_started_by_the_session_is_still_stopped(self, project):
        """start() records the session on the entry, so even a task the session
        never claimed (someone else held it) is stopped when the session ends."""
        tasks, _, _ = _stack(project, _provider())
        task = _create(tasks, project)
        assert tasks.claim_next(project, "other").id == task.id
        tasks.start(project, task.id, session_id="s1")

        ended = tasks.end_session(project, "s1")

        assert ended["clocks_stopped"] == [task.id]
        assert not tasks.detail(project, task.id).running

    def test_the_sweep_stops_clocks_left_by_a_session_that_never_came_back(self, project):
        tasks, _, outbox = _stack(project, _provider())
        task = _create(tasks, project)
        tasks.start(project, task.id, session_id="dead")
        with connect(project) as conn:
            conn.execute(
                "UPDATE tasks SET lease_expires_at = current_timestamp - INTERVAL 2 HOUR "
                "WHERE id = ?", [task.id],
            )

        swept = tasks.sweep_expired(project)

        assert swept == {"released": [task.id], "clocks_stopped": [task.id]}
        assert not tasks.detail(project, task.id).running
        assert tasks.get(project, task.id).claimed_by is None
        assert "time" in _ops(outbox, project)

    def test_the_sweep_leaves_a_live_lease_alone(self, project):
        tasks, _, _ = _stack(project, _provider())
        task = _create(tasks, project)
        tasks.start(project, task.id, session_id="alive")

        assert tasks.sweep_expired(project) == {"released": [], "clocks_stopped": []}
        assert tasks.detail(project, task.id).running

    def test_the_sweep_gives_a_quiet_holder_a_grace_period(self, project):
        """A lease that expired minutes ago may be an agent forty minutes into a
        build; an hour of silence is what says the session is gone."""
        tasks, _, _ = _stack(project, _provider())
        task = _create(tasks, project)
        tasks.start(project, task.id, session_id="quiet")
        with connect(project) as conn:
            conn.execute(
                "UPDATE tasks SET lease_expires_at = current_timestamp - INTERVAL 5 MINUTE "
                "WHERE id = ?", [task.id],
            )

        assert tasks.sweep_expired(project) == {"released": [], "clocks_stopped": []}
        assert tasks.detail(project, task.id).running

    def test_deleting_a_parent_promotes_its_children_on_the_board(self, project):
        provider = _provider()
        tasks, bridge, outbox = _stack(project, provider)
        parent = _create(tasks, project, "Parent")
        child = _create(tasks, project, "Child", parent_id=parent.id)
        bridge.flush(project)

        tasks.delete(project, parent.id)

        assert "parent" in _ops(outbox, project)
        bridge.flush(project)
        assert provider.promoted == ["r2"]
        assert tasks.get(project, child.id).parent_id is None

    def test_a_card_whose_field_sync_fails_is_not_remembered(self, project):
        """Remembered first, the retry would find the card and never send the
        fields it is missing."""
        provider = _provider()
        calls = {"n": 0}
        real = provider.update_fields

        def flaky(task_id, fields):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ProviderError("rejected once")
            return real(task_id, fields)

        provider.update_fields = flaky
        tasks, bridge, outbox = _stack(project, provider)
        task = _create(tasks, project, priority=3)
        first = bridge.flush(project)
        assert first["failed"] == 1
        link = bridge.route(project, task)
        assert outbox.remote_id(project, task.id, link["id"]) is None

        bridge.flush(project)

        assert outbox.remote_id(project, task.id, link["id"]) == "r1"
        assert provider.field_updates[-1][1] == {"priority": 3}
        assert len(provider.created_tasks) == 1, "the retry looked the card up by ref"

    def test_archive_mirrors_the_stretch_it_closes(self, project):
        provider = _provider()
        tasks, bridge, outbox = _stack(project, provider)
        task = _create(tasks, project)
        bridge.flush(project)
        tasks.start(project, task.id)

        tasks.archive(project, task.id)

        ops = _ops(outbox, project)
        assert ops.index("time") < ops.index("archive"), "the minutes go before the card"
        bridge.flush(project)
        assert provider.time_logs
        assert ("r1", True) in provider.archived

    def test_two_open_entries_both_close(self, project):
        """Two starts racing opened two; closing only the newest left one ticking."""
        tasks, _, _ = _stack(project, _provider())
        task = _create(tasks, project)
        container.task_repo.start_entry(project, "e1", task.id)
        container.task_repo.start_entry(project, "e2", task.id)

        tasks.stop(project, task.id)

        entries = container.task_repo.entries_for(project, task.id)
        assert all(e.end_at is not None for e in entries)


class TestDeleteAndDetach:
    def test_delete_archives_the_card(self, project):
        provider = _provider()
        tasks, bridge, outbox = _stack(project, provider)
        task = _create(tasks, project)
        bridge.flush(project)

        tasks.delete(project, task.id)

        assert _ops(outbox, project) == ["delete"]
        bridge.flush(project)
        assert provider.archived == [("r1", True)]

    def test_a_deleted_task_does_not_come_back_from_the_board(self, project):
        """The card carries memory-mcp:<id>; without a tombstone that read as
        'a task I have never seen' and re-created it."""
        provider = _provider()
        tasks, bridge, _ = _stack(project, provider)
        task = _create(tasks, project, "Gone")
        bridge.flush(project)
        tasks.delete(project, task.id)

        result = bridge.reconcile(project)

        assert result["imported"] == 0
        listing = tasks.list_tasks(project, TaskFilter(include_done=True, include_archived=True))
        assert listing.total == 0

    def test_detach_removes_the_remote_attachment(self, project, tmp_path):
        provider = _provider()
        tasks, bridge, outbox = _stack(project, provider)
        task = _create(tasks, project)
        shot = tmp_path / "shot.png"
        shot.write_bytes(b"\x89PNG fake")
        attachment = tasks.attach(project, task.id, str(shot))
        bridge.flush(project)
        assert provider.attachments_sent

        tasks.detach(project, attachment.id)

        assert "detach" in _ops(outbox, project)
        bridge.flush(project)
        assert provider.attachments_removed == [("r1", "shot.png")]


class TestFlusherRobustness:
    def test_an_op_that_has_to_create_the_card_is_still_sent(self, project):
        """The early return for a freshly created todo card fired before the op
        dispatch, so a comment that created the card was resolved unsent."""
        provider = _provider()
        tasks, bridge, outbox = _stack(project, provider)
        task = _create(tasks, project)
        create_row = outbox.pending(project)[0]
        outbox.resolve(project, create_row["id"])  # lose the create row
        tasks.comment(project, task.id, "still needed")

        bridge.flush(project)

        assert provider.created_tasks, "the comment row created the card"
        assert ("r1", "still needed") in provider.comments

    def test_a_transient_failure_spends_no_attempt(self, project):
        tasks, bridge, outbox = _stack(project, _provider(fail=TransientProviderError("down")))
        _create(tasks, project)

        result = bridge.flush(project)

        assert result["failed"] == 1
        row = outbox.pending(project)[0]
        assert row["attempts"] == 0
        assert "down" in row["last_error"]

    def test_a_rejected_call_does_spend_one(self, project):
        tasks, bridge, outbox = _stack(project, _provider(fail=ProviderError("rejected")))
        _create(tasks, project)

        bridge.flush(project)

        assert outbox.pending(project)[0]["attempts"] == 1

    def test_one_flush_drains_more_than_one_batch(self, project):
        """A row queued during a batch used to wait for the next mutation."""
        tasks, bridge, outbox = _stack(project, _provider())
        for title in ("a", "b", "c"):
            _create(tasks, project, title)

        result = bridge.flush(project, limit=1)

        assert result["flushed"] == 3
        assert outbox.depth(project) == 0

    def test_suppression_is_scoped_to_the_caller_not_the_service(self, project):
        """import_board used to blank the shared service's outbox, losing every
        concurrent write's mirror on every project."""
        tasks, _, outbox = _stack(project, _provider())
        inside = threading.Event()
        proceed = threading.Event()
        depths = {}

        def other_thread():
            inside.wait(5)
            _create(tasks, project, "from another thread")
            depths["other"] = outbox.depth(project)
            proceed.set()

        worker = threading.Thread(target=other_thread)
        worker.start()
        with tasks.suppress_mirroring():
            _create(tasks, project, "inbound")
            depths["suppressed"] = outbox.depth(project)
            inside.set()
            proceed.wait(5)
        worker.join(5)
        _create(tasks, project, "after")

        assert depths["suppressed"] == 0
        assert depths["other"] == 1, "a concurrent write keeps its mirror"
        assert outbox.depth(project) == 2

    def test_comment_kind_and_author_reach_the_board(self, project):
        provider = _provider()
        tasks, bridge, _ = _stack(project, provider)
        task = _create(tasks, project)
        tasks.comment(project, task.id, "ship it", kind="decision", author="reviewer")
        bridge.flush(project)

        body = provider.comments[-1][1]
        # The kind sits on its own line: a markdown body must not have its
        # first block pushed off the start of its line by the prefix.
        assert body.startswith("**[decision]**\n\nship it")
        assert body.endswith("— reviewer")

    def test_the_done_note_reaches_the_board(self, project):
        provider = _provider()
        tasks, bridge, _ = _stack(project, provider)
        task = _create(tasks, project)
        tasks.done(project, task.id, note="all green")
        bridge.flush(project)

        assert ("r1", "all green") in provider.comments

    def test_push_uses_the_same_full_sync_as_the_flusher(self, project):
        provider = _provider()
        tasks, bridge, _ = _stack(project, provider)
        _create(tasks, project, priority=3, role="frontend")

        bridge.push(project)

        assert provider.field_updates[-1][1] == {"priority": 3}
        assert provider.role_labels[-1] == ("r1", "wp1", "frontend")


class TestSessionAndSurfaces:
    def test_session_end_reports_the_clocks_it_stopped(self, project):
        ctx = container.session_service.start(project)
        task = container.task_service.create(CreateTaskRequest(project=project, title="X"))
        container.task_service.start(project, task.id, ctx.session_id)

        result = container.session_service.end(project, ctx.session_id, "bye")

        assert result["clocks_stopped"] == [task.id]
        assert not container.task_service.detail(project, task.id).running

    def test_a_new_session_does_not_touch_a_live_one(self, project):
        ctx1 = container.session_service.start(project)
        task = container.task_service.create(CreateTaskRequest(project=project, title="X"))
        container.task_service.start(project, task.id, ctx1.session_id)

        ctx2 = container.session_service.start(project)

        assert ctx2.expired_claims_released == 0
        assert ctx2.stale_clocks_stopped == 0
        assert container.task_service.detail(project, task.id).running

    def test_ui_create_accepts_role_and_the_planned_window(self, project):
        from memory_mcp.web import routes

        result = routes._task_create({"slug": project}, {
            "title": "UI task", "role": "backend",
            "begin_at": "2026-10-01T09:00:00", "end_at": "2026-10-01T17:00:00",
        }, {})

        assert result["task"]["role"] == "backend"
        assert result["task"]["begin_at"] is not None
        assert result["task"]["end_at"] is not None

    def test_ui_update_can_clear_the_role(self, project):
        from memory_mcp.web import routes

        task = container.task_service.create(
            CreateTaskRequest(project=project, title="X", role="backend"))
        result = routes._task_update({"slug": project, "tid": task.id}, {"role": ""}, {})

        assert result["task"]["role"] is None
        assert "role" in result["changed"]

    def test_a_plan_item_can_carry_a_role(self, project):
        result = container.task_planner.plan(project, "two things", [
            {"title": "API", "description": "the endpoint", "role": "backend"},
            {"title": "Screen", "description": "the page", "role": "frontend"},
        ], mirror=False)

        assert [t["role"] for t in result["tasks"]] == ["backend", "frontend"]
