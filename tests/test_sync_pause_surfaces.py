"""Pause and resume through each surface: the MCP tools, the web API and the CLI.

The behaviour itself is tested against the bridge in
tests/services/test_sync_pause.py; these check that each surface reaches it,
passes its arguments through, and answers in the documented shape.
"""

from datetime import date, timedelta

import pytest

from memory_mcp import daemon_client, server
from memory_mcp.container import container
from memory_mcp.db.registry import get_sync_pause, upsert_project_link
from memory_mcp.models import CreateTaskRequest
from memory_mcp.web import routes
from tests.providers.fakes import FakeProvider

SYNC_KEYS = frozenset({
    "slug", "linked", "paused", "since", "until", "reason", "overdue",
    "queued_ops", "drain",
})


@pytest.fixture
def project(monkeypatch):
    slug = "pause-surface"
    container.project_service.init_project(slug, "Pause Surface")
    upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id="wp1", label="board", is_default=True,
        default_list_id="l-todo",
        state_list_map={"todo": "l-todo", "in_progress": "l-doing", "done": "l-done"},
    )
    fake = FakeProvider()
    fake.seed(container_id="wp1", title="Board", space_id="p1",
              groups=(("l-todo", "To Do"), ("l-doing", "In Progress"), ("l-done", "Done")))
    monkeypatch.setattr(container.task_bridge, "_provider", fake)
    return slug


def _in_two_weeks() -> str:
    return (date.today() + timedelta(days=14)).isoformat()


class TestTheTools:
    def test_pause_then_resume(self, project):
        paused = server.memory_asoode_pause(
            project=project, until=_in_two_weeks(), reason="local only",
        )
        assert set(paused) >= SYNC_KEYS
        assert paused["paused"] is True and paused["reason"] == "local only"

        container.task_service.create(CreateTaskRequest(project=project, title="Offline"))
        resumed = server.memory_asoode_resume(project=project)
        container.sync_pause_service.wait_for_drains(timeout=30)

        assert resumed["was_paused"] is True and resumed["paused"] is False
        assert container.sync_pause_service.status(project)["queued_ops"] == 0

    def test_a_bad_until_comes_back_as_an_error_not_a_crash(self, project):
        answer = server.memory_asoode_pause(project=project, until="soon")
        assert "ISO date" in answer["error"]

    def test_status_with_a_project_shows_its_sync(self, project):
        server.memory_asoode_pause(project=project)
        status = server.memory_asoode_status(project=project)
        assert status["sync"]["paused"] is True

    def test_status_without_a_project_lists_paused_ones(self, project):
        server.memory_asoode_pause(project=project)
        assert project in server.memory_asoode_status()["paused_projects"]

    def test_a_paused_task_reply_says_why_the_queue_grows(self, project):
        server.memory_asoode_pause(project=project)
        answer = server.memory_task_add(
            project=project, title="Offline task",
            description="A task created while board sync is paused.",
        )
        assert answer["mirror"]["paused"] is True
        assert "error" not in answer


class TestTheWebApi:
    def test_routes_are_registered(self):
        paths = {
            (r.path, tuple(sorted(r.methods or ())))
            for r in routes.build_routes() if hasattr(r, "methods")
        }
        assert ("/api/projects/{slug}/asoode/sync", ("GET", "HEAD")) in paths
        assert ("/api/projects/{slug}/asoode/pause", ("POST",)) in paths
        assert ("/api/projects/{slug}/asoode/resume", ("POST",)) in paths

    def test_pause_status_resume(self, project):
        until = _in_two_weeks()
        paused = routes._asoode_pause({"slug": project}, {"until": until, "reason": "r"}, {})
        assert paused["paused"] is True and paused["until"] == until

        status = routes._asoode_sync_status({"slug": project}, {}, {})
        assert set(status) == SYNC_KEYS

        resumed = routes._asoode_resume({"slug": project}, {}, {})
        assert resumed["was_paused"] is True
        assert set(resumed) == SYNC_KEYS | {"was_paused"}

    def test_a_non_string_until_is_refused(self, project):
        with pytest.raises(ValueError):
            routes._asoode_pause({"slug": project}, {"until": 20261204}, {})


class TestTheCli:
    def test_pause_without_a_daemon_writes_locally(self, project, monkeypatch, capsys):
        from memory_mcp import asoode_cli

        def no_daemon(*a, **k):
            raise daemon_client.DaemonUnavailable("nothing listening")

        monkeypatch.setattr(daemon_client, "call", no_daemon)
        until = _in_two_weeks()
        asoode_cli.main(["pause", project, "--until", until, "--reason", "offline"])

        assert get_sync_pause(project)["until"] == until
        out = capsys.readouterr().out
        assert "PAUSED" in out and until in out

    def test_resume_goes_through_the_daemon(self, project, monkeypatch, capsys):
        from memory_mcp import asoode_cli

        calls = []

        def daemon(path, method="GET", payload=None, timeout=60.0):
            calls.append((path, method))
            return {
                "slug": project, "linked": True, "paused": False, "since": None,
                "until": None, "reason": None, "overdue": False, "queued_ops": 40,
                "was_paused": True,
                "drain": {"state": "running", "flushed": 60, "failed": 0,
                          "remaining": 40, "imported": None, "error": None,
                          "stopped_by_pause": False},
            }

        monkeypatch.setattr(daemon_client, "call", daemon)
        asoode_cli.main(["resume", project])

        assert calls == [(f"/api/projects/{project}/asoode/resume", "POST")]
        out = capsys.readouterr().out
        assert "60 sent" in out and "40 remaining" in out
        assert "background" in out

    def test_a_refusal_from_the_daemon_is_a_message(self, project, monkeypatch, capsys):
        from memory_mcp import asoode_cli

        def refuse(*a, **k):
            raise daemon_client.DaemonError("daemon returned 400: 'until' is in the past")

        monkeypatch.setattr(daemon_client, "call", refuse)
        with pytest.raises(SystemExit):
            asoode_cli.main(["pause", project, "--until", "2020-01-01"])
        assert "in the past" in capsys.readouterr().err
