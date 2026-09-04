"""The management UI must never steer what an MCP session writes to.

INCIDENT 2026-09-04: `POST /api/active-project` called set_active_project(), which
writes a process-global the daemon shares with every MCP session, every subagent
and the UI at once. A memory_task_plan with no explicit project then resolved to
whatever the panel was last looking at - seven tasks landed in the wrong project
and were mirrored onto that project's board within seconds.

A viewer must not steer a writer.
"""

import pytest

from memory_mcp.context import get_active_project, set_active_project
from memory_mcp.db.registry import get_setting
from memory_mcp.web import routes


@pytest.fixture(autouse=True)
def _clear_active():
    set_active_project("")
    yield
    set_active_project("")


class TestActiveProjectIsolation:
    def test_ui_selection_does_not_change_the_mcp_active_project(self, monkeypatch):
        """The whole point. Clicking a project in the panel must not redirect writes."""
        set_active_project("claude-memory-mcp")
        monkeypatch.setattr(
            routes.container.project_service, "get", lambda slug: object()
        )

        routes._set_active({}, {"slug": "some-other-project"}, {})

        assert get_active_project() == "claude-memory-mcp"

    def test_ui_selection_is_stored_under_its_own_key(self, monkeypatch):
        monkeypatch.setattr(
            routes.container.project_service, "get", lambda slug: object()
        )

        routes._set_active({}, {"slug": "some-other-project"}, {})

        assert get_setting(routes.UI_ACTIVE_PROJECT_KEY) == "some-other-project"

    def test_the_ui_route_cannot_reach_set_active_project_at_all(self):
        """Structural, not behavioural: the import is the thing that must not return."""
        assert not hasattr(routes, "set_active_project"), (
            "web routes must not be able to mutate the MCP-wide active project"
        )

    def test_a_blank_slug_is_refused(self, monkeypatch):
        with pytest.raises(ValueError):
            routes._set_active({}, {"slug": "  "}, {})
