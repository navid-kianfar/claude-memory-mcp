"""The PreToolUse gate: no work on a bound project without a task in progress.

This is the ONLY hook in the project that can refuse anything. The other three
print into the model's context and hope - and a rule saying "put the work on
the board first" was followed roughly 70% of the time, which is what this
exists to fix. Text can remind; only an exit status can require.

Every test here is really about the same property: it blocks the one case it
must, and OPENS on everything else. A gate that stops a person editing a file
because a board is unreachable would be a far worse bug than the one it fixes.
"""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from memory_mcp.container import container
from memory_mcp.db.connection import get_connection
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.models import CreateTaskRequest, TaskState, UpdateTaskRequest
from memory_mcp.web import routes

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "require-task.sh"


@pytest.fixture
def project(tmp_path):
    slug = "t-gate"
    container.project_repo.register(slug, slug)
    get_connection(slug).close()
    return slug


class _Req:
    """The slice of a Starlette request the gate actually reads."""

    def __init__(self, cwd):
        self.query_params = {"cwd": cwd}
        self.headers = {}
        self.cookies = {}
        self.scope = {"type": "http", "headers": []}


def _gate(cwd):
    response = asyncio.run(routes._hook_gate(_Req(cwd)))
    return json.loads(response.body)


def _bind(slug):
    upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id="wp1",
    )


class TestTheGateDecision:
    def test_a_directory_that_is_not_a_project_is_allowed(self):
        assert _gate("/tmp/definitely-not-a-memory-project")["allow"] is True

    def test_an_unbound_project_is_allowed(self, project, monkeypatch):
        """No board means no queue to be out of step with."""
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        answer = _gate("/anywhere")
        assert answer["allow"] is True
        assert "not bound" in answer["reason"]

    def test_a_bound_project_with_no_task_in_progress_is_BLOCKED(
        self, project, monkeypatch,
    ):
        """The one case the gate exists for."""
        _bind(project)
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        container.task_service.create(
            CreateTaskRequest(project=project, title="Queued, not started")
        )
        answer = _gate("/anywhere")
        assert answer["allow"] is False
        # The reason has to name the way out, or the model cannot act on it.
        assert "memory_task_start" in answer["reason"]
        assert "memory_task_plan" in answer["reason"]

    def test_starting_a_task_opens_the_gate(self, project, monkeypatch):
        _bind(project)
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        task = container.task_service.create(
            CreateTaskRequest(project=project, title="Real work")
        )
        assert _gate("/anywhere")["allow"] is False
        container.task_service.start(project, task.id)
        answer = _gate("/anywhere")
        assert answer["allow"] is True
        assert "Real work" in answer["reason"]

    def test_finishing_the_task_closes_it_again(self, project, monkeypatch):
        _bind(project)
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        task = container.task_service.create(
            CreateTaskRequest(project=project, title="Done soon")
        )
        container.task_service.start(project, task.id)
        container.task_service.done(project, task.id)
        assert _gate("/anywhere")["allow"] is False

    def test_a_paused_task_does_not_hold_the_gate_open(self, project, monkeypatch):
        """Pausing is stopping work; the gate must reflect that."""
        _bind(project)
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        task = container.task_service.create(
            CreateTaskRequest(project=project, title="Paused")
        )
        container.task_service.start(project, task.id)
        container.task_service.update(
            UpdateTaskRequest(
                project=project, task_id=task.id, state=TaskState.PAUSED,
            )
        )
        assert _gate("/anywhere")["allow"] is False

    def test_an_exception_inside_the_gate_fails_OPEN(self, project, monkeypatch):
        """The property that makes this safe to install by default."""
        def _boom(cwd):
            raise RuntimeError("registry is on fire")

        monkeypatch.setattr("memory_mcp.context.detect_project_from_cwd", _boom)
        answer = _gate("/anywhere")
        assert answer["allow"] is True
        assert "failing open" in answer["reason"]


@pytest.mark.skipif(not HOOK.exists(), reason="hook script not present")
class TestTheHookScript:
    """The script's own contract: exit 2 blocks, exit 0 allows."""

    def _run(self, payload, env=None):
        return subprocess.run(
            ["bash", str(HOOK)], input=json.dumps(payload), text=True,
            capture_output=True, timeout=30, env=env,
        )

    def test_an_unreachable_daemon_allows(self):
        """A dead daemon must never stop someone editing a file."""
        import os

        env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": "9"}  # nothing listens on 9
        result = self._run({"cwd": str(HOOK.parent)}, env=env)
        assert result.returncode == 0, result.stderr

    def test_the_off_switch_allows(self):
        import os

        env = {**os.environ, "MEMORY_MCP_NO_GATE": "1"}
        result = self._run({"cwd": str(HOOK.parent)}, env=env)
        assert result.returncode == 0

    def test_no_cwd_allows(self):
        assert self._run({}).returncode == 0
