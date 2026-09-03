"""A bound project's session start hands over a 'work this' brief, not a 'wait' one.

The point of these: the loop must not have to be re-told per project. Being bound
to an asoode board is the whole opt-in, and nothing about an integration failure
may stop a session from starting.
"""

import pytest

from memory_mcp.asoode_client import AsoodeError
from memory_mcp.container import container
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.models import CreateTaskRequest
from memory_mcp.services.asoode_bridge import AsoodeBridge
from memory_mcp.services.session_service import SessionService

BOARD = {
    "id": "wp-1", "title": "Board",
    "lists": [
        {"id": "l-todo", "title": "To Do", "tasks": [
            {"id": "r1", "title": "Local one", "state": 1},
            {"id": "r2", "title": "Added in asoode", "state": 1},
        ]},
        {"id": "l-done", "title": "Done", "tasks": [
            {"id": "r3", "title": "Finished", "state": 3},
        ]},
    ],
}


class Client:
    def __init__(self, board=BOARD, error=None):
        self._board, self._error = board, error

    def fetch_work_package(self, package_id):
        if self._error:
            raise self._error
        return self._board


@pytest.fixture
def project():
    slug = "brief-test"
    container.project_service.init_project(slug, "Brief Test")
    container.task_service.create(CreateTaskRequest(project=slug, title="Local one"))
    return slug


def _service(slug, client=None, bind=True):
    if bind:
        upsert_project_link(
            slug, base_url="https://api.asoode.com", remote_project_id="p1",
            remote_work_package_id="wp-1",
        )
    bridge = AsoodeBridge(
        container.project_service, container.task_service, client or Client()
    )
    return SessionService(
        container.session_repo, container.memory_repo, container.project_repo,
        container.rules_service, container.task_service, bridge,
    )


class TestUnbound:
    def test_keeps_the_capture_contract(self, project):
        service = _service(project, bind=False)
        ctx = service.start(project)
        assert ctx.asoode is None
        assert "NOT instructions" in ctx.task_instructions
        assert "Do NOT start any of them" in ctx.task_instructions

    def test_a_session_with_no_bridge_at_all_is_unchanged(self, project):
        service = SessionService(
            container.session_repo, container.memory_repo, container.project_repo,
            container.rules_service, container.task_service,
        )
        ctx = service.start(project)
        assert ctx.asoode is None
        assert "NOT instructions" in ctx.task_instructions


class TestBound:
    def test_the_brief_says_to_work_the_queue(self, project):
        ctx = _service(project).start(project)
        assert ctx.asoode["bound"] and ctx.asoode["reachable"]
        assert "THIS BOARD IS THE WORK QUEUE" in ctx.task_instructions
        assert "do not ask permission to begin" in ctx.task_instructions

    def test_it_does_not_carry_the_do_not_start_contract(self, project):
        """The two briefs are contradictory; a session must never get both."""
        ctx = _service(project).start(project)
        assert "Do NOT start any of them" not in ctx.task_instructions

    def test_blocked_states_are_still_excluded_from_auto_start(self, project):
        ctx = _service(project).start(project)
        assert "blocked, blocker, paused or cancelled" in ctx.task_instructions

    def test_the_board_url_is_in_the_brief(self, project):
        ctx = _service(project).start(project)
        assert "https://app.asoode.com/projects/p1" in ctx.task_instructions

    def test_tasks_added_in_asoode_are_surfaced(self, project):
        ctx = _service(project).start(project)
        assert ctx.asoode["remote_only"] == ["Added in asoode"]
        assert "Added in asoode" in ctx.task_instructions

    def test_closed_remote_tasks_are_not_counted_as_waiting(self, project):
        ctx = _service(project).start(project)
        titles = [t["title"] for t in ctx.asoode["remote_open"]]
        assert "Finished" not in titles, "state 3 is Done"
        assert len(titles) == 2


class TestFailureNeverBlocksASession:
    def test_unreachable_board_still_starts_the_session(self, project):
        service = _service(project, Client(error=AsoodeError("connection refused")))
        ctx = service.start(project)
        assert ctx.session_id
        assert ctx.asoode["reachable"] is False
        assert "could not be reached" in ctx.task_instructions
        assert "Do not treat this as a reason to stop" in ctx.task_instructions

    def test_an_unexpected_exception_is_contained(self, project):
        class Exploding:
            def fetch_work_package(self, package_id):
                raise RuntimeError("something entirely unexpected")

        ctx = _service(project, Exploding()).start(project)
        assert ctx.session_id
        assert ctx.asoode["reachable"] is False
        assert "RuntimeError" in ctx.asoode["error"]

    def test_rules_and_context_still_load_when_asoode_is_down(self, project):
        service = _service(project, Client(error=AsoodeError("down")))
        ctx = service.start(project)
        assert ctx.queued_tasks, "the local queue is the same queue, mirrored"


class TestQueueStatus:
    def test_returns_none_for_an_unbound_project(self, project):
        bridge = AsoodeBridge(
            container.project_service, container.task_service, Client()
        )
        assert bridge.queue_status(project) is None

    def test_reports_rather_than_raising_when_the_pat_is_rejected(self, project):
        upsert_project_link(
            project, base_url="https://api.asoode.com", remote_project_id="p1",
            remote_work_package_id="wp-1",
        )
        bridge = AsoodeBridge(
            container.project_service, container.task_service,
            Client(error=AsoodeError("asoode rejected the PAT (401)")),
        )
        status = bridge.queue_status(project)
        assert status["bound"] is True
        assert status["reachable"] is False
        assert "401" in status["error"]
