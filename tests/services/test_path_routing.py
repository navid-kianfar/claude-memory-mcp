"""A task lands on the board that owns the path its work touches.

The user's case: one repo bound to several work packages, each owning a
subtree - and every task piling onto the default board anyway, because nothing
read `match_paths` and the planner dropped `target`. These tests pin the rules
as a caller sees them: what a create/update/plan answers, where the task routes,
what is refused and what that refusal leaves behind.

`test_task_routing.py` pins the two routing rules that existed before paths and
must stay unchanged; this file is everything paths added on top.
"""

import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memory_mcp.container import container
from memory_mcp.db.registry import (
    get_project_links,
    set_credential,
    upsert_project_link,
)
from memory_mcp.exceptions import MemoryMCPError, ValidationError
from memory_mcp.models import CreateTaskRequest, UpdateTaskRequest
from memory_mcp.providers import ProviderError
from memory_mcp.services.task_service import MirroredRerouteError
from memory_mcp.sync_cli import _MANIFEST_LINK_KEYS, _manifest_links
from tests.providers.fakes import FakeProvider

SLUG = "path-routing-test"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "monorepo"
    root.mkdir()
    container.project_service.init_project(SLUG, "Path Routing Test")
    container.project_repo.update_project_path(SLUG, str(root))
    return root


@pytest.fixture
def project(repo):
    return SLUG


def _link(slug, wp, label, *, match_paths=None, is_default=False):
    return upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id=wp, label=label, is_default=is_default,
        default_list_id="l-todo", state_list_map={"todo": "l-todo"},
        match_paths=match_paths,
    )


@pytest.fixture
def boards(project):
    """A monorepo: a default board, an api board and a web board."""
    return {
        "main": _link(project, "wp-main", "main", is_default=True),
        "api": _link(project, "wp-api", "api", match_paths=["apps/api"]),
        "web": _link(project, "wp-web", "web", match_paths=["apps/web"]),
    }


def _create(project, **fields):
    return container.task_service.create_routed(
        CreateTaskRequest(project=project, title=fields.pop("title", "T"), **fields)
    )


def _count(project):
    return container.task_service.list_tasks(project, limit=100).total


class TestAPathPicksTheBoard:
    def test_each_subtree_lands_on_the_board_that_owns_it(self, project, boards):
        api_task, api_routing = _create(project, path="apps/api/tests/test_x.py")
        web_task, web_routing = _create(project, path="apps/web/src/App.tsx")

        assert api_task.link_id == boards["api"]["id"]
        assert container.task_bridge.route(project, api_task)["remote_work_package_id"] == "wp-api"
        assert api_routing == {
            "path": "apps/api/tests/test_x.py",
            "normalised": "apps/api/tests/test_x.py",
            "matched": True, "matched_prefix": "apps/api",
            "reason": "longest match_paths prefix",
            "link_id": boards["api"]["id"], "board": "api",
        }
        assert web_task.link_id == boards["web"]["id"]
        assert web_routing["board"] == "web"

    def test_the_longest_prefix_wins(self, project, boards):
        deep = _link(project, "wp-internal", "internal", match_paths=["apps/api/internal"])
        task, routing = _create(project, path="apps/api/internal/x.py")
        assert task.link_id == deep["id"]
        assert routing["matched_prefix"] == "apps/api/internal"

    def test_a_prefix_matches_whole_segments_only(self, project, boards):
        task, routing = _create(project, path="apps/api-old/x.py")
        assert routing["matched"] is False
        assert task.link_id is None

    def test_matching_is_case_sensitive(self, project, boards):
        _, routing = _create(project, path="Apps/API/x.py")
        assert routing["matched"] is False

    @pytest.mark.parametrize("path", [
        "apps/api", "apps/api/", "./apps/api/tests", "apps\\api\\tests\\x.py",
        "apps/web/../api/x.py",
    ])
    def test_a_file_a_directory_and_their_spellings_route_alike(self, project, boards, path):
        task, _ = _create(project, path=path)
        assert task.link_id == boards["api"]["id"]

    def test_an_absolute_path_inside_the_folder_is_made_relative(self, project, boards, repo):
        task, routing = _create(project, path=str(repo / "apps" / "web" / "x.tsx"))
        assert task.link_id == boards["web"]["id"]
        assert routing["normalised"] == "apps/web/x.tsx"

    def test_a_worktree_path_routes_like_the_main_checkout(self, project, boards, repo):
        inside = repo / ".claude" / "worktrees" / "agent-1" / "apps" / "api" / "x.py"
        task, routing = _create(project, path=str(inside))
        assert task.link_id == boards["api"]["id"]
        assert routing["normalised"] == "apps/api/x.py"

    def test_a_trailing_glob_is_stored_as_its_prefix_and_matches(self, project):
        _link(project, "wp-main", "main", is_default=True)
        link = _link(project, "wp-be", "backend", match_paths=["apps/backend/**"])
        assert link["match_paths"] == ["apps/backend"]
        task, _ = _create(project, path="apps/backend/src/main.py")
        assert task.link_id == link["id"]


