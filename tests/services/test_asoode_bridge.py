"""The outbound bridge: linking a project to a board, and mirroring tasks."""

import pytest

from memory_mcp.providers import Container, Group, ProviderError
from memory_mcp.container import container
from memory_mcp.db.registry import (
    delete_project_link,
    get_default_project_link,
    get_project_links,
    upsert_project_link,
)
from memory_mcp.models import CreateTaskRequest, TaskState
from memory_mcp.services.task_bridge import TaskBridge, build_state_list_map
from tests.providers.fakes import FakeProvider

def _provider():
    """Two boards that already exist - what `attach` links to - plus the space
    they live in, so `bootstrap` has somewhere to create."""
    p = FakeProvider()
    p.seed(container_id="wp-worker", title="Worker & Jobs", space_id="proj-1",
           external_ref="app-worker",
           groups=(("l-backlog", "Backlog"), ("l-todo", "To Do"),
                   ("l-doing", "In Progress"), ("l-done", "Done")))
    p.seed(container_id="wp-backend", title="Backend API", space_id="proj-1",
           external_ref="app-backend",
           groups=(("l-backlog", "Backlog"), ("l-todo", "To Do"),
                   ("l-doing", "In Progress"), ("l-done", "Done")))
    return p


@pytest.fixture
def linked_project():
    slug = "bridge-test"
    container.project_service.init_project(slug, "Bridge Test")
    return slug


class TestStateListMap:
    def _board(self):
        return Container(id="wp1", title="Board", groups=(
            Group(id="l-backlog", title="Backlog"), Group(id="l-todo", title="To Do"),
            Group(id="l-doing", title="In Progress"), Group(id="l-done", title="Done")))

    def test_states_match_columns_by_title(self):
        mapping, default = build_state_list_map(self._board())
        assert mapping["todo"] == "l-todo"
        assert mapping["in_progress"] == "l-doing"
        assert mapping["done"] == "l-done"
        assert default == "l-backlog"

    def test_every_state_gets_a_column_so_a_push_never_stalls(self):
        mapping, _ = build_state_list_map(self._board())
        from memory_mcp.models import TaskState

        assert set(mapping) == {s.value for s in TaskState}
        # no column for these, so they land in the first one
        assert mapping["blocker"] == "l-backlog"
        assert mapping["cancelled"] == "l-backlog"

    def test_a_board_with_no_lists_yields_nothing_rather_than_guessing(self):
        assert build_state_list_map(Container(id="wp", title="B")) == ({}, None)


class TestBootstrap:
    def test_creates_the_project_and_board_and_stores_the_link(self, linked_project):
        fake = _provider()
        bridge = TaskBridge(container.project_service, container.task_service, fake)
        result = bridge.bootstrap(linked_project)

        assert len(fake.created_spaces) == 1
        assert result["work_package"]["id"], "a container was created"
        link = get_default_project_link(linked_project)
        assert link["remote_work_package_id"] == result["work_package"]["id"]
        columns = {item["title"]: item["id"] for item in result["lists"]}
        assert link["state_list_map"]["done"] == columns["Done"]
        assert link["base_url"] == "https://api.asoode.com"

    def test_is_idempotent_on_the_project_uid(self, linked_project):
        fake = _provider()
        bridge = TaskBridge(container.project_service, container.task_service, fake)
        first = bridge.bootstrap(linked_project)
        second = bridge.bootstrap(linked_project)

        assert first["work_package"]["id"] == second["work_package"]["id"]
        assert len(fake.created_spaces) == 1, "must not make a second space"
        assert len(get_project_links(linked_project)) == 1, "one link, not two"

    def test_reuses_an_existing_asoode_project_when_asked(self, linked_project):
        fake = _provider()
        space = fake.create_space("Team Board")
        before = list(fake.created_spaces)
        bridge = TaskBridge(container.project_service, container.task_service, fake)
        result = bridge.bootstrap(linked_project, reuse_project_id=space.id)

        assert result["project"]["id"] == space.id
        assert fake.created_spaces == before, "must not create another space"

    def test_unknown_reuse_id_is_an_error(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider()
        )
        with pytest.raises(ProviderError, match="no space with id"):
            bridge.bootstrap(linked_project, reuse_project_id="nope")

    def test_matches_an_existing_space_by_title_instead_of_duplicating_it(self, linked_project):
        """A space carries no external ref, so its title is the only handle -
        creating a second one of the same name is worse than reusing it."""
        fake = _provider()
        existing = fake.create_space("Bridge Test")   # the project's display name
        before = list(fake.created_spaces)

        bridge = TaskBridge(container.project_service, container.task_service, fake)
        result = bridge.bootstrap(linked_project)

        assert result["project"]["id"] == existing.id
        assert fake.created_spaces == before, "must reuse, not duplicate"


