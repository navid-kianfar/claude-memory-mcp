"""Auto-mirroring: a local mutation queues, a flusher drains, nothing is lost.

The requirement that shapes all of it: if asoode is unreachable the local write
must STILL succeed and the mutation must be retried later. A failed mirror can
never lose or block a local task.
"""

import pytest

from memory_mcp.asoode_client import AsoodeError
from memory_mcp.container import container
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.models import CreateTaskRequest, TaskFilter
from memory_mcp.repositories import OutboxRepository
from memory_mcp.services.asoode_bridge import AsoodeBridge
from memory_mcp.services.task_service import TaskService

BOARD_LISTS = {"todo": "l-todo", "in_progress": "l-doing", "done": "l-done"}


class Client:
    def __init__(self, fail=None):
        self.fail = fail
        self.created, self.states, self.comments, self.moves = [], [], [], []
        self._by_ref, self._n = {}, 0

    def _boom(self):
        if self.fail:
            raise self.fail

    def create_task(self, list_id, title, *, description="", external_ref=None, **kw):
        self._boom()
        if external_ref and external_ref in self._by_ref:
            return self._by_ref[external_ref]
        self._n += 1
        task = {"id": f"r{self._n}", "title": title}
        if external_ref:
            self._by_ref[external_ref] = task
        self.created.append({"list_id": list_id, "title": title, "ref": external_ref})
        return task

    def change_state(self, task_id, state):
        self._boom()
        self.states.append((task_id, state))

    def reposition(self, task_id, list_id, order=0):
        self.moves.append((task_id, list_id))

    def comment(self, task_id, message, private=False):
        self._boom()
        self.comments.append((task_id, message))


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
        tasks, _, outbox = _stack(project, Client())
        tasks.create(CreateTaskRequest(project=project, title="New"))
        assert outbox.depth(project) == 1

    def test_completing_a_task_queues_one(self, project):
        tasks, _, outbox = _stack(project, Client())
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        tasks.done(project, task.id)
        assert outbox.depth(project) >= 2

    def test_a_comment_queues_one(self, project):
        tasks, _, outbox = _stack(project, Client())
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        tasks.comment(project, task.id, "a note")
        ops = [r["op"] for r in outbox.pending(project)]
        assert "comment" in ops

    def test_a_local_only_edit_queues_nothing(self, project):
        """Reordering has no remote meaning; queuing it would be a wasted call."""
        tasks, _, outbox = _stack(project, Client())
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        before = outbox.depth(project)
        tasks.reorder(project, [task.id])
        assert outbox.depth(project) == before


class TestFlush:
    def test_drains_and_creates_remotely(self, project):
        client = Client()
        tasks, bridge, outbox = _stack(project, client)
        tasks.create(CreateTaskRequest(project=project, title="Ship it"))
        result = bridge.flush(project)

        assert result["flushed"] == 1
        assert outbox.depth(project) == 0
        assert client.created[0]["title"] == "Ship it"

    def test_the_external_ref_is_the_local_id(self, project):
        client = Client()
        tasks, bridge, _ = _stack(project, client)
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        bridge.flush(project)
        assert client.created[0]["ref"] == f"memory-mcp:{task.id}"

    def test_the_remote_id_is_remembered_so_edits_do_not_re_create(self, project):
        client = Client()
        tasks, bridge, outbox = _stack(project, client)
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        bridge.flush(project)
        link = bridge.route(project, tasks.get(project, task.id))
        assert outbox.remote_id(project, task.id, link["id"]) == "r1"

        tasks.done(project, task.id)
        bridge.flush(project)
        assert len(client.created) == 1, "an edit must not create a second remote task"

    def test_completion_mirrors_state_and_moves_the_card(self, project):
        client = Client()
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
        client = Client()
        tasks, bridge, _ = _stack(project, client)
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        tasks.comment(project, task.id, "what I learned")
        bridge.flush(project)
        assert client.comments == [("r1", "what I learned")]


class TestUnreachableAsoodeNeverLosesAWrite:
    def test_the_local_task_survives_a_dead_remote(self, project):
        tasks, bridge, outbox = _stack(project, Client(fail=AsoodeError("down")))
        task = tasks.create(CreateTaskRequest(project=project, title="Offline work"))

        assert tasks.get(project, task.id).title == "Offline work"
        result = bridge.flush(project)
        assert result["failed"] == 1
        assert outbox.depth(project) == 1, "the row stays, to retry"

    def test_the_row_is_retried_when_the_remote_returns(self, project):
        client = Client(fail=AsoodeError("down"))
        tasks, bridge, outbox = _stack(project, client)
        tasks.create(CreateTaskRequest(project=project, title="Deferred"))
        bridge.flush(project)
        assert outbox.depth(project) == 1

        client.fail = None
        assert bridge.flush(project)["flushed"] == 1
        assert outbox.depth(project) == 0
        assert client.created[0]["title"] == "Deferred"

    def test_the_failure_is_recorded_rather_than_silent(self, project):
        tasks, bridge, outbox = _stack(project, Client(fail=AsoodeError("boom")))
        tasks.create(CreateTaskRequest(project=project, title="X"))
        bridge.flush(project)
        row = outbox.pending(project)[0]
        assert row["attempts"] == 1
        assert "boom" in row["last_error"]

    def test_it_stops_at_the_first_failure_rather_than_hammering(self, project):
        client = Client(fail=AsoodeError("down"))
        tasks, bridge, _ = _stack(project, client)
        for i in range(3):
            tasks.create(CreateTaskRequest(project=project, title=f"T{i}"))
        result = bridge.flush(project)
        assert result["failed"] == 1, "one attempt, not one per queued row"

    def test_an_unlinked_project_drops_rows_instead_of_retrying_forever(self, project):
        from memory_mcp.db.registry import delete_project_link, get_project_links

        tasks, bridge, outbox = _stack(project, Client())
        tasks.create(CreateTaskRequest(project=project, title="X"))
        for link in get_project_links(project):
            delete_project_link(link["id"])

        result = bridge.flush(project)
        assert result["skipped"] == 1
        assert outbox.depth(project) == 0

    def test_a_task_deleted_before_the_flush_is_not_an_error(self, project):
        tasks, bridge, outbox = _stack(project, Client())
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
