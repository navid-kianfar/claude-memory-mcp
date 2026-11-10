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
        path_resolver=lambda project, **kw: bridge_box["bridge"].routing_for(project, **kw),
        board_mover=lambda project, from_id, to_id: bridge_box["bridge"].can_move_card(
            project, from_id, to_id),
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


def _link_id(slug, label):
    from memory_mcp.db.registry import get_project_links

    return next(l["id"] for l in get_project_links(slug) if l["label"] == label)


class TestBoardMove:
    def test_a_mirrored_task_moves_instead_of_being_refused(self, project):
        from memory_mcp.models import UpdateTaskRequest

        provider = _provider()
        tasks, bridge, outbox = _stack(provider)
        task = _create(tasks, project)
        bridge.flush(project)

        moved, changed, routing = tasks.update_routed(
            UpdateTaskRequest(project=project, task_id=task.id, target="other"),
        )

        assert moved.link_id == _link_id(project, "other")
        assert "link_id" in changed
        assert "move" in _ops(outbox, project)

    def test_the_card_moves_and_its_mapping_follows(self, project):
        from memory_mcp.models import UpdateTaskRequest

        provider = _provider()
        tasks, bridge, outbox = _stack(provider)
        task = _create(tasks, project)
        bridge.flush(project)
        tasks.update(UpdateTaskRequest(project=project, task_id=task.id, target="other"))

        bridge.flush(project)

        assert provider.board_moves == [("r1", "wp2", "o-todo")]
        assert len(provider.created_tasks) == 1, "a move must never create a second card"
        assert outbox.remote_id(project, task.id, _link_id(project, "other")) == "r1"
        assert outbox.remote_id(project, task.id, _link_id(project, "board")) is None

    def test_the_card_lands_in_the_column_of_its_state(self, project):
        from memory_mcp.models import TaskState, UpdateTaskRequest

        provider = _provider()
        tasks, bridge, _ = _stack(provider)
        task = _create(tasks, project)
        tasks.set_state(project, task.id, TaskState.IN_PROGRESS)
        bridge.flush(project)
        tasks.update(UpdateTaskRequest(project=project, task_id=task.id, target="other"))

        bridge.flush(project)

        assert provider.board_moves == [("r1", "wp2", "o-doing")]

    def test_sub_tasks_travel_with_the_card(self, project):
        from memory_mcp.models import TaskState, UpdateTaskRequest

        provider = _provider()
        tasks, bridge, outbox = _stack(provider)
        parent = _create(tasks, project, "Parent")
        child = _create(tasks, project, "Child", parent_id=parent.id)
        bridge.flush(project)
        other = _link_id(project, "other")

        tasks.update(UpdateTaskRequest(project=project, task_id=parent.id, target="other"))
        assert tasks.get(project, child.id).link_id == other
        tasks.set_state(project, child.id, TaskState.DONE)
        bridge.flush(project)

        assert [m[0] for m in provider.board_moves] == ["r1"], "the child rides along"
        assert outbox.remote_id(project, child.id, other) == "r2"
        assert ("r2", "done") in provider.states
        assert len(provider.created_tasks) == 2

    def test_an_op_queued_before_the_move_does_not_create_a_second_card(self, project):
        from memory_mcp.models import UpdateTaskRequest

        provider = _provider()
        tasks, bridge, _ = _stack(provider)
        task = _create(tasks, project)
        bridge.flush(project)
        tasks.comment(project, task.id, "queued while still on the first board")
        tasks.update(UpdateTaskRequest(project=project, task_id=task.id, target="other"))

        bridge.flush(project)

        assert len(provider.created_tasks) == 1
        assert provider.board_moves == [("r1", "wp2", "o-todo")]
        assert provider.comments == [("r1", "queued while still on the first board")]

    def test_without_the_capability_the_reroute_is_still_refused(self, project):
        from memory_mcp.models import UpdateTaskRequest
        from memory_mcp.services.task_service import MirroredRerouteError

        provider = _provider(with_bulk=False)
        tasks, bridge, _ = _stack(provider)
        task = _create(tasks, project)
        bridge.flush(project)

        with pytest.raises(MirroredRerouteError):
            tasks.update(UpdateTaskRequest(project=project, task_id=task.id, target="other"))
        assert tasks.get(project, task.id).link_id is None

    def test_a_board_of_another_memory_project_is_never_a_move_target(self, project):
        provider = _provider()
        _, bridge, _ = _stack(provider)
        stranger = "bulk-mirror-stranger"
        container.project_service.init_project(stranger, "Stranger")
        upsert_project_link(
            stranger, base_url="https://api.asoode.com", remote_project_id="p9",
            remote_work_package_id="wp9", label="theirs", is_default=True,
            default_list_id="x", state_list_map={},
        )

        assert not bridge.can_move_card(
            project, _link_id(project, "board"), _link_id(stranger, "theirs"),
        )
        assert bridge.can_move_card(
            project, _link_id(project, "board"), _link_id(project, "other"),
        )

    def test_boards_on_different_servers_cannot_trade_cards(self, project):
        provider = _provider()
        _, bridge, _ = _stack(provider)
        upsert_project_link(
            project, base_url="https://asoode.example.org", remote_project_id="p5",
            remote_work_package_id="wp5", label="elsewhere", is_default=False,
            default_list_id="e", state_list_map={},
        )

        assert not bridge.can_move_card(
            project, _link_id(project, "board"), _link_id(project, "elsewhere"),
        )
