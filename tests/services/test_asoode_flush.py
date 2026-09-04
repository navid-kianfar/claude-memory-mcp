"""Auto-mirroring: a local mutation queues, a flusher drains, nothing is lost.

The requirement that shapes all of it: if asoode is unreachable the local write
must STILL succeed and the mutation must be retried later. A failed mirror can
never lose or block a local task.
"""

import pytest

from memory_mcp.providers import ProviderError
from memory_mcp.container import container
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.models import CreateTaskRequest, TaskFilter
from memory_mcp.repositories import OutboxRepository
from memory_mcp.services.asoode_bridge import AsoodeBridge
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
    slug = "flush-test"
    container.project_service.init_project(slug, "Flush Test")
    upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id="wp1", label="board", is_default=True,
        default_list_id="l-todo", state_list_map=BOARD_LISTS,
    )
    return slug


def _stack(slug, client):
    """A task service that enqueues, and a bridge that drains - no threads, so a
    test asserts on the flush instead of racing it."""
    outbox = OutboxRepository()
    tasks = TaskService(
        container.task_repo, container.provenance_repo, container.project_repo,
        container.session_repo, outbox_repo=outbox,
    )
    return tasks, AsoodeBridge(
        container.project_service, tasks, client, outbox_repo=outbox
    ), outbox


class TestMutationsQueue:
    def test_creating_a_task_queues_a_mirror(self, project):
        tasks, _, outbox = _stack(project, _provider())
        tasks.create(CreateTaskRequest(project=project, title="New"))
        assert outbox.depth(project) == 1

    def test_completing_a_task_queues_one(self, project):
        tasks, _, outbox = _stack(project, _provider())
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        tasks.done(project, task.id)
        assert outbox.depth(project) >= 2

    def test_a_comment_queues_one(self, project):
        tasks, _, outbox = _stack(project, _provider())
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        tasks.comment(project, task.id, "a note")
        ops = [r["op"] for r in outbox.pending(project)]
        assert "comment" in ops

    def test_a_local_only_edit_queues_nothing(self, project):
        """Reordering has no remote meaning; queuing it would be a wasted call."""
        tasks, _, outbox = _stack(project, _provider())
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        before = outbox.depth(project)
        tasks.reorder(project, [task.id])
        assert outbox.depth(project) == before


class TestFlush:
    def test_drains_and_creates_remotely(self, project):
        client = _provider()
        tasks, bridge, outbox = _stack(project, client)
        tasks.create(CreateTaskRequest(project=project, title="Ship it"))
        result = bridge.flush(project)

        assert result["flushed"] == 1
        assert outbox.depth(project) == 0
        assert client.created_tasks[0]["title"] == "Ship it"

    def test_the_external_ref_is_the_local_id(self, project):
        client = _provider()
        tasks, bridge, _ = _stack(project, client)
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        bridge.flush(project)
        assert client.created_tasks[0]["external_ref"] == f"memory-mcp:{task.id}"

    def test_the_remote_id_is_remembered_so_edits_do_not_re_create(self, project):
        client = _provider()
        tasks, bridge, outbox = _stack(project, client)
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        bridge.flush(project)
        link = bridge.route(project, tasks.get(project, task.id))
        assert outbox.remote_id(project, task.id, link["id"]) == "r1"

        tasks.done(project, task.id)
        bridge.flush(project)
        assert len(client.created_tasks) == 1, "an edit must not create a second remote task"

    def test_completion_mirrors_state_and_moves_the_card(self, project):
        client = _provider()
        tasks, bridge, _ = _stack(project, client)
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        bridge.flush(project)
        tasks.done(project, task.id)
        bridge.flush(project)

        assert ("r1", "done") in client.states
        assert ("r1", "l-done") in client.moves, (
            "asoode keeps state and column independent - a Done card would sit in To Do"
        )

    def test_a_comment_reaches_the_remote_task(self, project):
        client = _provider()
        tasks, bridge, _ = _stack(project, client)
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        tasks.comment(project, task.id, "what I learned")
        bridge.flush(project)
        assert client.comments == [("r1", "what I learned")]


