"""One memory project, many asoode boards: which one does a task go to?

The user's model is a monorepo where each app has its own work package, so
"the project's board" is not a thing - a task has to be able to name its own.
"""

import pytest

from memory_mcp.providers import ProviderError
from memory_mcp.container import container
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.models import CreateTaskRequest


@pytest.fixture
def project():
    slug = "routing-test"
    container.project_service.init_project(slug, "Routing Test")
    return slug


def _link(slug, wp, label, is_default=False):
    return upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id=wp, label=label, is_default=is_default,
        default_list_id="l-todo", state_list_map={"todo": "l-todo", "done": "l-done"},
    )


class TestResolvingABoardName:
    def test_by_label(self, project):
        link = _link(project, "wp-worker", "worker")
        assert container.task_bridge.resolve_link(project, "worker") == link["id"]

    def test_is_case_insensitive_on_the_label(self, project):
        """The label is the human-typed identifier."""
        link = _link(project, "wp-worker", "worker")
        assert container.task_bridge.resolve_link(project, "WORKER") == link["id"]

    def test_by_work_package_id(self, project):
        link = _link(project, "wp-worker", "worker")
        assert container.task_bridge.resolve_link(project, "wp-worker") == link["id"]

    def test_no_target_means_the_default(self, project):
        _link(project, "wp-worker", "worker", is_default=True)
        assert container.task_bridge.resolve_link(project, None) is None

    def test_a_wrong_name_is_rejected_and_lists_the_real_ones(self, project):
        _link(project, "wp-worker", "worker")
        _link(project, "wp-backend", "backend")
        with pytest.raises(ProviderError) as e:
            container.task_bridge.resolve_link(project, "frontend")
        assert "worker" in str(e.value) and "backend" in str(e.value)

    def test_targeting_an_unlinked_project_says_to_attach(self, project):
        with pytest.raises(ProviderError, match="memory_asoode_attach"):
            container.task_bridge.resolve_link(project, "worker")


class TestTheRoutingRule:
    def test_a_task_goes_to_the_board_it_names(self, project):
        _link(project, "wp-backend", "backend", is_default=True)
        _link(project, "wp-worker", "worker")
        task = container.task_service.create(CreateTaskRequest(
            project=project, title="Worker thing", target="worker"))
        assert container.task_bridge.route(project, task)["remote_work_package_id"] == "wp-worker"

    def test_a_task_with_no_target_goes_to_the_default(self, project):
        _link(project, "wp-backend", "backend", is_default=True)
        _link(project, "wp-worker", "worker")
        task = container.task_service.create(
            CreateTaskRequest(project=project, title="Unspecified"))
        assert task.link_id is None
        assert container.task_bridge.route(project, task)["remote_work_package_id"] == "wp-backend"

    def test_tasks_predating_the_column_still_route(self, project):
        """Defaulting rather than refusing is why upgrading does not strand them."""
        _link(project, "wp-backend", "backend", is_default=True)
        task = container.task_service.create(
            CreateTaskRequest(project=project, title="Legacy"))
        assert task.link_id is None
        assert container.task_bridge.route(project, task) is not None

    def test_an_unlinked_project_routes_nowhere_rather_than_failing(self, project):
        task = container.task_service.create(
            CreateTaskRequest(project=project, title="No boards"))
        assert container.task_bridge.route(project, task) is None

    def test_links_but_no_default_refuses_instead_of_guessing(self, project):
        _link(project, "wp-a", "a", is_default=False)
        _link(project, "wp-b", "b", is_default=False)
        task = container.task_service.create(
            CreateTaskRequest(project=project, title="Ambiguous"))
        with pytest.raises(ProviderError, match="no default"):
            container.task_bridge.route(project, task)

    def test_a_deleted_link_falls_back_rather_than_dropping_the_task(self, project):
        from memory_mcp.db.registry import delete_project_link

        _link(project, "wp-backend", "backend", is_default=True)
        gone = _link(project, "wp-worker", "worker")
        task = container.task_service.create(CreateTaskRequest(
            project=project, title="Orphaned", target="worker"))
        delete_project_link(gone["id"])
        assert container.task_bridge.route(project, task)["remote_work_package_id"] == "wp-backend"

    def test_a_bad_target_fails_the_create_rather_than_landing_anywhere(self, project):
        _link(project, "wp-backend", "backend", is_default=True)
        with pytest.raises(ProviderError):
            container.task_service.create(CreateTaskRequest(
                project=project, title="Typo", target="wroker"))
        assert container.task_service.list_tasks(project, limit=10).total == 0
