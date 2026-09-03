"""The outbound bridge: linking a project to a board, and mirroring tasks."""

import pytest

from memory_mcp.asoode_client import AsoodeError
from memory_mcp.container import container
from memory_mcp.db.registry import (
    delete_project_link,
    get_default_project_link,
    get_project_links,
    upsert_project_link,
)
from memory_mcp.models import CreateTaskRequest, TaskState
from memory_mcp.services.asoode_bridge import AsoodeBridge, build_state_list_map

BOARD = {
    "id": "wp-1",
    "title": "Board",
    "lists": [
        {"id": "l-backlog", "title": "Backlog"},
        {"id": "l-todo", "title": "To Do"},
        {"id": "l-doing", "title": "In Progress"},
        {"id": "l-done", "title": "Done"},
    ],
}


class FakeClient:
    """Records calls instead of making them, and mimics asoode's externalRef rule."""

    def __init__(self, projects=None):
        self.projects = projects or []
        self.created_projects, self.created_tasks, self.state_changes = [], [], []
        self.work_packages = {}
        self._by_ref = {}
        self._next = 0

    def list_projects(self):
        return self.projects

    def find_project_by_title(self, title):
        wanted = title.strip().lower()
        return next(
            (p for p in self.projects if p["title"].strip().lower() == wanted), None
        )

    def fetch_project(self, project_id):
        return next((p for p in self.projects if p["id"] == project_id), None)

    def create_project(self, title, description="", **kw):
        project = {"id": f"proj-{len(self.created_projects)}", "title": title}
        self.created_projects.append(project)
        self.projects.append(project)
        return project

    def create_work_package(self, project_id, title, *, description="",
                            external_ref=None, board_template=5):
        # asoode returns the existing board for a repeated externalRef.
        if external_ref and external_ref in self.work_packages:
            return self.work_packages[external_ref]
        board = dict(BOARD, title=title, projectId=project_id, externalRef=external_ref)
        if external_ref:
            self.work_packages[external_ref] = board
        return board

    def create_task(self, list_id, title, *, description="", external_ref=None,
                    parent_id=None, assign_self=True, assignees=None):
        if external_ref and external_ref in self._by_ref:
            return self._by_ref[external_ref]
        self._next += 1
        task = {"id": f"remote-{self._next}", "title": title, "listId": list_id}
        if external_ref:
            self._by_ref[external_ref] = task
        self.created_tasks.append({"list_id": list_id, "title": title,
                                   "external_ref": external_ref,
                                   "description": description})
        return task

    def change_state(self, task_id, state):
        self.state_changes.append((task_id, state))
        return True


@pytest.fixture
def linked_project():
    slug = "bridge-test"
    container.project_service.init_project(slug, "Bridge Test")
    return slug


class TestStateListMap:
    def test_states_match_columns_by_title(self):
        mapping, default = build_state_list_map(BOARD)
        assert mapping["todo"] == "l-todo"
        assert mapping["in_progress"] == "l-doing"
        assert mapping["done"] == "l-done"
        assert default == "l-backlog"

    def test_every_state_gets_a_column_so_a_push_never_stalls(self):
        mapping, _ = build_state_list_map(BOARD)
        from memory_mcp.models import TaskState

        assert set(mapping) == {s.value for s in TaskState}
        # no column for these, so they land in the first one
        assert mapping["blocker"] == "l-backlog"
        assert mapping["cancelled"] == "l-backlog"

    def test_a_board_with_no_lists_yields_nothing_rather_than_guessing(self):
        assert build_state_list_map({"id": "wp", "lists": []}) == ({}, None)