class TestUnreachableAsoodeNeverLosesAWrite:
    def test_the_local_task_survives_a_dead_remote(self, project):
        tasks, bridge, outbox = _stack(project, _provider(fail=ProviderError("down")))
        task = tasks.create(CreateTaskRequest(project=project, title="Offline work"))

        assert tasks.get(project, task.id).title == "Offline work"
        result = bridge.flush(project)
        assert result["failed"] == 1
        assert outbox.depth(project) == 1, "the row stays, to retry"

    def test_the_row_is_retried_when_the_remote_returns(self, project):
        client = _provider(fail=ProviderError("down"))
        tasks, bridge, outbox = _stack(project, client)
        tasks.create(CreateTaskRequest(project=project, title="Deferred"))
        bridge.flush(project)
        assert outbox.depth(project) == 1

        client.fail = None
        assert bridge.flush(project)["flushed"] == 1
        assert outbox.depth(project) == 0
        assert client.created_tasks[0]["title"] == "Deferred"

    def test_the_failure_is_recorded_rather_than_silent(self, project):
        tasks, bridge, outbox = _stack(project, _provider(fail=ProviderError("boom")))
        tasks.create(CreateTaskRequest(project=project, title="X"))
        bridge.flush(project)
        row = outbox.pending(project)[0]
        assert row["attempts"] == 1
        assert "boom" in row["last_error"]

    def test_it_stops_at_the_first_failure_rather_than_hammering(self, project):
        client = _provider(fail=ProviderError("down"))
        tasks, bridge, _ = _stack(project, client)
        for i in range(3):
            tasks.create(CreateTaskRequest(project=project, title=f"T{i}"))
        result = bridge.flush(project)
        assert result["failed"] == 1, "one attempt, not one per queued row"

    def test_an_unlinked_project_drops_rows_instead_of_retrying_forever(self, project):
        from memory_mcp.db.registry import delete_project_link, get_project_links

        tasks, bridge, outbox = _stack(project, _provider())
        tasks.create(CreateTaskRequest(project=project, title="X"))
        for link in get_project_links(project):
            delete_project_link(link["id"])

        result = bridge.flush(project)
        assert result["skipped"] == 1
        assert outbox.depth(project) == 0

    def test_a_task_deleted_before_the_flush_is_not_an_error(self, project):
        tasks, bridge, outbox = _stack(project, _provider())
        task = tasks.create(CreateTaskRequest(project=project, title="Fleeting"))
        container.task_repo.hard_delete(project, task.id)
        result = bridge.flush(project)
        assert result["failed"] == 0
        assert outbox.depth(project) == 0


class TestNoAsoodeConfigured:
    def test_a_task_store_with_no_outbox_behaves_as_before(self, project):
        tasks = TaskService(
            container.task_repo, container.provenance_repo,
            container.project_repo, container.session_repo,
        )
        task = tasks.create(CreateTaskRequest(project=project, title="Plain"))
        assert task.title == "Plain"
        assert tasks.list_tasks(project, TaskFilter(), limit=10).total == 1


class TestTestsNeverReachTheNetwork:
    """A regression guard with teeth.

    Wiring auto-mirror into TaskService made every container-backed test spawn a
    background thread that called the live asoode API. The suite went from ~100s
    to hanging past seven minutes, and a fixture with a real work-package id
    would have written to a real board. settings.asoode_auto_mirror is off in
    conftest; this asserts it stays that way.
    """

    def test_the_mirror_is_disabled_under_test(self):
        from memory_mcp.config import settings

        assert settings.asoode_auto_mirror is False

    def test_creating_a_task_through_the_container_makes_no_call(self, project, monkeypatch):
        def forbidden(*a, **k):
            raise AssertionError("a test must not reach asoode")

        monkeypatch.setattr("httpx.Client.post", forbidden)
        monkeypatch.setattr("httpx.Client.request", forbidden)
        task = container.task_service.create(
            CreateTaskRequest(project=project, title="No network please")
        )
        container.task_service.done(project, task.id)
        assert container.task_service.get(project, task.id).state.value == "done"


class TestAPoisonRowIsEventuallyAbandoned:
    """Retrying forever is not eventual consistency - it is a loop with a side effect.

    The live failure that forced this: a row posted its comment to asoode and
    then failed the LOCAL delete, so every pass re-posted the comment. Bounding
    the attempts trades one lost mirror for not duplicating a side effect
    indefinitely.
    """

    def test_a_row_is_dropped_after_the_attempt_cap(self, project):
        from memory_mcp.repositories.task_repository import MAX_OUTBOX_ATTEMPTS

        tasks, bridge, outbox = _stack(project, _provider(fail=ProviderError("nope")))
        tasks.create(CreateTaskRequest(project=project, title="Doomed"))

        for _ in range(MAX_OUTBOX_ATTEMPTS):
            bridge.flush(project)
        assert outbox.depth(project) == 0, "the poison row must not queue forever"

    def test_giving_up_is_reported_not_silent(self, project):
        from memory_mcp.repositories.task_repository import MAX_OUTBOX_ATTEMPTS

        tasks, bridge, outbox = _stack(project, _provider(fail=ProviderError("nope")))
        tasks.create(CreateTaskRequest(project=project, title="Doomed"))

        results = [bridge.flush(project) for _ in range(MAX_OUTBOX_ATTEMPTS)]
        assert results[-1]["abandoned"] == 1

    def test_the_local_task_outlives_the_abandoned_mirror(self, project):
        from memory_mcp.repositories.task_repository import MAX_OUTBOX_ATTEMPTS

        tasks, bridge, _ = _stack(project, _provider(fail=ProviderError("nope")))
        task = tasks.create(CreateTaskRequest(project=project, title="Survivor"))
        for _ in range(MAX_OUTBOX_ATTEMPTS):
            bridge.flush(project)
        assert tasks.get(project, task.id).title == "Survivor"

    def test_a_row_that_succeeds_before_the_cap_is_not_abandoned(self, project):
        provider = _provider(fail=ProviderError("temporarily down"))
        tasks, bridge, outbox = _stack(project, provider)
        tasks.create(CreateTaskRequest(project=project, title="Recovers"))

        bridge.flush(project)
        provider.fail = None
        result = bridge.flush(project)
        assert result["flushed"] == 1 and result["abandoned"] == 0