class TestAFallbackIsVisible:
    def test_nothing_matching_uses_the_default_and_says_so(self, project, boards):
        task, routing = _create(project, path="src/memory_mcp/server.py")

        assert task.link_id is None, "an unmatched path follows the default, like no path"
        assert container.task_bridge.route(project, task)["remote_work_package_id"] == "wp-main"
        assert routing["matched"] is False
        assert routing["board"] == "main", "board names where the task actually goes"
        assert routing["link_id"] == boards["main"]["id"]
        assert "default" in routing["reason"]
        assert routing["candidates"] == [
            {"link_id": boards["main"]["id"], "board": "main", "match_paths": [], "is_default": True},
            {"link_id": boards["api"]["id"], "board": "api", "match_paths": ["apps/api"], "is_default": False},
            {"link_id": boards["web"]["id"], "board": "web", "match_paths": ["apps/web"], "is_default": False},
        ]

    def test_no_path_on_a_multi_board_project_still_reports_the_default(self, project, boards):
        task, routing = _create(project)
        assert task.link_id is None
        assert routing["matched"] is False and routing["path"] is None
        assert routing["board"] == "main"
        assert "path=" in routing["reason"]

    def test_no_path_on_a_single_board_project_reports_nothing(self, project):
        _link(project, "wp-main", "main", is_default=True)
        _, routing = _create(project)
        assert routing is None

    def test_an_unlinked_project_routes_nowhere_and_never_raises(self, project):
        task, routing = _create(project, path="../somewhere/else")
        assert task.link_id is None
        assert routing["matched"] is False and routing["link_id"] is None
        assert routing["board"] is None

    def test_the_decision_is_recorded_on_the_task(self, project, boards):
        task, _ = _create(project, path="apps/web/x.tsx")
        entries = [e for e in container.task_service.activity(project, task.id)
                   if e.operation == "task_route"]
        assert len(entries) == 1
        assert entries[0].details["matched_prefix"] == "apps/web"