class TestBootstrap:
    def test_creates_the_project_and_board_and_stores_the_link(self, linked_project):
        fake = FakeClient()
        bridge = AsoodeBridge(container.project_service, container.task_service, fake)
        result = bridge.bootstrap(linked_project)

        assert len(fake.created_projects) == 1
        assert result["work_package"]["id"] == "wp-1"
        link = get_default_project_link(linked_project)
        assert link["remote_work_package_id"] == "wp-1"
        assert link["state_list_map"]["done"] == "l-done"
        assert link["base_url"] == "https://api.asoode.com"

    def test_is_idempotent_on_the_project_uid(self, linked_project):
        fake = FakeClient()
        bridge = AsoodeBridge(container.project_service, container.task_service, fake)
        first = bridge.bootstrap(linked_project)
        second = bridge.bootstrap(linked_project)

        assert first["work_package"]["id"] == second["work_package"]["id"]
        assert len(fake.created_projects) == 1, "must not make a second project"
        assert len(get_project_links(linked_project)) == 1, "one link, not two"

    def test_reuses_an_existing_asoode_project_when_asked(self, linked_project):
        fake = FakeClient(projects=[{"id": "existing", "title": "Team Board"}])
        bridge = AsoodeBridge(container.project_service, container.task_service, fake)
        result = bridge.bootstrap(linked_project, reuse_project_id="existing")

        assert result["project"]["id"] == "existing"
        assert fake.created_projects == [], "must not create a project"

    def test_unknown_reuse_id_is_an_error(self, linked_project):
        bridge = AsoodeBridge(
            container.project_service, container.task_service, FakeClient()
        )
        with pytest.raises(AsoodeError, match="no asoode project"):
            bridge.bootstrap(linked_project, reuse_project_id="nope")

    def test_matches_an_existing_project_by_title_instead_of_duplicating(self, linked_project):
        fake = FakeClient(projects=[{"id": "p9", "title": "Bridge Test"}])
        bridge = AsoodeBridge(container.project_service, container.task_service, fake)
        result = bridge.bootstrap(linked_project)
        assert result["project"]["id"] == "p9"
        assert fake.created_projects == []


class TestPush:
    def _tasks(self, slug, *specs):
        for title, state in specs:
            task = container.task_service.create(
                CreateTaskRequest(project=slug, title=title)
            )
            if state != "todo":
                container.task_service.set_state(slug, task.id, TaskState(state))

    def test_pushes_each_task_into_the_column_for_its_state(self, linked_project):
        fake = FakeClient()
        bridge = AsoodeBridge(container.project_service, container.task_service, fake)
        bridge.bootstrap(linked_project)
        self._tasks(linked_project, ("Ship it", "todo"), ("Halfway", "in_progress"))

        result = bridge.push(linked_project)
        assert result["counts"] == {"pushed": 2, "failed": 0, "considered": 2}
        lists = {c["title"]: c["list_id"] for c in fake.created_tasks}
        assert lists["Ship it"] == "l-todo"
        assert lists["Halfway"] == "l-doing"

    def test_carries_the_state_over_because_asoode_creates_everything_as_todo(
        self, linked_project
    ):
        fake = FakeClient()
        bridge = AsoodeBridge(container.project_service, container.task_service, fake)
        bridge.bootstrap(linked_project)
        self._tasks(linked_project, ("Blocked one", "blocked"))

        bridge.push(linked_project)
        assert fake.state_changes == [("remote-1", "blocked")]

    def test_a_todo_task_needs_no_state_call(self, linked_project):
        fake = FakeClient()
        bridge = AsoodeBridge(container.project_service, container.task_service, fake)
        bridge.bootstrap(linked_project)
        self._tasks(linked_project, ("Fresh", "todo"))

        bridge.push(linked_project)
        assert fake.state_changes == []

    def test_re_pushing_does_not_duplicate(self, linked_project):
        fake = FakeClient()
        bridge = AsoodeBridge(container.project_service, container.task_service, fake)
        bridge.bootstrap(linked_project)
        self._tasks(linked_project, ("Once", "todo"), ("Twice", "todo"))

        first = bridge.push(linked_project)
        second = bridge.push(linked_project)

        assert len(fake.created_tasks) == 2, "the second push must create nothing new"
        assert (
            [t["remote_id"] for t in first["pushed"]]
            == [t["remote_id"] for t in second["pushed"]]
        )

    def test_the_external_ref_is_the_local_task_id(self, linked_project):
        fake = FakeClient()
        bridge = AsoodeBridge(container.project_service, container.task_service, fake)
        bridge.bootstrap(linked_project)
        task = container.task_service.create(
            CreateTaskRequest(project=linked_project, title="X")
        )

        bridge.push(linked_project)
        assert fake.created_tasks[0]["external_ref"] == f"memory-mcp:{task.id}"

    def test_one_failure_does_not_abort_the_rest(self, linked_project):
        fake = FakeClient()
        bridge = AsoodeBridge(container.project_service, container.task_service, fake)
        bridge.bootstrap(linked_project)
        self._tasks(linked_project, ("Good", "todo"), ("Bad", "todo"))

        original = fake.create_task

        def flaky(list_id, title, **kw):
            if title == "Bad":
                raise AsoodeError("upstream said no")
            return original(list_id, title, **kw)

        fake.create_task = flaky
        result = bridge.push(linked_project)

        assert result["counts"]["pushed"] == 1
        assert result["counts"]["failed"] == 1
        assert result["failed"][0]["title"] == "Bad"

    def test_pushing_without_a_link_says_to_bootstrap_first(self, linked_project):
        bridge = AsoodeBridge(
            container.project_service, container.task_service, FakeClient()
        )
        with pytest.raises(AsoodeError, match="not linked"):
            bridge.push(linked_project)


