"""The mirror on a platform with the bulk API: real deletes, board moves,
batched archives.

Driven through the real TaskService -> outbox -> TaskBridge path against
FakeProvider(with_bulk=True), whose bulk calls have asoode's tree semantics -
remove deletes a card's sub-tasks, move carries them, archive applies to each.
A provider without the capabilities keeps the old behaviour; those paths are
pinned by test_mirror_parity.py and test_board_columns.py.
"""

import pytest

from memory_mcp.container import container
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.models import CreateTaskRequest
from memory_mcp.providers import ProviderError
from memory_mcp.repositories import OutboxRepository
from memory_mcp.services.task_bridge import TaskBridge
from memory_mcp.services.task_service import TaskService
from tests.providers.fakes import FakeProvider

BOARD_LISTS = {"todo": "l-todo", "in_progress": "l-doing", "done": "l-done"}
OTHER_LISTS = {"todo": "o-todo", "in_progress": "o-doing", "done": "o-done"}


def _provider(with_bulk=True):
    p = FakeProvider(with_bulk=with_bulk)
    p.seed(container_id="wp1", title="Board", space_id="p1",
           groups=(("l-todo", "To Do"), ("l-doing", "In Progress"), ("l-done", "Done")))
    p.seed(container_id="wp2", title="Other", space_id="p1",
           groups=(("o-todo", "To Do"), ("o-doing", "In Progress"), ("o-done", "Done")))
    return p


@pytest.fixture
def project():
    slug = "bulk-mirror-test"
    container.project_service.init_project(slug, "Bulk Mirror Test")
    upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id="wp1", label="board", is_default=True,
        default_list_id="l-todo", state_list_map=BOARD_LISTS,
    )
    upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id="wp2", label="other", is_default=False,
        default_list_id="o-todo", state_list_map=OTHER_LISTS,
    )
    return slug


def _stack(provider):
    outbox = OutboxRepository()
    bridge_box: dict = {}
    tasks = TaskService(
        container.task_repo, container.provenance_repo, container.project_repo,
        container.session_repo, outbox_repo=outbox,
        link_resolver=lambda project, target: bridge_box["bridge"].resolve_link(
            project, target),
    )
    bridge = TaskBridge(container.project_service, tasks, provider, outbox_repo=outbox)
    bridge_box["bridge"] = bridge
    return tasks, bridge, outbox


def _create(tasks, slug, title="Task", **kw):
    return tasks.create(CreateTaskRequest(project=slug, title=title, **kw))


def _ops(outbox, slug):
    return [r["op"] for r in outbox.pending(slug)]


class TestRealDelete:
    def test_the_card_is_deleted_not_archived(self, project):
        provider = _provider()
        tasks, bridge, _ = _stack(provider)
        task = _create(tasks, project)
        bridge.flush(project)

        tasks.delete(project, task.id)
        bridge.flush(project)

        assert provider.deleted == ["r1"]
        assert provider.archived == [], "a platform that can delete must not just archive"

    def test_the_promote_is_queued_before_the_delete(self, project):
        provider = _provider()
        tasks, bridge, outbox = _stack(provider)
        parent = _create(tasks, project, "Parent")
        _create(tasks, project, "Child", parent_id=parent.id)
        bridge.flush(project)

        tasks.delete(project, parent.id)

        assert _ops(outbox, project) == ["parent", "delete"]

    def test_a_promoted_child_survives_its_parents_delete_on_the_board(self, project):
        """THE trap: local delete promotes children, the platform deletes the
        tree. The child's promote must land before the parent's remove."""
        provider = _provider()
        tasks, bridge, _ = _stack(provider)
        parent = _create(tasks, project, "Parent")
        child = _create(tasks, project, "Child", parent_id=parent.id)
        bridge.flush(project)
        child_card = "r2"

        tasks.delete(project, parent.id)
        bridge.flush(project)

        promote_at = provider.calls.index(("promote", child_card))
        remove_at = provider.calls.index(("bulk", "remove", ("r1",)))
        assert promote_at < remove_at
        assert provider.deleted == ["r1"], "only the parent card goes"
        assert child_card in provider._tasks, "the promoted child must still be on the board"
        assert tasks.get(project, child.id).parent_id is None

    def test_the_child_is_saved_even_when_its_promote_row_was_lost(self, project):
        """A promote row given up on (or unroutable) must not let the remove
        take the child: the delete promotes the children it carries itself."""
        provider = _provider()
        tasks, bridge, outbox = _stack(provider)
        parent = _create(tasks, project, "Parent")
        _create(tasks, project, "Child", parent_id=parent.id)
        bridge.flush(project)
        tasks.delete(project, parent.id)
        promote_row = next(r for r in outbox.pending(project) if r["op"] == "parent")
        outbox.resolve(project, promote_row["id"])

        bridge.flush(project)

        assert "r2" in provider._tasks
        assert provider._tasks["r2"].get("parent") is None

    def test_a_child_already_gone_does_not_block_the_delete(self, project):
        provider = _provider()
        tasks, bridge, _ = _stack(provider)
        parent = _create(tasks, project, "Parent")
        _create(tasks, project, "Child", parent_id=parent.id)
        bridge.flush(project)
        del provider._tasks["r2"]  # someone deleted the sub-task on the board

        tasks.delete(project, parent.id)
        result = bridge.flush(project)

        assert provider.deleted == ["r1"]
        assert result["failed"] == 0

    def test_a_delete_the_board_skipped_stays_queued_and_is_reported(self, project):
        provider = _provider()
        tasks, bridge, outbox = _stack(provider)
        task = _create(tasks, project)
        bridge.flush(project)
        provider._tasks.pop("r1")  # gone, or no admin access - the board cannot say which

        tasks.delete(project, task.id)
        result = bridge.flush(project)

        assert result["failed"] == 1
        assert _ops(outbox, project) == ["delete"]
        assert "did not delete" in (outbox.pending(project)[0]["last_error"] or "")

    def test_without_the_capability_the_card_is_still_archived(self, project):
        provider = _provider(with_bulk=False)
        tasks, bridge, _ = _stack(provider)
        task = _create(tasks, project)
        bridge.flush(project)

        tasks.delete(project, task.id)
        bridge.flush(project)

        assert provider.deleted == []
        assert ("r1", True) in provider.archived

    def test_a_dead_remote_keeps_the_delete(self, project):
        provider = _provider()
        tasks, bridge, outbox = _stack(provider)
        task = _create(tasks, project)
        bridge.flush(project)
        provider.fail = ProviderError("down")

        tasks.delete(project, task.id)
        bridge.flush(project)

        assert _ops(outbox, project) == ["delete"]
