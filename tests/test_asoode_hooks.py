"""asoode rides the hook path, so it survives compaction and is never re-told.

SERVER_INSTRUCTIONS is read once at connect. These blocks are re-injected every
turn (rules) and at session start (intro), which is why a long session cannot
forget what asoode is or that this project's queue lives on a board.
"""

import pytest

from memory_mcp import enforcement
from memory_mcp.container import container
from memory_mcp.db.registry import upsert_project_link


@pytest.fixture
def project():
    slug = "hook-test"
    container.project_service.init_project(slug, "Hook Test")
    return slug


def _bind(slug):
    upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id="wp1",
    )


class TestPerTurnLine:
    def test_a_bound_project_gets_the_line_every_turn(self, project):
        _bind(project)
        line = enforcement.asoode_line(project)
        assert "IS this project's work queue" in line
        assert "work it one task at a time" in line

    def test_an_unbound_project_gets_nothing(self, project):
        assert enforcement.asoode_line(project) == ""

    def test_it_stays_one_line(self, project):
        """A block repeated on every prompt is cost and noise."""
        _bind(project)
        assert "\n" not in enforcement.asoode_line(project)

    def test_it_carries_the_do_not_auto_start_exception(self, project):
        _bind(project)
        assert "blocked/blocker/paused/cancelled" in enforcement.asoode_line(project)

    def test_a_bound_project_with_no_rules_still_gets_it(self, project):
        """The workflow must not depend on the project happening to have rules."""
        _bind(project)
        block = enforcement.rules_text_for_project(project)
        assert "asoode" in block

    def test_it_survives_a_broken_registry_read(self, project, monkeypatch):
        monkeypatch.setattr(
            "memory_mcp.db.registry.get_default_project_link",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("registry gone")),
        )
        assert enforcement.asoode_line(project) == ""


class TestIntro:
    def test_bound_intro_says_to_work_the_queue(self, project):
        _bind(project)
        text = enforcement.format_intro(project)
        assert "work queue" in text
        assert "memory_asoode_status" in text

    def test_bound_intro_never_also_says_do_not_start(self, project):
        """A session handed both contradictory instructions would do neither."""
        _bind(project)
        container.task_service.create(
            __import__("memory_mcp.models", fromlist=["CreateTaskRequest"])
            .CreateTaskRequest(project=project, title="Something")
        )
        text = enforcement.format_intro(project)
        assert "NOT instructions" not in text

    def test_unbound_with_a_pat_still_learns_the_word(self, project, monkeypatch):
        monkeypatch.setattr("memory_mcp.asoode.get_pat", lambda *a, **k: "asoode_pat_x")
        text = enforcement.format_intro(project)
        assert "asoode is the task manager" in text
        assert "never bind unprompted" in text

    def test_unbound_without_a_pat_says_nothing_about_asoode(self, project, monkeypatch):
        monkeypatch.setattr("memory_mcp.asoode.get_pat", lambda *a, **k: None)
        assert "asoode" not in enforcement.format_intro(project)

    def test_an_unbound_project_keeps_the_capture_contract(self, project, monkeypatch):
        monkeypatch.setattr("memory_mcp.asoode.get_pat", lambda *a, **k: None)
        container.task_service.create(
            __import__("memory_mcp.models", fromlist=["CreateTaskRequest"])
            .CreateTaskRequest(project=project, title="Parked")
        )
        assert "NOT instructions" in enforcement.format_intro(project)


class TestNoNetworkOnTheHotPath:
    def test_the_per_turn_line_never_calls_asoode(self, project, monkeypatch):
        """The per-turn hook runs behind a 2s timeout on every single prompt."""
        _bind(project)

        def forbidden(*a, **k):
            raise AssertionError("the hook path must not touch the network")

        monkeypatch.setattr("httpx.Client.request", forbidden)
        monkeypatch.setattr("httpx.Client.post", forbidden)
        assert enforcement.asoode_line(project)
        assert enforcement.format_intro(project)
