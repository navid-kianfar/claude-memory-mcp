"""memory_task_bulk: one action over many tasks, local first, dry run by default.

Each id goes through the method its single-task tool uses, so these assert
what a caller observes - the dry run writes nothing, a confirmed run changes
exactly the ids named, a bad id is reported and the rest still go - not how
the loop is built.
"""

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from memory_mcp.container import container
from memory_mcp.models import CreateTaskRequest, TaskFilter, TaskState

SLUG = "t-task-bulk"


@pytest.fixture
def project():
    container.project_service.init_project(SLUG, "Task Bulk")
    return SLUG


def _make(project, title="T", **kw):
    return container.task_service.create(CreateTaskRequest(project=project, title=title, **kw))


def _bulk(project, ids, action, **kw):
    return container.task_service.bulk(project, ids, action, **kw)


def _visible(project):
    listing = container.task_service.list_tasks(project, TaskFilter(), limit=100)
    return {t.id for t in listing.tasks}


class TestTheDryRun:
    def test_nothing_is_written_without_confirm(self, project):
        a, b = _make(project, "a"), _make(project, "b")
        depth = container.outbox_repo.depth(project)

        answer = _bulk(project, [a.id, b.id], "archive")

        assert answer["dry_run"] is True
        assert {c["id"] for c in answer["would_change"]} == {a.id, b.id}
        assert _visible(project) == {a.id, b.id}
        assert container.outbox_repo.depth(project) == depth, "a preview queues no mirror"

    def test_it_separates_what_would_change_from_what_already_is(self, project):
        a, b = _make(project, "a"), _make(project, "b")
        container.task_service.set_state(project, b.id, TaskState.BLOCKED)

        answer = _bulk(project, [a.id, b.id], "set_state", state="blocked")

        assert [c["id"] for c in answer["would_change"]] == [a.id]
        assert answer["would_change"][0]["to"] == "blocked"
        assert answer["unchanged"] == [b.id]

    def test_an_unknown_id_is_reported_not_raised(self, project):
        a = _make(project)
        answer = _bulk(project, [a.id, "no-such-task"], "delete")
        assert "no-such-task" in answer["failed"]
        assert [c["id"] for c in answer["would_change"]] == [a.id]

    def test_a_delete_preview_says_how_many_sub_tasks_it_promotes(self, project):
        parent = _make(project, "parent")
        _make(project, "child", parent_id=parent.id)
        answer = _bulk(project, [parent.id], "delete")
        assert answer["would_change"][0]["sub_tasks_promoted"] == 1


class TestTheConfirmedRun:
    def test_archive_takes_exactly_the_ids_named(self, project):
        keep, a, b = _make(project, "keep"), _make(project, "a"), _make(project, "b")

        answer = _bulk(project, [a.id, b.id], "archive", confirm=True)

        assert answer["succeeded"] == [a.id, b.id] and answer["failed"] == {}
        assert _visible(project) == {keep.id}

    def test_unarchive_brings_them_back_and_queues_the_mirror(self, project):
        a = _make(project)
        container.task_service.archive(project, a.id)

        _bulk(project, [a.id], "unarchive", confirm=True)

        assert a.id in _visible(project)
        rows = [r for r in container.outbox_repo.pending(project, 50) if r["op"] == "archive"]
        assert rows[-1]["payload"] == {"archived": False}

    def test_set_state_moves_every_task(self, project):
        a, b = _make(project, "a"), _make(project, "b")
        _bulk(project, [a.id, b.id], "set_state", state="done", confirm=True,
              session_id="s-bulk")
        assert {container.task_service.get(project, t).state for t in (a.id, b.id)} == {
            TaskState.DONE}

    def test_one_bad_id_never_undoes_the_rest(self, project):
        a, b = _make(project, "a"), _make(project, "b")

        answer = _bulk(project, [a.id, "missing", b.id], "delete", confirm=True)

        assert answer["succeeded"] == [a.id, b.id]
        assert list(answer["failed"]) == ["missing"]
        assert _visible(project) == set()

    def test_deleting_a_parent_and_its_child_together_works_in_either_order(self, project):
        parent = _make(project, "parent")
        child = _make(project, "child", parent_id=parent.id)

        answer = _bulk(project, [child.id, parent.id], "delete", confirm=True)

        assert answer["failed"] == {}
        assert _visible(project) == set()


class TestTheArguments:
    @pytest.mark.parametrize("call", [
        lambda p, ids: _bulk(p, ids, "explode"),
        lambda p, ids: _bulk(p, ids, "set_state"),
        lambda p, ids: _bulk(p, ids, "set_state", state="finished"),
        lambda p, ids: _bulk(p, ids, "move"),
        lambda p, ids: _bulk(p, [], "archive"),
        lambda p, ids: _bulk(p, [f"id-{n}" for n in range(501)], "archive"),
    ])
    def test_a_malformed_call_is_refused_before_anything_runs(self, project, call):
        from memory_mcp.exceptions import ValidationError

        a = _make(project)
        with pytest.raises(ValidationError):
            call(project, [a.id])
        assert _visible(project) == {a.id}


class TestTheTool:
    def test_it_requires_a_session_to_change_state(self, project):
        from memory_mcp import server

        a = _make(project)
        answer = server.memory_task_bulk(
            task_ids=[a.id], action="set_state", state="done", project=project, confirm=True,
        )
        assert "session_id" in answer["error"]
        assert container.task_service.get(project, a.id).state == TaskState.TODO

    def test_a_dry_run_needs_no_session(self, project):
        from memory_mcp import server

        a = _make(project)
        answer = server.memory_task_bulk(
            task_ids=[a.id], action="set_state", state="done", project=project,
        )
        assert answer["dry_run"] is True

    def test_it_archives_through_the_service(self, project):
        from memory_mcp import server

        a = _make(project)
        answer = server.memory_task_bulk(
            task_ids=[a.id], action="archive", project=project, confirm=True,
        )
        assert answer["succeeded"] == [a.id]
        assert _visible(project) == set()


class TestTheHttpRoute:
    @pytest.fixture
    def http(self):
        from memory_mcp.web.routes import build_routes

        with TestClient(Starlette(routes=build_routes())) as client:
            yield client

    def test_dry_run_then_confirm(self, http, project):
        a = _make(project)
        url = f"/api/projects/{project}/tasks/bulk"

        preview = http.post(url, json={"ids": [a.id], "action": "archive"})
        assert preview.status_code == 200 and preview.json()["dry_run"] is True
        assert _visible(project) == {a.id}

        done = http.post(url, json={"ids": [a.id], "action": "archive", "confirm": True})
        assert done.status_code == 200 and done.json()["succeeded"] == [a.id]
        assert _visible(project) == set()

    @pytest.mark.parametrize("body", [
        {"action": "archive"},
        {"ids": [], "action": "archive"},
        {"ids": [1], "action": "archive"},
        {"ids": ["x"]},
        {"ids": ["x"], "action": "explode"},
    ])
    def test_a_bad_body_is_400(self, http, project, body):
        res = http.post(f"/api/projects/{project}/tasks/bulk", json=body)
        assert res.status_code == 400
        assert "Traceback" not in res.text