class TestConcurrentFlushes:
    def test_two_flushes_do_not_race_the_same_row(self, project):
        """A manual flush used to race the background mirror, and both would
        DELETE the same outbox row - DuckDB failed that with an index error."""
        import threading

        tasks, bridge, outbox = _stack(project, _provider())
        for i in range(5):
            tasks.create(CreateTaskRequest(project=project, title=f"T{i}"))

        errors = []

        def run():
            try:
                bridge.flush(project)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert outbox.depth(project) == 0


class TestTimeTrackingIsMirrored:
    """Time spent was recorded locally and never sent, so every board task read
    0 minutes while the local store held real work."""

    def _timed_task(self, tasks, project, minutes=30):
        from datetime import datetime, timedelta, timezone

        task = tasks.create(CreateTaskRequest(project=project, title="Timed work"))
        tasks.start(project, task.id)
        # Backdate the open entry so the stretch has a real duration.
        from memory_mcp.db.connection import connect

        begin = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        with connect(project) as conn:
            conn.execute(
                "UPDATE task_time_entries SET begin_at = ? WHERE task_id = ?",
                [begin, task.id],
            )
        return task

    def test_a_stopped_clock_reaches_the_provider(self, project):
        tasks, bridge, _ = _stack(project, _provider())
        task = self._timed_task(tasks, project)
        tasks.stop(project, task.id)
        bridge.flush(project)

        assert bridge.provider_for(None).time_logs, "the stretch must be sent"
        logged_task, begin, end = bridge.provider_for(None).time_logs[0]
        assert begin is not None and end is not None

    def test_completing_a_task_sends_its_time(self, project):
        tasks, bridge, _ = _stack(project, _provider())
        task = self._timed_task(tasks, project)
        tasks.done(project, task.id)
        bridge.flush(project)
        assert bridge.provider_for(None).time_logs

    def test_an_open_stretch_is_not_sent(self, project):
        """It has no duration yet; sending it would mean correcting the remote."""
        tasks, bridge, _ = _stack(project, _provider())
        self._timed_task(tasks, project)          # started, never stopped
        bridge.flush(project)
        assert bridge.provider_for(None).time_logs == []

    def test_time_is_never_sent_twice(self, project):
        """A time entry has no externalRef, so a retried flush would double-count.
        Over-reporting hours is worse than a delay."""
        tasks, bridge, _ = _stack(project, _provider())
        task = self._timed_task(tasks, project)
        tasks.stop(project, task.id)

        bridge.flush(project)
        first = len(bridge.provider_for(None).time_logs)
        container.outbox_repo.enqueue(project, task.id, "time", {})
        bridge.flush(project)

        assert len(bridge.provider_for(None).time_logs) == first, "sent once, only once"

    def test_a_provider_without_time_tracking_keeps_the_entries(self, project):
        """Not a loss: the local record stands and can be sent if the platform
        ever gains the capability."""
        from memory_mcp.providers import Capabilities

        provider = _provider()
        provider.__class__.capabilities = property(
            lambda self: Capabilities(supports_external_ref=True, states=STATES_ALL)
        )
        try:
            tasks, bridge, _ = _stack(project, provider)
            task = self._timed_task(tasks, project)
            tasks.stop(project, task.id)
            bridge.flush(project)
            assert provider.time_logs == []
            assert container.outbox_repo.unmirrored_time(project, task.id), (
                "the entry is still there to send later"
            )
        finally:
            del provider.__class__.capabilities


STATES_ALL = (
    "todo", "in_progress", "done", "paused", "blocked",
    "cancelled", "duplicate", "incomplete", "blocker",
)