class TestRefusalsCreateNothing:
    def test_an_absolute_path_outside_the_project_is_refused_naming_the_root(
        self, project, boards, repo, tmp_path,
    ):
        with pytest.raises(ProviderError) as e:
            _create(project, path=str(tmp_path / "other-repo" / "apps" / "api"))
        assert str(repo) in str(e.value)
        assert _count(project) == 0

    def test_a_relative_path_leaving_the_root_is_refused(self, project, boards):
        with pytest.raises(ProviderError, match="leaves the project root"):
            _create(project, path="../other/apps/api")
        assert _count(project) == 0

    def test_an_absolute_path_with_no_folder_names_memory_link_folder(self, tmp_path):
        slug = "no-folder-test"
        container.project_service.init_project(slug, "No Folder")
        _link(slug, "wp-main", "main", is_default=True)
        with pytest.raises(ProviderError, match="memory_link_folder"):
            _create(slug, path="/abs/apps/api")
        assert _count(slug) == 0

    def test_two_boards_claiming_a_path_equally_is_refused_naming_both(self, project, boards):
        _link(project, "wp-api-2", "api-two", match_paths=["apps/api"])
        with pytest.raises(ProviderError) as e:
            _create(project, path="apps/api/x.py")
        assert "'api'" in str(e.value) and "'api-two'" in str(e.value)
        assert _count(project) == 0

    def test_a_path_and_a_target_that_disagree_are_refused(self, project, boards):
        with pytest.raises(ProviderError, match="belongs to board 'api'"):
            _create(project, path="apps/api/x.py", target="web")
        assert _count(project) == 0

    def test_no_match_and_no_default_is_refused(self, project):
        _link(project, "wp-api", "api", match_paths=["apps/api"])
        _link(project, "wp-web", "web", match_paths=["apps/web"])
        with pytest.raises(ProviderError, match="no default"):
            _create(project, path="docs/readme.md")
        assert _count(project) == 0

    @pytest.mark.parametrize("entry", [
        "apps/*/api", "*.py", "apps/**/test", "apps/[ab]", "/abs/path", "a/../b",
        ".", "./**",
    ])
    def test_a_pattern_the_router_cannot_honour_is_refused_at_write_time(self, project, entry):
        with pytest.raises(ValidationError) as e:
            _link(project, "wp-x", "x", match_paths=["apps/ok", entry])
        assert repr(entry) in str(e.value), "the message names the offending entry"
        assert get_project_links(project) == []


class TestPrecedence:
    def test_a_target_wins_over_a_path_that_matched_nothing(self, project, boards):
        task, routing = _create(project, path="docs/x.md", target="web")
        assert task.link_id == boards["web"]["id"]
        assert routing["matched"] is True and routing["board"] == "web"

    def test_a_target_and_an_agreeing_path(self, project, boards):
        task, routing = _create(project, path="apps/web/x.tsx", target="WEB")
        assert task.link_id == boards["web"]["id"]
        assert routing["matched_prefix"] == "apps/web"


class TestRerouting:
    def _update(self, project, task_id, **fields):
        return container.task_service.update_routed(
            UpdateTaskRequest(project=project, task_id=task_id, **fields)
        )

    def test_a_task_not_on_a_board_yet_can_be_rerouted(self, project, boards):
        task, _ = _create(project)
        updated, changed, routing = self._update(project, task.id, path="apps/web/x.tsx")
        assert updated.link_id == boards["web"]["id"]
        assert "link_id" in changed
        assert routing["board"] == "web"

    def test_rerouting_a_mirrored_task_is_refused_naming_its_board(self, project, boards):
        task, _ = _create(project, title="Already mirrored")
        container.outbox_repo.remember(project, task.id, boards["main"]["id"], "r1", "todo")

        with pytest.raises(MirroredRerouteError, match="board 'main'"):
            self._update(project, task.id, target="web", title="and a rename")

        after = container.task_service.get(project, task.id)
        assert after.link_id is None
        assert after.title == "Already mirrored", "a refused update writes nothing"

    def test_naming_the_board_a_mirrored_task_is_already_on_is_fine(self, project, boards):
        task, _ = _create(project, path="apps/api/x.py")
        container.outbox_repo.remember(project, task.id, boards["api"]["id"], "r1", "todo")
        updated, _, routing = self._update(project, task.id, target="api")
        assert updated.link_id == boards["api"]["id"]
        assert routing["board"] == "api"

    def test_an_update_path_matching_nothing_keeps_the_board(self, project, boards):
        task, _ = _create(project, path="apps/api/x.py")
        updated, changed, routing = self._update(project, task.id, path="docs/x.md")
        assert updated.link_id == boards["api"]["id"]
        assert "link_id" not in changed
        assert routing["matched"] is False and routing["board"] == "api"


