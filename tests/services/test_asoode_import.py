"""Reading asoode boards back into the local store.

The defect this fixes: 35 tasks existed on the user's boards and zero locally,
because they were created in asoode and there was no read path. Not data loss -
a missing direction.
"""

import pytest

from memory_mcp.providers import ProviderError
from memory_mcp.container import container
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.models import TaskFilter
from memory_mcp.services.asoode_bridge import AsoodeBridge
from tests.providers.fakes import FakeProvider


def _provider(tasks=None, fail=None):
    """A board with two tasks: one open, one already finished in asoode."""
    provider = FakeProvider(fail=fail)
    provider.seed(
        container_id="wp1", title="Board", space_id="p1",
        groups=(("l-todo", "To Do"), ("l-done", "Done")),
        tasks=[{"id": "r1", "title": "Made in asoode", "state": "todo",
                "description": "written by a human", "group_id": "l-todo"},
               {"id": "r2", "title": "Already finished", "state": "done",
                "group_id": "l-done"}] if tasks is None else tasks,
    )
    return provider


@pytest.fixture
def project():
    slug = "import-test"
    container.project_service.init_project(slug, "Import Test")
    upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id="wp1", label="board", is_default=True,
        default_list_id="l-todo", state_list_map={"todo": "l-todo", "done": "l-done"},
    )
    return slug


def _bridge(client=None):
    return AsoodeBridge(
        container.project_service, container.task_service, client or _provider(),
        outbox_repo=container.outbox_repo,
    )


class TestImport:
    def test_creates_local_tasks_from_the_board(self, project):
        result = _bridge().import_all(project)
        assert result["counts"]["created"] == 2
        titles = {t.title for t in container.task_service.list_tasks(
            project, TaskFilter(include_done=True), limit=10).tasks}
        assert titles == {"Made in asoode", "Already finished"}

    def test_the_remote_state_carries_over(self, project):
        _bridge().import_all(project)
        tasks = {t.title: t for t in container.task_service.list_tasks(
            project, TaskFilter(include_done=True), limit=10).tasks}
        assert tasks["Already finished"].state.value == "done"
        assert tasks["Made in asoode"].state.value == "todo"

    def test_imported_tasks_are_attributed_to_asoode(self, project):
        _bridge().import_all(project)
        sources = {t.source for t in container.task_service.list_tasks(
            project, TaskFilter(include_done=True), limit=10).tasks}
        assert sources == {"asoode"}

    def test_they_are_routed_back_to_the_board_they_came_from(self, project):
        _bridge().import_all(project)
        task = container.task_service.list_tasks(project, limit=10).tasks[0]
        assert task.link_id is not None

    def test_re_importing_does_not_duplicate(self, project):
        bridge = _bridge()
        bridge.import_all(project)
        second = bridge.import_all(project)
        assert second["counts"]["created"] == 0
        assert container.task_service.list_tasks(
            project, TaskFilter(include_done=True), limit=20).total == 2

    def test_identity_is_the_remote_id_not_the_title(self, project):
        """A task created in asoode has no externalRef, so the id is all there is."""
        provider = _provider()
        bridge = _bridge(provider)
        bridge.import_all(project)
        provider._tasks["r1"]["title"] = "Renamed in asoode"
        result = bridge.import_all(project)

        assert result["counts"]["created"] == 0, "a rename is not a new task"
        assert result["counts"]["updated"] == 1
        titles = {t.title for t in container.task_service.list_tasks(
            project, TaskFilter(include_done=True), limit=10).tasks}
        assert "Renamed in asoode" in titles and "Made in asoode" not in titles

    def test_a_remote_state_change_updates_the_local_task(self, project):
        provider = _provider()
        bridge = _bridge(provider)
        bridge.import_all(project)
        provider._tasks["r1"]["state"] = "done"
        bridge.import_all(project)
        tasks = {t.title: t for t in container.task_service.list_tasks(
            project, TaskFilter(include_done=True), limit=10).tasks}
        assert tasks["Made in asoode"].state.value == "done"

    def test_an_import_does_not_bounce_straight_back_out(self, project):
        """Importing writes through TaskService, which queues a mirror for every
        write - 35 imported tasks would immediately push 35 back."""
        before = container.outbox_repo.depth(project)
        _bridge().import_all(project)
        assert container.outbox_repo.depth(project) == before

    def test_a_titleless_remote_row_is_skipped_not_crashed_on(self, project):
        provider = _provider(tasks=[{"id": "r9", "title": "   ", "state": "todo"}])
        result = _bridge(provider).import_all(project)
        assert result["boards"][0]["skipped"] == 1
        assert result["counts"]["created"] == 0

    def test_an_unlinked_project_says_so(self, project):
        from memory_mcp.db.registry import delete_project_link, get_project_links

        for link in get_project_links(project):
            delete_project_link(link["id"])
        with pytest.raises(ProviderError, match="not linked"):
            _bridge().import_all(project)

    def test_one_unreadable_board_does_not_abort_the_rest(self, project):
        upsert_project_link(
            project, base_url="https://api.asoode.com", remote_project_id="p1",
            remote_work_package_id="wp-broken", label="broken", is_default=False,
        )

        # wp-broken is never seeded, so fetching it raises - the same shape as a
        # board the credential cannot read.
        result = _bridge(_provider()).import_all(project)
        assert result["counts"]["created"] == 2
        assert result["failed"][0]["board"] == "broken"
