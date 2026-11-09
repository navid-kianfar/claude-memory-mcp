"""The duplicate warning on task writes: similar_tasks.

The lead creates tasks before every dispatch, so a lead that never looks at the
board re-creates work that is already on it. The answer must say so - and must
never refuse, because "similar" is a guess and the lead decides.
"""

import pytest

from memory_mcp import server
from memory_mcp.container import container
from memory_mcp.db.connection import get_connection
from memory_mcp.models import CreateTaskRequest, TaskState, UpdateTaskRequest


@pytest.fixture
def project():
    slug = "t-similar-tasks"
    container.project_repo.register(slug, slug)
    get_connection(slug).close()
    return slug


def _add(project, title, **kw):
    return container.task_service.create(
        CreateTaskRequest(project=project, title=title, **kw),
    )


class TestSimilarTasks:
    def test_a_reworded_copy_is_flagged_and_still_created(self, project):
        existing = _add(project, "Fix the outbox sweep reopening project databases")

        answer = server.memory_task_add(
            title="Outbox sweep reopens every project database",
            description="dup?", project=project,
        )

        assert answer["status"] == "ok"
        hits = answer["similar_tasks"][answer["task"]["id"]]
        assert hits[0]["id"] == existing.id
        assert "similar_note" in answer

    def test_unrelated_work_is_not_flagged(self, project):
        _add(project, "Fix the outbox sweep reopening project databases")

        answer = server.memory_task_add(
            title="Add dark mode to the management UI", project=project,
        )

        assert "similar_tasks" not in answer

    def test_a_subtask_is_not_a_duplicate_of_its_parent(self, project):
        parent = _add(project, "Codex notify chaining and payload")

        answer = server.memory_task_add(
            title="Codex notify payload chaining", parent_id=parent.id,
            project=project,
        )

        assert "similar_tasks" not in answer

    def test_a_plan_checks_each_task_but_not_against_itself(self, project):
        existing = _add(project, "Validate a task role against installed agents")

        answer = server.memory_task_plan(
            request="r",
            tasks=[
                {"title": "Validate task role against the installed agents",
                 "description": "d"},
                {"title": "Write the release notes for v1", "description": "d"},
            ],
            project=project,
        )

        similar = answer["similar_tasks"]
        assert list(similar) == [answer["tasks"][0]["id"]]
        assert similar[answer["tasks"][0]["id"]][0]["id"] == existing.id

    def test_cancelled_work_is_not_offered(self, project):
        old = _add(project, "Fix the outbox sweep reopening project databases")
        container.task_service.update(UpdateTaskRequest(
            project=project, task_id=old.id, state=TaskState.CANCELLED,
        ))

        answer = server.memory_task_add(
            title="Outbox sweep reopens every project database", project=project,
        )

        assert "similar_tasks" not in answer


class TestLeadBriefingReachesEveryHarness:
    """OpenCode and Codex have no hook that reaches the model; session_start is
    the one call every harness makes, so the lead's contract rides it."""

    def test_the_lead_gets_the_team_contract(self, project):
        answer = server.memory_session_start(project=project)

        assert "TASKS FIRST" in answer["team_instructions"]
        assert "FULL SUITE ONCE PER MERGE POINT" in answer["team_instructions"]
        server.memory_session_end(answer["session_id"], "t", project=project)

    def test_a_dispatched_agent_does_not(self, project):
        answer = server.memory_session_start(project=project, agent="python")

        assert "team_instructions" not in answer
        server.memory_session_end(answer["session_id"], "t", project=project)