class TestThePlannerRoutes:
    def test_items_route_by_their_own_path_and_the_plan_default(self, project, boards):
        result = container.task_planner.plan(project, "ui and api", [
            {"title": "Screen", "description": "the page", "path": "apps/web/src/x.tsx"},
            {"title": "Endpoint", "description": "the route"},
        ], mirror=False, path="apps/api")

        screen, endpoint = result["tasks"]
        assert screen["link_id"] == boards["web"]["id"]
        assert screen["routing"]["board"] == "web"
        assert endpoint["link_id"] == boards["api"]["id"], "the plan's path filled in"
        assert endpoint["routing"]["matched_prefix"] == "apps/api"

    def test_an_item_target_is_not_overridden_by_the_plan_path(self, project, boards):
        result = container.task_planner.plan(project, "two", [
            {"title": "A", "description": "a", "target": "web"},
            {"title": "B", "description": "b"},
        ], mirror=False, path="apps/api")
        assert result["tasks"][0]["link_id"] == boards["web"]["id"]

    def test_a_planned_target_is_forwarded(self, project, boards):
        """The reported bug: the planner dropped target, so every planned task
        landed on the default board."""
        result = container.task_planner.plan(project, "two", [
            {"title": "A", "description": "a", "target": "api"},
            {"title": "B", "description": "b", "target": "web"},
        ], mirror=False)
        assert [t["link_id"] for t in result["tasks"]] == [
            boards["api"]["id"], boards["web"]["id"],
        ]

    def test_one_refused_route_rolls_the_whole_plan_back(self, project, boards):
        from memory_mcp.services.task_planner import PlanError

        with pytest.raises(PlanError, match="ROLLED BACK"):
            container.task_planner.plan(project, "two", [
                {"title": "A", "description": "a", "path": "apps/api"},
                {"title": "B", "description": "b", "path": "../elsewhere"},
            ], mirror=False)
        assert _count(project) == 0


@pytest.fixture
def http():
    from memory_mcp.web.routes import build_routes

    with TestClient(Starlette(routes=build_routes())) as client:
        yield client


