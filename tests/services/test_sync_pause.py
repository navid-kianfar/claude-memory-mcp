"""Pausing board sync for a while, then sending everything in one catch-up.

What has to hold: while paused, nothing reaches the board and nothing is read
from it; the queue keeps every row with no attempt spent, however many times
the flusher comes round; local task tools carry on as normal; and resume sends
the whole queue, however long, and says what went and what did not.
"""

from datetime import date, timedelta

import pytest

from memory_mcp.container import container
from memory_mcp.db.registry import get_sync_pause, set_sync_pause, upsert_project_link
from memory_mcp.exceptions import ValidationError
from memory_mcp.models import CreateTaskRequest
from memory_mcp.providers import ProviderError
from memory_mcp.repositories import OutboxRepository
from memory_mcp.services.sync_pause import DRAIN_FINISHED, SyncPauseService
from memory_mcp.services.task_bridge import SyncPausedError, TaskBridge
from memory_mcp.services.task_service import TaskService
from tests.providers.fakes import FakeProvider

BOARD_LISTS = {"todo": "l-todo", "in_progress": "l-doing", "done": "l-done"}
LONG_QUEUE = 130


class NetworkTouched(AssertionError):
    """Raised by a provider that must not be called at all."""


def _provider(fail=None):
    p = FakeProvider(fail=fail)
    p.seed(container_id="wp1", title="Board", space_id="p1",
           groups=(("l-todo", "To Do"), ("l-doing", "In Progress"), ("l-done", "Done")))
    return p


def _link(slug):
    upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id="wp1", label="board", is_default=True,
        default_list_id="l-todo", state_list_map=BOARD_LISTS,
    )


@pytest.fixture
def project():
    slug = "pause-test"
    container.project_service.init_project(slug, "Pause Test")
    _link(slug)
    return slug


def _stack(client):
    """Task service, bridge and pause service over one outbox, no threads."""
    outbox = OutboxRepository()
    tasks = TaskService(
        container.task_repo, container.provenance_repo, container.project_repo,
        container.session_repo, outbox_repo=outbox,
    )
    bridge = TaskBridge(container.project_service, tasks, client, outbox_repo=outbox)
    service = SyncPauseService(bridge, outbox, container.project_service)
    return tasks, bridge, service, outbox


def _make(tasks, slug, count):
    for n in range(count):
        tasks.create(CreateTaskRequest(project=slug, title=f"Local task {n}"))


class TestPausedMeansNoTraffic:
    def test_flush_sends_nothing_and_keeps_every_row(self, project):
        tasks, bridge, service, outbox = _stack(_provider(fail=NetworkTouched("network")))
        service.pause(project)
        _make(tasks, project, 3)

        result = bridge.flush(project)

        assert result["paused"] is True
        assert result["flushed"] == 0 and result["failed"] == 0
        assert result["remaining"] == outbox.depth(project) == 3

    def test_no_attempt_is_spent_however_often_the_flusher_runs(self, project):
        """Two weeks of sweeps is ~20,000 flushes. Past the outbox's attempt
        and transient caps, a row that was ever tried would be dropped."""
        tasks, bridge, service, outbox = _stack(_provider(fail=NetworkTouched("network")))
        service.pause(project)
        _make(tasks, project, 2)

        for _ in range(60):
            bridge.flush(project)

        rows = outbox.pending(project)
        assert len(rows) == 2
        assert all(r["attempts"] == 0 and r["last_error"] is None for r in rows)

    def test_reconcile_and_backfill_do_not_read_the_board(self, project):
        _, bridge, service, _ = _stack(_provider(fail=NetworkTouched("network")))
        service.pause(project)

        assert bridge.reconcile(project)["paused"] is True
        assert bridge.backfill_remote_time(project)["paused"] is True

    def test_explicit_push_and_import_say_why_they_refuse(self, project):
        _, bridge, service, _ = _stack(_provider(fail=NetworkTouched("network")))
        service.pause(project)

        with pytest.raises(SyncPausedError, match="resume"):
            bridge.push(project)
        with pytest.raises(SyncPausedError):
            bridge.import_all(project)

    def test_session_start_does_not_fetch_the_board(self, project):
        tasks, bridge, service, _ = _stack(_provider(fail=NetworkTouched("network")))
        service.pause(project, reason="offline sprint")
        _make(tasks, project, 4)

        status = bridge.queue_status(project)

        assert status["paused"]["reason"] == "offline sprint"
        assert status["paused"]["queued_ops"] == 4

    def test_local_task_tools_behave_as_before(self, project):
        tasks, _, service, _ = _stack(_provider(fail=NetworkTouched("network")))
        service.pause(project)

        task = tasks.create(CreateTaskRequest(project=project, title="Offline"))
        tasks.comment(project, task.id, "noted")
        tasks.done(project, task.id)

        assert tasks.get(project, task.id).state.value == "done"

    def test_the_minute_sweep_leaves_a_paused_project_alone(self, project, monkeypatch):
        container.task_service.create(CreateTaskRequest(project=project, title="Queued"))
        nudged = []
        monkeypatch.setattr(container, "_mirror_soon", nudged.append)
        container.sync_pause_service.pause(project)

        assert container.sweep_outboxes() == []
        assert nudged == []


