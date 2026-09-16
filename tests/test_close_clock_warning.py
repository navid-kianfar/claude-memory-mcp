"""A task closed without ever being started says so, first, in the close reply.

Measured on 2026-09-15: every local task with no time entry was closed straight
from todo. On the latest batch the five tasks had no claim and no comment of
their own and the edit gate was open for another task, so the close was the
first moment anything could see it - and the reason, nested in `time_note`, was
read past while four more were closed the same way. These tests hold the MCP
close tools to leading with a `clock_warning` in exactly that case.
"""

import pytest

from memory_mcp import server
from memory_mcp.container import container
from memory_mcp.db.connection import get_connection
from memory_mcp.models import CreateTaskRequest
from tests.services.test_task_service import _age, _session


@pytest.fixture
def project():
    slug = "t-close-warning"
    container.project_repo.register(slug, slug)
    get_connection(slug).close()
    return slug


def _add(project, title):
    return container.task_service.create(CreateTaskRequest(project=project, title=title))


class TestTheCloseReplyLeadsWithTheWarning:
    def test_done_on_a_never_started_task_warns_and_names_the_estimate(self, project):
        task = _add(project, "Planned and never started")
        session = _session(container, project, "s-agent")
        _age(project, minutes=20)

        answer = server.memory_task_done(task.id, project=project, session_id=session)

        assert next(iter(answer)) == "clock_warning", "the warning comes first"
        assert "CLOSED WITHOUT BEING STARTED" in answer["clock_warning"]
        assert "ESTIMATE from session-start" in answer["clock_warning"]
        assert "memory_task_start" in answer["clock_warning"]
        assert answer["time_note"]["minutes"] == 20

    def test_the_passed_session_is_the_one_credited(self, project):
        """A dispatched agent shares the lead's connection; its own session_id
        must win over the one the connection remembers."""
        task = _add(project, "An agent's task")
        session = _session(container, project, "s-dispatched")
        _age(project, minutes=5)

        server.memory_task_done(task.id, project=project, session_id=session)

        entries = container.task_repo.entries_for(project, task.id)
        assert [entry.session_id for entry in entries] == [session]

    def test_update_to_done_names_the_task_whose_clock_ran(self, project):
        running = _add(project, "The task that was started")
        task = _add(project, "Worked under the other clock")
        session = _session(container, project, "s-lead")
        container.task_service.start(project, running.id, session)
        _age(project, minutes=30)

        answer = server.memory_task_update(
            task.id, state="done", project=project, session_id=session,
        )

        assert next(iter(answer)) == "clock_warning"
        assert "The task that was started" in answer["clock_warning"]
        assert running.id in answer["clock_warning"]
        assert container.task_repo.entries_for(project, task.id) == []

    def test_with_no_evidence_it_says_nothing_was_recorded(self, project):
        task = _add(project, "No claim, no comment, no session")

        answer = server.memory_task_update(task.id, state="done", project=project)

        assert "No time was recorded" in answer["clock_warning"]

    def test_a_started_task_closes_quietly(self, project):
        task = _add(project, "Started properly")
        session = _session(container, project, "s-good")
        container.task_service.start(project, task.id, session)

        answer = server.memory_task_done(task.id, project=project, session_id=session)

        assert "clock_warning" not in answer
        assert "error" not in answer

    def test_a_task_recovered_from_its_state_history_closes_quietly(self, project):
        """It WAS started; only its clock was lost. Not the pattern warned about."""
        task = _add(project, "Moved to in progress, clock lost")
        server.memory_task_update(task.id, state="in_progress", project=project)
        with get_connection(project) as conn:
            conn.execute("DELETE FROM task_time_entries WHERE task_id = ?", [task.id])
        _age(project, minutes=10)

        answer = server.memory_task_done(task.id, project=project)

        assert "clock_warning" not in answer
        assert answer["time_note"]["from"] == "state-history"