class TestTheHttpContract:
    def test_create_carries_routing(self, http, project, boards):
        res = http.post(f"/api/projects/{project}/tasks",
                        json={"title": "x", "path": "apps/web/a.tsx"})
        assert res.status_code == 200
        body = res.json()
        assert body["task"]["link_id"] == boards["web"]["id"]
        assert body["routing"]["board"] == "web"

    def test_a_refused_route_is_400_with_the_reason(self, http, project, boards):
        res = http.post(f"/api/projects/{project}/tasks",
                        json={"title": "x", "path": "apps/api/a.py", "target": "web"})
        assert res.status_code == 400
        assert "belongs to board 'api'" in res.json()["error"]

    def test_put_target_reroutes_and_a_mirrored_task_is_409(self, http, project, boards):
        free, _ = _create(project)
        res = http.put(f"/api/projects/{project}/tasks/{free.id}", json={"target": "web"})
        assert res.status_code == 200
        assert res.json()["routing"]["matched"] is True
        assert res.json()["task"]["link_id"] == boards["web"]["id"]

        held, _ = _create(project)
        container.outbox_repo.remember(project, held.id, boards["main"]["id"], "r9", "todo")
        res = http.put(f"/api/projects/{project}/tasks/{held.id}", json={"target": "web"})
        assert res.status_code == 409
        assert "'main'" in res.json()["error"]

    def test_patch_normalises_a_glob_and_refuses_a_mid_path_one(self, http, project, boards):
        url = f"/api/projects/{project}/asoode/links/{boards['web']['id']}"
        res = http.patch(url, json={"match_paths": ["apps/backend/**"]})
        assert res.status_code == 200
        assert res.json()["link"]["match_paths"] == ["apps/backend"]

        res = http.patch(url, json={"match_paths": ["apps/*/api"]})
        assert res.status_code == 400
        assert "'apps/*/api'" in res.json()["error"]
        assert get_project_links(project)[2]["match_paths"] == ["apps/backend"]

    def test_patch_null_clears_and_absent_keys_are_left_alone(self, http, project, boards):
        url = f"/api/projects/{project}/asoode/links/{boards['api']['id']}"
        res = http.patch(url, json={"label": "api-renamed"})
        assert res.json()["link"]["match_paths"] == ["apps/api"]
        res = http.patch(url, json={"match_paths": None})
        assert res.json()["link"]["match_paths"] is None
        assert res.json()["link"]["label"] == "api-renamed"

    def test_patch_promotes_one_default_and_refuses_clearing_it(self, http, project, boards):
        base = f"/api/projects/{project}/asoode/links"
        res = http.patch(f"{base}/{boards['web']['id']}", json={"is_default": True})
        assert res.status_code == 200
        assert [l["label"] for l in get_project_links(project) if l["is_default"]] == ["web"]

        res = http.patch(f"{base}/{boards['web']['id']}", json={"is_default": False})
        assert res.status_code == 400
        assert "Promote another board" in res.json()["error"]

    def test_a_link_of_another_project_is_404_with_a_json_error(self, http, project, boards):
        container.project_service.init_project("someone-else", "Someone Else")
        theirs = _link("someone-else", "wp-theirs", "theirs", is_default=True)
        res = http.patch(f"/api/projects/{project}/asoode/links/{theirs['id']}",
                         json={"match_paths": ["x"]})
        assert res.status_code == 404
        assert "error" in res.json()
        assert get_project_links("someone-else")[0]["match_paths"] is None

    def test_delete_makes_its_tasks_fall_back_to_the_default(self, http, project, boards):
        task, _ = _create(project, path="apps/web/x.tsx")
        res = http.delete(f"/api/projects/{project}/asoode/links/{boards['web']['id']}")
        assert res.status_code == 200 and res.json() == {"deleted": True}
        task = container.task_service.get(project, task.id)
        assert container.task_bridge.route(project, task)["remote_work_package_id"] == "wp-main"

    def test_unlinking_the_default_while_others_remain_is_refused(self, http, project, boards):
        res = http.delete(f"/api/projects/{project}/asoode/links/{boards['main']['id']}")
        assert res.status_code == 400
        assert len(get_project_links(project)) == 3

    def test_attaching_a_second_board_without_is_default_keeps_the_default(
        self, http, project, monkeypatch,
    ):
        fake = FakeProvider()
        fake.seed(container_id="wp-a", title="A", space_id="s1", external_ref="a")
        fake.seed(container_id="wp-b", title="B", space_id="s1", external_ref="b")
        monkeypatch.setattr(container.task_bridge, "_provider", fake)

        first = http.post(f"/api/projects/{project}/asoode/link",
                          json={"attach": True, "work_package_id": "wp-a"})
        second = http.post(f"/api/projects/{project}/asoode/link",
                           json={"attach": True, "work_package_id": "wp-b",
                                 "match_paths": ["frontend/**"]})

        assert first.json()["link"]["is_default"] is True
        assert second.json()["link"]["is_default"] is False
        assert second.json()["link"]["match_paths"] == ["frontend"]

    def test_links_carry_match_paths_and_proposals(self, http, project, boards):
        body = http.get(f"/api/projects/{project}/asoode/links").json()
        assert body["proposals"] == []
        assert [l["match_paths"] for l in body["links"]] == [None, ["apps/api"], ["apps/web"]]


def _manifest_entry(wp, label, paths, *, is_default=False):
    return {
        "provider": "asoode", "base_url": "https://api.asoode.com",
        "remote_project_id": "p1", "remote_work_package_id": wp,
        "label": label, "is_default": is_default, "match_paths": paths,
    }