class TestPausing:
    def test_until_reason_and_since_are_recorded(self, project):
        _, _, service, _ = _stack(_provider())
        until = (date.today() + timedelta(days=14)).isoformat()

        state = service.pause(project, until=until, reason="local only for two weeks")

        assert state["paused"] is True
        assert state["until"] == until
        assert state["reason"] == "local only for two weeks"
        assert state["since"]
        assert state["overdue"] is False

    def test_pausing_again_keeps_the_original_since(self, project):
        _, _, service, _ = _stack(_provider())
        first = service.pause(project)
        later = (date.today() + timedelta(days=3)).isoformat()

        second = service.pause(project, until=later)

        assert second["since"] == first["since"]
        assert second["until"] == later

    def test_a_timestamp_is_reduced_to_its_date(self, project):
        _, _, service, _ = _stack(_provider())
        day = date.today() + timedelta(days=2)
        state = service.pause(project, until=f"{day.isoformat()}T09:30:00")
        assert state["until"] == day.isoformat()

    @pytest.mark.parametrize("until", ["next week", "2026-13-45"])
    def test_a_malformed_until_is_refused(self, project, until):
        _, _, service, _ = _stack(_provider())
        with pytest.raises(ValidationError, match="ISO date"):
            service.pause(project, until=until)
        assert get_sync_pause(project) is None

    def test_a_past_until_is_refused(self, project):
        _, _, service, _ = _stack(_provider())
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        with pytest.raises(ValidationError, match="past"):
            service.pause(project, until=yesterday)

    def test_an_unlinked_project_has_nothing_to_pause(self):
        container.project_service.init_project("pause-unlinked", "Unlinked")
        _, _, service, _ = _stack(_provider())
        with pytest.raises(ValidationError, match="not linked"):
            service.pause("pause-unlinked")

    def test_a_passed_until_is_reported_but_never_resumes_on_its_own(self, project):
        tasks, bridge, service, outbox = _stack(_provider())
        # Written directly: the API refuses a past date, but time moves on.
        set_sync_pause(project, {"since": "2026-01-01T00:00:00+00:00",
                                 "until": "2026-01-15", "reason": None})
        _make(tasks, project, 1)

        bridge.flush(project)

        assert service.status(project)["overdue"] is True
        assert service.status(project)["paused"] is True
        assert outbox.depth(project) == 1