class TestLinkTable:
    def test_upsert_is_keyed_on_slug_and_work_package(self, linked_project):
        first = upsert_project_link(
            linked_project, base_url="https://api.asoode.com",
            remote_project_id="p1", remote_work_package_id="wp1", label="One",
        )
        second = upsert_project_link(
            linked_project, base_url="https://api.asoode.com",
            remote_project_id="p1", remote_work_package_id="wp1", label="Renamed",
        )
        assert first["id"] == second["id"]
        assert second["label"] == "Renamed"
        assert len(get_project_links(linked_project)) == 1

    def test_one_project_can_link_to_several_boards(self, linked_project):
        for i in (1, 2):
            upsert_project_link(
                linked_project, base_url="https://api.asoode.com",
                remote_project_id="p1", remote_work_package_id=f"wp{i}",
                is_default=(i == 1),
            )
        links = get_project_links(linked_project)
        assert len(links) == 2
        assert get_default_project_link(linked_project)["remote_work_package_id"] == "wp1"

    def test_json_columns_round_trip(self, linked_project):
        link = upsert_project_link(
            linked_project, base_url="https://api.asoode.com",
            remote_project_id="p1", remote_work_package_id="wp1",
            state_list_map={"todo": "l1"}, match_paths=["apps/backend"],
        )
        assert link["state_list_map"] == {"todo": "l1"}
        assert link["match_paths"] == ["apps/backend"]

    def test_delete_forgets_only_the_link(self, linked_project):
        link = upsert_project_link(
            linked_project, base_url="https://api.asoode.com",
            remote_project_id="p1", remote_work_package_id="wp1",
        )
        assert delete_project_link(link["id"]) is True
        assert get_project_links(linked_project) == []

    def test_a_project_is_never_auto_linked(self, linked_project):
        """Copies bind_backend's rule: linking is always explicit."""
        assert get_project_links(linked_project) == []


class TestRebinding:
    """Re-linking a project to a different board must actually redirect pushes."""

    def test_a_new_default_demotes_the_previous_one(self, linked_project):
        first = upsert_project_link(
            linked_project, base_url="https://api.asoode.com",
            remote_project_id="p1", remote_work_package_id="wp-old", label="Old",
        )
        second = upsert_project_link(
            linked_project, base_url="https://api.asoode.com",
            remote_project_id="p2", remote_work_package_id="wp-new", label="New",
        )
        assert first["id"] != second["id"], "a different board is a different link"

        default = get_default_project_link(linked_project)
        assert default["remote_work_package_id"] == "wp-new"
        assert len(get_project_links(linked_project)) == 2, "the old link is kept"
        old = [
            l for l in get_project_links(linked_project)
            if l["remote_work_package_id"] == "wp-old"
        ][0]
        assert old["is_default"] is False

    def test_push_follows_the_new_board(self, linked_project):
        fake = FakeClient()
        bridge = AsoodeBridge(container.project_service, container.task_service, fake)
        bridge.bootstrap(linked_project)
        container.task_service.create(
            CreateTaskRequest(project=linked_project, title="Follow me")
        )
        upsert_project_link(
            linked_project, base_url="https://api.asoode.com",
            remote_project_id="p2", remote_work_package_id="wp-new",
            default_list_id="l-new", state_list_map={"todo": "l-new"},
        )
        bridge.push(linked_project)
        assert fake.created_tasks[0]["list_id"] == "l-new"

    def test_explicit_non_default_link_does_not_steal_the_default(self, linked_project):
        upsert_project_link(
            linked_project, base_url="https://api.asoode.com",
            remote_project_id="p1", remote_work_package_id="wp-main",
        )
        upsert_project_link(
            linked_project, base_url="https://api.asoode.com",
            remote_project_id="p1", remote_work_package_id="wp-side",
            is_default=False,
        )
        assert get_default_project_link(linked_project)["remote_work_package_id"] == "wp-main"