class TestManifestProposals:
    def test_proposals_are_diffed_and_nothing_is_linked(self, project, boards):
        proposals = container.task_bridge.set_link_proposals(project, [
            _manifest_entry("wp-api", "api", ["apps/api/**"]),
            _manifest_entry("wp-web", "web", ["apps/web", "docs"]),
            _manifest_entry("wp-new", "new", ["tools"]),
        ])
        by_board = {p["remote_work_package_id"]: p for p in proposals}

        assert by_board["wp-api"]["status"] == "matches"
        assert by_board["wp-api"]["link_id"] == boards["api"]["id"]
        assert by_board["wp-web"]["status"] == "differs"
        assert by_board["wp-web"]["link_id"] == boards["web"]["id"], "the UI refuses one without it"
        assert by_board["wp-web"]["current_match_paths"] == ["apps/web"]
        assert by_board["wp-new"]["status"] == "unlinked"
        assert by_board["wp-new"]["link_id"] is None
        assert by_board["wp-new"]["current_match_paths"] is None
        assert len(get_project_links(project)) == 3, "a proposal never binds"

    def test_applying_a_differs_proposal_settles_it(self, project, boards):
        container.task_bridge.set_link_proposals(project, [
            _manifest_entry("wp-web", "web", ["apps/web", "docs"]),
        ])
        container.task_bridge.update_link(
            project, boards["web"]["id"], match_paths=["apps/web", "docs"],
        )
        assert container.task_bridge.link_proposals(project) == []

    def test_export_keeps_bindings_nobody_here_has_acted_on(self, project):
        """A teammate who never linked a board must not erase the committed
        bindings, and an unreviewed `differs` must not revert them."""
        _link(project, "wp-web", "web", match_paths=["apps/web"], is_default=True)
        container.task_bridge.set_link_proposals(project, [
            _manifest_entry("wp-web", "web", ["apps/web", "docs"], is_default=True),
            _manifest_entry("wp-new", "new", ["tools"]),
        ])
        exported = {l["remote_work_package_id"]: l
                    for l in _manifest_links(container.task_bridge.manifest_links(project))}
        assert exported["wp-web"]["match_paths"] == ["apps/web", "docs"]
        assert exported["wp-new"]["match_paths"] == ["tools"]

    def test_the_projection_never_carries_a_credential_or_an_id(self, project, boards):
        set_credential("https://api.asoode.com", "asoode_pat_SECRET_TOKEN")
        projected = _manifest_links(get_project_links(project))

        assert all(tuple(entry) == _MANIFEST_LINK_KEYS for entry in projected)
        text = json.dumps(projected)
        assert "SECRET_TOKEN" not in text
        assert '"id"' not in text and "state_list_map" not in text
        assert [e["match_paths"] for e in projected] == [[], ["apps/api"], ["apps/web"]]

    def test_the_projection_is_idempotent_and_drops_junk(self):
        raw = [
            {"remote_work_package_id": "wp-1", "label": "one", "is_default": "yes",
             "match_paths": ["a", 3, ""], "id": 7, "token": "x"},
            {"label": "no identity"}, "junk",
        ]
        once = _manifest_links(raw)
        assert once == _manifest_links(once)
        assert once == [{
            "provider": "asoode", "base_url": "", "remote_project_id": None,
            "remote_work_package_id": "wp-1", "label": "one", "is_default": False,
            "match_paths": ["a"],
        }]


