"""Time logged on the board comes back into the local store.

The defect: ~800 cards imported from asoode as already done showed zero local
time, because import read titles and states and never a card's `timeSpents`.
The two ways this can go wrong are both worse than the gap: counting a stretch
twice (re-import, or a stretch we sent out coming back in), and sending an
imported stretch back out, which doubles it on the board.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from memory_mcp.container import container
from memory_mcp.db.registry import get_default_project_link, upsert_project_link
from memory_mcp.models import CreateTaskRequest, TaskFilter
from memory_mcp.providers import ProviderError, RemoteTimeEntry
from memory_mcp.repositories import OutboxRepository
from memory_mcp.services import task_bridge as bridge_module
from memory_mcp.services.task_bridge import TaskBridge
from memory_mcp.services.task_service import TaskService
from tests.providers.fakes import FakeProvider

# The live card read on 2026-09-15: 15:13:32.070Z to 15:39:41.527Z, 26 minutes.
LIVE_BEGIN = datetime(2026, 9, 15, 15, 13, 32, 70000, tzinfo=timezone.utc)
LIVE_END = datetime(2026, 9, 15, 15, 39, 41, 527000, tzinfo=timezone.utc)


def _stretch(entry_id, begin=LIVE_BEGIN, end=LIVE_END, manual=True):
    return RemoteTimeEntry(id=entry_id, begin=begin, end=end, manual=manual)


def _provider(time_on_done=(), time_on_open=()):
    provider = FakeProvider()
    provider.seed(
        container_id="wp1", title="Board", space_id="p1",
        groups=(("l-todo", "To Do"), ("l-done", "Done")),
        tasks=[
            {"id": "r1", "title": "Open card", "state": "todo", "group_id": "l-todo",
             "time": list(time_on_open)},
            {"id": "r2", "title": "Finished card", "state": "done", "group_id": "l-done",
             "time": list(time_on_done)},
        ],
    )
    return provider


@pytest.fixture
def project():
    slug = "time-import-test"
    container.project_service.init_project(slug, "Time Import Test")
    upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id="wp1", label="board", is_default=True,
        default_list_id="l-todo", state_list_map={"todo": "l-todo", "done": "l-done"},
    )
    return slug


def _stack(provider):
    """A task service that queues mirrors and a bridge that drains them, with no
    threads - so a test can prove an import queues nothing."""
    outbox = OutboxRepository()
    tasks = TaskService(
        container.task_repo, container.provenance_repo, container.project_repo,
        container.session_repo, outbox_repo=outbox,
    )
    bridge = TaskBridge(container.project_service, tasks, provider, outbox_repo=outbox)
    return tasks, bridge, outbox


def _task_titled(project, title):
    return next(
        t for t in container.task_service.list_tasks(
            project, TaskFilter(include_done=True), limit=20).tasks
        if t.title == title
    )


def _closed_entries(project, task_id):
    return [e for e in container.task_repo.entries_for(project, task_id) if e.end_at]


def _unreadable(task_id):
    raise ProviderError(f"card {task_id} is unreadable")


def _as_local_clock(instant):
    return instant.astimezone().replace(tzinfo=None)


class TestAnImportedCardBringsItsTime:
    def test_a_done_cards_stretch_appears_on_the_local_task(self, project):
        _, bridge, _ = _stack(_provider(time_on_done=[_stretch("ts-live")]))

        result = bridge.import_all(project)

        task = _task_titled(project, "Finished card")
        (entry,) = _closed_entries(project, task.id)
        assert result["boards"][0]["time"]["entries"] == 1
        assert entry.begin_at == _as_local_clock(LIVE_BEGIN), "stored in the local clock"
        assert entry.end_at - entry.begin_at == LIVE_END - LIVE_BEGIN
        assert container.task_repo.seconds_spent(project, task.id) == 1569

    def test_the_platforms_manual_flag_comes_with_it(self, project):
        _, bridge, _ = _stack(_provider(time_on_done=[_stretch("ts-timer", manual=False)]))

        bridge.import_all(project)

        (entry,) = _closed_entries(project, _task_titled(project, "Finished card").id)
        assert entry.manual is False

    def test_reconcile_brings_time_to_a_task_that_already_exists(self, project):
        """Reconcile never overwrites a task, but time is appended, not overwritten
        - so a card imported before this change gets its time on the next read."""
        provider = _provider()
        _, bridge, _ = _stack(provider)
        bridge.reconcile(project)
        provider._tasks["r1"]["time"].append(_stretch("ts-later"))

        result = bridge.reconcile(project)

        assert result["time_imported"] == 1
        assert len(_closed_entries(project, _task_titled(project, "Open card").id)) == 1

    def test_a_card_with_no_time_is_not_read(self, project):
        provider = _provider()
        _, bridge, _ = _stack(provider)

        bridge.import_all(project)

        assert provider.time_reads == []


class TestNothingIsCountedTwice:
    def test_a_re_import_adds_nothing_and_reads_no_card(self, project):
        provider = _provider(time_on_done=[_stretch("ts-1"), _stretch(
            "ts-2", begin=LIVE_END, end=LIVE_END + timedelta(minutes=10))])
        _, bridge, _ = _stack(provider)
        bridge.import_all(project)
        reads_after_first = len(provider.time_reads)

        second = bridge.import_all(project)

        task = _task_titled(project, "Finished card")
        assert len(_closed_entries(project, task.id)) == 2
        assert second["boards"][0]["time"]["entries"] == 0
        assert len(provider.time_reads) == reads_after_first, (
            "a card whose time is already here costs no network call"
        )

    def test_a_stretch_we_mirrored_out_is_not_imported_back(self, project):
        provider = _provider()
        tasks, bridge, outbox = _stack(provider)
        task = tasks.create(CreateTaskRequest(project=project, title="Worked here"))
        bridge.flush(project)
        # Microseconds, as the local clock records them; the board keeps milliseconds.
        begin = datetime(2026, 9, 15, 18, 13, 32, 70311)
        container.task_repo.add_manual_entry(
            project, str(uuid.uuid4()), task.id, begin, begin + timedelta(minutes=26, seconds=9),
        )
        bridge.queue_unsent_time(project)
        bridge.flush(project)
        assert len(provider.time_logs) == 1
        # Someone else logs time on the same card, so the card has to be read -
        # and our own stretch comes back with it, in UTC, to the millisecond.
        (remote_card,) = [log[0] for log in provider.time_logs]
        provider._tasks[remote_card]["time"].append(_stretch("ts-colleague"))

        result = bridge.reconcile(project)

        assert result["time_imported"] == 1, "only the colleague's stretch is new"
        assert len(_closed_entries(project, task.id)) == 2

    def test_a_stretch_sent_but_never_marked_is_matched_and_not_sent_again(self, project):
        """A flush that sent a stretch and died before marking it would send it
        again. Once the board is seen holding it, it counts as sent."""
        provider = _provider()
        tasks, bridge, outbox = _stack(provider)
        task = tasks.create(CreateTaskRequest(project=project, title="Sent, not marked"))
        bridge.flush(project)
        begin = datetime(2026, 9, 15, 10, 0, 0, 123456)
        end = begin + timedelta(minutes=40)
        container.task_repo.add_manual_entry(project, str(uuid.uuid4()), task.id, begin, end)
        remote_id = container.outbox_repo.remote_id(
            project, task.id, get_default_project_link(project)["id"])
        provider.log_time(remote_id, begin, end)

        bridge.reconcile(project)

        assert len(_closed_entries(project, task.id)) == 1
        assert outbox.unmirrored_time(project, task.id) == []
        assert bridge.queue_unsent_time(project) == ()

    def test_two_identical_stretches_on_the_board_stay_two(self, project):
        """Matching is one to one: two people can log the same hour."""
        provider = _provider(time_on_done=[_stretch("ts-a"), _stretch("ts-b")])
        _, bridge, _ = _stack(provider)

        bridge.import_all(project)

        assert len(_closed_entries(project, _task_titled(project, "Finished card").id)) == 2


class TestImportedTimeIsNeverSentBack:
    def test_an_import_queues_no_mirror_and_leaves_nothing_unsent(self, project):
        provider = _provider(time_on_done=[_stretch("ts-live")])
        _, bridge, outbox = _stack(provider)
        before = outbox.depth(project)

        bridge.import_all(project)

        task = _task_titled(project, "Finished card")
        assert outbox.depth(project) == before
        assert outbox.unmirrored_time(project, task.id) == []
        assert bridge.queue_unsent_time(project) == ()

    def test_a_flush_after_an_import_logs_no_time(self, project):
        provider = _provider(time_on_done=[_stretch("ts-live")])
        _, bridge, _ = _stack(provider)
        bridge.import_all(project)

        bridge.flush(project)

        assert provider.time_logs == []


class TestAFailingCard:
    def test_one_unreadable_card_does_not_stop_the_rest(self, project, monkeypatch):
        provider = _provider(time_on_done=[_stretch("ts-done")], time_on_open=[_stretch("ts-open")])
        _, bridge, _ = _stack(provider)
        real = provider.time_entries

        def time_entries(task_id):
            if task_id == "r1":
                raise ProviderError("card r1 is unreadable")
            return real(task_id)

        monkeypatch.setattr(provider, "time_entries", time_entries)

        result = bridge.import_all(project)

        time = result["boards"][0]["time"]
        assert time["entries"] == 1
        assert [f["card"] for f in time["failed"]] == ["r1"]
        assert len(_closed_entries(project, _task_titled(project, "Finished card").id)) == 1

    def test_the_failed_card_is_read_again_next_time(self, project, monkeypatch):
        provider = _provider(time_on_open=[_stretch("ts-open")])
        _, bridge, _ = _stack(provider)
        real = provider.time_entries
        monkeypatch.setattr(
            provider, "time_entries",
            _unreadable,
        )
        bridge.import_all(project)
        monkeypatch.setattr(provider, "time_entries", real)

        bridge.import_all(project)

        assert len(_closed_entries(project, _task_titled(project, "Open card").id)) == 1


class TestTheOneTimeBackfill:
    def _imported_without_time(self, project, provider):
        """Cards imported the way they were before time came with them."""
        _, bridge, _ = _stack(provider)
        saved = {tid: list(t["time"]) for tid, t in provider._tasks.items()}
        for task in provider._tasks.values():
            task["time"] = []
        bridge.import_all(project)
        for tid, entries in saved.items():
            provider._tasks[tid]["time"] = entries
        return bridge

    def test_it_brings_in_the_time_of_cards_already_imported(self, project):
        provider = _provider(time_on_done=[_stretch("ts-1")], time_on_open=[_stretch("ts-2")])
        bridge = self._imported_without_time(project, provider)

        result = bridge.backfill_remote_time(project)

        assert result["entries"] == 2
        assert result["failed"] == []

    def test_it_runs_once_per_link(self, project):
        provider = _provider(time_on_done=[_stretch("ts-1")])
        bridge = self._imported_without_time(project, provider)
        bridge.backfill_remote_time(project)
        provider._tasks["r1"]["time"].append(_stretch("ts-after"))

        second = bridge.backfill_remote_time(project)

        assert second["entries"] == 0, "caught up once; the imports keep it current"
        assert bridge.reconcile(project)["time_imported"] == 1

    def test_a_truncated_backfill_is_resumed_not_marked_done(self, project, monkeypatch):
        provider = _provider(time_on_done=[_stretch("ts-1")], time_on_open=[_stretch("ts-2")])
        bridge = self._imported_without_time(project, provider)
        monkeypatch.setattr(bridge_module, "TIME_READS_PER_BACKFILL", 1)

        first = bridge.backfill_remote_time(project)
        second = bridge.backfill_remote_time(project)
        third = bridge.backfill_remote_time(project)

        assert (first["entries"], second["entries"], third["entries"]) == (1, 1, 0)

    def test_a_backfill_that_hit_a_failure_is_tried_again(self, project, monkeypatch):
        provider = _provider(time_on_done=[_stretch("ts-1")])
        bridge = self._imported_without_time(project, provider)
        real = provider.time_entries
        monkeypatch.setattr(
            provider, "time_entries",
            _unreadable,
        )
        failed = bridge.backfill_remote_time(project)
        monkeypatch.setattr(provider, "time_entries", real)

        retried = bridge.backfill_remote_time(project)

        assert failed["entries"] == 0 and failed["failed"]
        assert retried["entries"] == 1

    def test_the_container_backfills_every_linked_project(self, project, monkeypatch):
        provider = _provider(time_on_done=[_stretch("ts-1")])
        bridge = self._imported_without_time(project, provider)
        monkeypatch.setattr(container, "task_bridge", bridge)

        assert container.backfill_remote_time() == {project: 1}
        assert container.backfill_remote_time() == {}