class TestPush:
    def _tasks(self, slug, *specs):
        for title, state in specs:
            task = container.task_service.create(
                CreateTaskRequest(project=slug, title=title)
            )
            if state != "todo":
                container.task_service.set_state(slug, task.id, TaskState(state))

    def test_pushes_each_task_into_the_column_for_its_state(self, linked_project):
        fake = _provider()
        bridge = TaskBridge(container.project_service, container.task_service, fake)
        board = bridge.bootstrap(linked_project)
        columns = {item["title"]: item["id"] for item in board["lists"]}
        self._tasks(linked_project, ("Ship it", "todo"), ("Halfway", "in_progress"))

        result = bridge.push(linked_project)
        assert result["counts"] == {"pushed": 2, "failed": 0, "considered": 2}
        lists = {c["title"]: c["list_id"] for c in fake.created_tasks}
        assert lists["Ship it"] == columns["To Do"]
        assert lists["Halfway"] == columns["In Progress"]

    def test_carries_the_state_over_because_asoode_creates_everything_as_todo(
        self, linked_project
    ):
        fake = _provider()
        bridge = TaskBridge(container.project_service, container.task_service, fake)
        bridge.bootstrap(linked_project)
        self._tasks(linked_project, ("Blocked one", "blocked"))

        bridge.push(linked_project)
        assert [state for _, state in fake.states] == ["blocked"]

    def test_a_todo_task_needs_no_state_call(self, linked_project):
        fake = _provider()
        bridge = TaskBridge(container.project_service, container.task_service, fake)
        bridge.bootstrap(linked_project)
        self._tasks(linked_project, ("Fresh", "todo"))

        bridge.push(linked_project)
        assert fake.states == []

    def test_re_pushing_does_not_duplicate(self, linked_project):
        fake = _provider()
        bridge = TaskBridge(container.project_service, container.task_service, fake)
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
        fake = _provider()
        bridge = TaskBridge(container.project_service, container.task_service, fake)
        bridge.bootstrap(linked_project)
        task = container.task_service.create(
            CreateTaskRequest(project=linked_project, title="X")
        )

        bridge.push(linked_project)
        assert fake.created_tasks[0]["external_ref"] == f"memory-mcp:{task.id}"

    def test_one_failure_does_not_abort_the_rest(self, linked_project):
        fake = _provider()
        bridge = TaskBridge(container.project_service, container.task_service, fake)
        bridge.bootstrap(linked_project)
        self._tasks(linked_project, ("Good", "todo"), ("Bad", "todo"))

        original = fake.create_task

        def flaky(container_id, group_id, title, **kw):
            if title == "Bad":
                raise ProviderError("upstream said no")
            return original(container_id, group_id, title, **kw)

        fake.create_task = flaky
        result = bridge.push(linked_project)

        assert result["counts"]["pushed"] == 1
        assert result["counts"]["failed"] == 1
        assert result["failed"][0]["title"] == "Bad"

    def test_pushing_without_a_link_says_to_bootstrap_first(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider()
        )
        with pytest.raises(ProviderError, match="not linked"):
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
        fake = _provider()
        bridge = TaskBridge(container.project_service, container.task_service, fake)
        bridge.bootstrap(linked_project)
        container.task_service.create(
            CreateTaskRequest(project=linked_project, title="Follow me")
        )
        other = fake.seed(container_id="wp-new", title="Other", space_id="proj-1",
                          groups=(("l-new", "New"),))
        upsert_project_link(
            linked_project, base_url="https://api.asoode.com",
            remote_project_id="proj-1", remote_work_package_id=other.id,
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


class TestAttachExisting:
    """Linking boards that already exist - the normal case for a monorepo.

    bootstrap CREATES a board. Running it on a workspace whose per-app boards
    already exist adds a duplicate beside the real ones, which is exactly what
    happened to the asoode project's nine app boards.
    """

    def test_attaches_without_creating_anything(self, linked_project):
        fake = _provider()
        bridge = TaskBridge(container.project_service, container.task_service, fake)
        result = bridge.attach(linked_project, external_ref="app-worker")

        assert result["created"] is False
        assert fake.created_spaces == []
        assert fake.created_spaces == [], "attach must not create anything"
        assert result["work_package"]["id"] == "wp-worker"

    def test_resolves_by_external_ref(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider()
        )
        result = bridge.attach(linked_project, external_ref="app-backend")
        assert result["work_package"]["title"] == "Backend API"

    def test_resolves_by_work_package_id(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider()
        )
        result = bridge.attach(linked_project, work_package_id="wp-worker")
        assert result["work_package"]["external_ref"] == "app-worker"

    def test_an_unknown_ref_says_how_to_find_the_right_one(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider()
        )
        with pytest.raises(ProviderError, match="asoode boards"):
            bridge.attach(linked_project, external_ref="does-not-exist")

    def test_needs_some_identifier(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider()
        )
        with pytest.raises(ProviderError, match="work_package_id or external_ref"):
            bridge.attach(linked_project)

    def test_one_project_attaches_to_many_boards(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider()
        )
        bridge.attach(linked_project, external_ref="app-backend", is_default=True)
        bridge.attach(linked_project, external_ref="app-worker", is_default=False)

        links = bridge.links(linked_project)
        assert len(links) == 2
        assert sum(1 for l in links if l["is_default"]) == 1, "exactly one default"
        assert get_default_project_link(linked_project)["label"] == "Backend API"

    def test_a_later_default_takes_over(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider()
        )
        bridge.attach(linked_project, external_ref="app-backend")
        bridge.attach(linked_project, external_ref="app-worker")
        assert get_default_project_link(linked_project)["remote_work_package_id"] == "wp-worker"

    def test_re_attaching_updates_rather_than_duplicating(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider()
        )
        bridge.attach(linked_project, external_ref="app-worker", label="First")
        bridge.attach(linked_project, external_ref="app-worker", label="Renamed")
        links = bridge.links(linked_project)
        assert len(links) == 1
        assert links[0]["label"] == "Renamed"

    def test_the_label_is_what_a_task_routes_by(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider()
        )
        result = bridge.attach(linked_project, external_ref="app-worker", label="worker")
        assert result["link"]["label"] == "worker"

    def test_the_state_map_comes_from_the_real_board(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider()
        )
        attached = bridge.attach(linked_project, external_ref="app-worker")
        columns = {item["title"]: item["id"] for item in attached["lists"]}
        assert attached["link"]["state_list_map"]["done"] == columns["Done"]

    def test_boards_lists_what_can_be_attached(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider()
        )
        refs = {b["external_ref"] for b in bridge.boards()}
        assert refs == {"app-worker", "app-backend"}


class TestBackfillIsOfferedNotAutomatic:
    """Linking a project with a long history would otherwise flood a board
    someone just created, and there is no bulk undo on the far side."""

    def test_attach_reports_what_is_not_mirrored_yet(self, linked_project):
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider(),
            outbox_repo=container.outbox_repo,
        )
        for i in range(3):
            container.task_service.create(
                CreateTaskRequest(project=linked_project, title=f"Existing {i}"))

        result = bridge.attach(linked_project, external_ref="app-worker")
        assert result["backfill_available"] == 3
        assert "Re-run with backfill=True" in result["backfill_hint"]

    def test_nothing_is_sent_without_being_asked(self, linked_project):
        fake = _provider()
        bridge = TaskBridge(
            container.project_service, container.task_service, fake,
            outbox_repo=container.outbox_repo,
        )
        container.task_service.create(
            CreateTaskRequest(project=linked_project, title="Stays put"))
        bridge.attach(linked_project, external_ref="app-worker")
        assert fake.created_tasks == [], "linking must not push on its own"

    def test_backfill_true_mirrors_them(self, linked_project):
        fake = _provider()
        bridge = TaskBridge(
            container.project_service, container.task_service, fake,
            outbox_repo=container.outbox_repo,
        )
        container.task_service.create(
            CreateTaskRequest(project=linked_project, title="Send me"))
        result = bridge.attach(linked_project, external_ref="app-worker", backfill=True)

        assert result["backfilled"]["pushed"] == 1
        assert [t["title"] for t in fake.created_tasks] == ["Send me"]

    def test_already_mirrored_tasks_are_not_counted(self, linked_project):
        """Reporting "27 not mirrored" for a project whose 27 tasks are all
        already there is worse than saying nothing."""
        fake = _provider()
        bridge = TaskBridge(
            container.project_service, container.task_service, fake,
            outbox_repo=container.outbox_repo,
        )
        container.task_service.create(
            CreateTaskRequest(project=linked_project, title="Already there"))
        bridge.attach(linked_project, external_ref="app-worker", backfill=True)

        again = bridge.attach(linked_project, external_ref="app-worker")
        assert again["backfill_available"] == 0
        assert again["backfill_hint"] is None


class TestPushRecordsTheMapping:
    def test_a_pushed_task_is_identifiable_afterwards(self, linked_project):
        """push() used to create remote tasks without storing the mapping, so a
        pushed task looked unmirrored to the backfill count and NEW to reconcile
        - which is how 54 duplicates were created."""
        bridge = TaskBridge(
            container.project_service, container.task_service, _provider(),
            outbox_repo=container.outbox_repo,
        )
        bridge.bootstrap(linked_project)
        task = container.task_service.create(
            CreateTaskRequest(project=linked_project, title="Pushed"))
        bridge.push(linked_project)

        link = get_default_project_link(linked_project)
        assert container.outbox_repo.remote_id(linked_project, task.id, link["id"])