class TestTheTools:
    def test_task_add_reports_routing(self, project, boards):
        from memory_mcp import server

        answer = server.memory_task_add(
            title="probe ui", path="apps/web/src/Integrations.tsx", project=project,
        )
        assert answer["routing"]["matched"] is True
        assert answer["routing"]["board"] == "web"
        assert answer["task"]["link_id"] == boards["web"]["id"]

    def test_task_update_refuses_moving_a_mirrored_task_as_an_error(self, project, boards):
        from memory_mcp import server

        task, _ = _create(project, path="apps/web/x.tsx")
        container.outbox_repo.remember(project, task.id, boards["web"]["id"], "r1", "todo")
        answer = server.memory_task_update(task_id=task.id, target="main", project=project)
        assert answer["type"] == "MirroredRerouteError"
        assert "'web'" in answer["error"]

    def test_task_plan_takes_a_plan_level_path(self, project, boards, monkeypatch):
        from memory_mcp import server

        # A bound project's plan drains the outbox; no test may reach asoode.
        monkeypatch.setattr(container.task_bridge, "flush", lambda slug: {})
        answer = server.memory_task_plan(
            request="two", project=project, path="apps/api",
            tasks=[{"title": "A", "description": "a", "path": "apps/web/a"},
                   {"title": "B", "description": "b"}],
        )
        assert [t["routing"]["board"] for t in answer["tasks"]] == ["web", "api"]

    def test_link_update_normalises_refuses_and_clears(self, project, boards):
        from memory_mcp import server

        web = boards["web"]["id"]
        ok = server.memory_asoode_link_update(
            link_id=web, match_paths=["frontend/**"], project=project,
        )
        assert ok["link"]["match_paths"] == ["frontend"]

        bad = server.memory_asoode_link_update(
            link_id=web, match_paths=["apps/*/x"], project=project,
        )
        assert "'apps/*/x'" in bad["error"]

        untouched = server.memory_asoode_link_update(link_id=web, label="ui", project=project)
        assert untouched["link"]["match_paths"] == ["frontend"]
        cleared = server.memory_asoode_link_update(link_id=web, match_paths=[], project=project)
        assert cleared["link"]["match_paths"] is None

    def test_links_include_proposals(self, project, boards):
        from memory_mcp import server

        container.task_bridge.set_link_proposals(project, [
            _manifest_entry("wp-web", "web", ["apps/web", "docs"]),
        ])
        answer = server.memory_asoode_links(project=project)
        assert answer["proposals"][0]["status"] == "differs"
        assert answer["proposals"][0]["link_id"] == boards["web"]["id"]


class TestTheCli:
    def test_attach_sends_match_paths_and_no_default_claim(self, project, monkeypatch):
        from memory_mcp import asoode_cli, daemon_client

        sent = {}

        def _daemon(path, method="GET", payload=None, timeout=60.0):
            sent.update(payload)
            return {"work_package": {"id": "wp-b", "title": "B", "external_ref": None},
                    "link": {"is_default": False, "match_paths": ["apps/api"]},
                    "lists": []}

        monkeypatch.setattr(daemon_client, "call", _daemon)
        asoode_cli.main(["attach", project, "--wp-id", "wp-b",
                         "--match-path", "apps/api", "--match-path", "libs/**"])

        assert sent["match_paths"] == ["apps/api", "libs/**"]
        assert sent["is_default"] is None, "no flag must not mean 'make it the default'"

    def test_the_local_fallback_forwards_them_too(self, project, monkeypatch):
        from memory_mcp import asoode_cli, daemon_client

        def _nobody(*args, **kwargs):
            raise daemon_client.DaemonUnavailable("nothing listening")

        fake = FakeProvider()
        fake.seed(container_id="wp-a", title="A", space_id="s1")
        fake.seed(container_id="wp-b", title="B", space_id="s1")
        monkeypatch.setattr(daemon_client, "call", _nobody)
        monkeypatch.setattr(container.task_bridge, "_provider", fake)

        asoode_cli.main(["attach", project, "--wp-id", "wp-a"])
        asoode_cli.main(["attach", project, "--wp-id", "wp-b", "--match-path", "frontend/**"])

        links = {l["remote_work_package_id"]: l for l in get_project_links(project)}
        assert links["wp-a"]["is_default"] is True
        assert links["wp-b"]["is_default"] is False
        assert links["wp-b"]["match_paths"] == ["frontend"]


def test_routing_refusals_are_both_provider_and_domain_errors(project, boards):
    """ProviderError keeps every existing caller working; MemoryMCPError is what
    makes the daemon answer 400 rather than 500."""
    with pytest.raises(MemoryMCPError):
        _create(project, path="../x")
