"""There is NO active project: a project-scoped call names its project or fails.

The old mechanism - a process-global (then a per-session map) that one call set
and every project-less call silently inherited - put seven tasks into the wrong
project and onto the wrong board on 2026-09-04, and the cleanup could only
archive cards by hand because asoode has no delete. The rule that followed
("pass project= on every write") depended on the agent remembering; the
mechanism is the thing that had to go. Removed 2026-11-03, user decision.

These tests pin the world that replaced it: nothing shared exists to leak.
"""

import pytest

from memory_mcp import context
from memory_mcp.context import resolve_project
from memory_mcp.server import _resolve


class TestTheMechanismIsGone:
    def test_the_old_globals_do_not_exist(self):
        import memory_mcp.context as mod

        for name in (
            "set_active_project", "load_active_project", "get_active_project",
            "forget_session_project", "_active_project", "_session_projects",
        ):
            assert not hasattr(mod, name), f"{name} still exists"

    def test_the_ui_key_is_gone_from_the_routes(self):
        import memory_mcp.web.routes as routes

        assert not hasattr(routes, "UI_ACTIVE_PROJECT_KEY")
        assert not hasattr(routes, "_set_active")

    def test_resolution_has_no_hidden_fallback(self):
        """explicit > cwd-detect > None - and nothing else. No global, no
        session map, no registry key."""
        assert resolve_project("p") == "p"
        assert resolve_project(None, "/nonexistent-path-xyz") is None
        assert resolve_project(None) is None

    def test_cwd_detection_still_works_for_its_owners(self, tmp_path, monkeypatch):
        """Hooks and the CLI pass a cwd; that path is stateless and stays."""
        from memory_mcp.repositories.project_repository import ProjectRepository

        uid = "abcdefab-abcd-abcd-abcd-abcdefabcdef"
        snap = tmp_path / ".agent-memory"
        snap.mkdir()
        (snap / "manifest.json").write_text(
            f'{{"version": 3, "project_id": "{uid}", "slug": "cwd-proj"}}'
        )
        ProjectRepository().register("cwd-proj", "CWD Proj")
        ProjectRepository().set_uid("cwd-proj", uid)
        monkeypatch.chdir(tmp_path)
        assert context.detect_project_from_cwd(str(tmp_path)) == "cwd-proj"


class TestServerModeIsExplicitOnly:
    def test_server_mode_never_detects_from_cwd(self, monkeypatch):
        """Remote clients have a meaningless daemon cwd; the answer there is
        the teaching error, not a guess."""
        from memory_mcp.config import settings

        monkeypatch.setattr(settings, "mode", "server")
        with pytest.raises(ValueError, match="project=<slug>"):
            _resolve(None)

    def test_explicit_project_passes_in_server_mode(self, monkeypatch):
        from memory_mcp.config import settings

        monkeypatch.setattr(settings, "mode", "server")
        assert _resolve("p1") == "p1"