class TestResume:
    def test_a_long_queue_is_drained_in_one_catch_up(self, project):
        client = _provider()
        tasks, _, service, outbox = _stack(client)
        service.pause(project)
        _make(tasks, project, LONG_QUEUE)
        assert outbox.depth(project) == LONG_QUEUE

        state = service.resume(project, wait_seconds=None)

        assert state["was_paused"] is True
        assert state["paused"] is False
        assert state["queued_ops"] == 0
        drain = state["drain"]
        assert drain["state"] == DRAIN_FINISHED
        assert drain["flushed"] == LONG_QUEUE
        assert drain["failed"] == 0 and drain["remaining"] == 0
        assert drain["imported"] == 0, "the board is read once the queue has landed"
        assert len(client.created_tasks) == LONG_QUEUE

    def test_a_drain_past_its_wait_carries_on_in_the_background(self, project):
        client = _provider()
        tasks, _, service, outbox = _stack(client)
        service.pause(project)
        _make(tasks, project, LONG_QUEUE)

        state = service.resume(project, wait_seconds=0)
        assert state["drain"]["state"] in ("running", DRAIN_FINISHED)

        service.wait_for_drains(timeout=30)
        final = service.status(project)
        assert final["drain"]["state"] == DRAIN_FINISHED
        assert final["drain"]["flushed"] == LONG_QUEUE
        assert outbox.depth(project) == 0

    def test_a_failure_stops_the_drain_and_is_reported(self, project):
        tasks, _, service, outbox = _stack(_provider(fail=ProviderError("board is down")))
        service.pause(project)
        _make(tasks, project, 5)

        state = service.resume(project, wait_seconds=None)

        drain = state["drain"]
        assert drain["state"] == DRAIN_FINISHED
        assert drain["failed"] == 1
        assert drain["remaining"] == 5, "the rows stay queued for the next try"
        assert "board is down" in drain["error"]
        assert outbox.depth(project) == 5

    def test_resuming_an_unpaused_project_still_sends_the_queue(self, project):
        client = _provider()
        tasks, _, service, _ = _stack(client)
        _make(tasks, project, 2)

        state = service.resume(project, wait_seconds=None)

        assert state["was_paused"] is False
        assert state["drain"]["flushed"] == 2


class TestTheBriefSaysPaused:
    def test_the_session_brief_names_the_pause_and_works_local(self, project):
        from memory_mcp.services.task_brief import paused_brief

        pause = {"since": "2026-11-20T10:00:00+00:00", "until": "2026-12-04",
                 "reason": "offline sprint"}
        text = paused_brief(project, [object(), object()], pause, 7, overdue=False)

        assert "PAUSED" in text
        assert "since 2026-11-20" in text and "until 2026-12-04" in text
        assert "offline sprint" in text
        assert "7 changes are queued" in text
        assert "Do NOT push" in text
        assert "ask the user" not in text

    def test_an_arrived_until_asks_the_user(self, project):
        from memory_mcp.services.task_brief import paused_brief

        pause = {"since": "2026-11-01T10:00:00+00:00", "until": "2026-11-15", "reason": None}
        text = paused_brief(project, [], pause, 0, overdue=True)

        assert "ask the user" in text
        assert "never resumes on its own" in text

    def test_session_start_hands_out_the_paused_brief(self, project, monkeypatch):
        monkeypatch.setattr(container.task_bridge, "_provider",
                            _provider(fail=NetworkTouched("network")))
        container.sync_pause_service.pause(project, reason="offline")

        status, brief = container.session_service._task_context(project, [])

        assert status["paused"]["reason"] == "offline"
        assert status["paused"]["overdue"] is False
        assert "PAUSED" in brief and "Do NOT push" in brief

    def test_the_per_turn_line_shows_the_pause_and_queue(self, project):
        from memory_mcp.enforcement import asoode_line

        container.task_service.create(CreateTaskRequest(project=project, title="Queued"))
        until = (date.today() + timedelta(days=10)).isoformat()
        container.sync_pause_service.pause(project, until=until, reason="offline")

        line = asoode_line(project)

        assert "PAUSED" in line
        assert f"until {until}" in line
        assert "Reason: offline" in line
        assert "1 change is queued" in line
        assert "Do not push" in line

    def test_the_per_turn_line_is_unchanged_when_not_paused(self, project):
        from memory_mcp.enforcement import asoode_line

        assert "PAUSED" not in asoode_line(project)
        assert "IS this project's work queue" in asoode_line(project)
