"""The active project is per MCP session, not per process.

THE INCIDENT this closes: one daemon serves every Claude session on the machine,
every subagent of every session, and the management UI. With a single global,
session A calling memory_use('x') redirected session B's next project-less write.
On 2026-09-04 that put seven tasks in the wrong project and mirrored them to that
project's board within seconds; asoode has no hard delete, so the cleanup was
archiving seven cards by hand.

The UI half was fixed first (tests/test_active_project_isolation.py). This is the
other half: two MCP sessions can no longer redirect each other.
"""

import pytest

from memory_mcp import context
from memory_mcp.context import (
    forget_session_project, get_active_project, set_active_project,
)


@pytest.fixture
def as_session(monkeypatch):
    """Pretend calls arrive from a named MCP session."""
    def _use(session_id):
        monkeypatch.setattr(context, "current_session_id", lambda: session_id)
    return _use


@pytest.fixture(autouse=True)
def _clean():
    context._session_projects.clear()
    yield
    context._session_projects.clear()


class TestPerSessionActiveProject:
    def test_two_sessions_do_not_redirect_each_other(self, as_session, monkeypatch):
        """The whole point."""
        as_session("session-a")
        set_active_project("project-a")
        as_session("session-b")
        set_active_project("project-b")

        as_session("session-a")
        assert get_active_project() == "project-a"
        as_session("session-b")
        assert get_active_project() == "project-b"

    def test_a_caller_with_no_session_still_sees_the_last_choice(self, as_session):
        """The CLI, the hooks and the daemon's background work have no session.

        Their behaviour must be exactly what it was before, or every shell
        command would suddenly resolve to nothing.
        """
        as_session("session-a")
        set_active_project("project-a")

        as_session(None)
        assert get_active_project() == "project-a"

    def test_a_session_that_never_chose_falls_back(self, as_session):
        as_session(None)
        set_active_project("global-choice")

        as_session("fresh-session")
        assert get_active_project() == "global-choice"

    def test_ending_a_session_drops_its_choice(self, as_session):
        as_session("session-a")
        set_active_project("project-a")
        as_session(None)
        set_active_project("global-choice")

        forget_session_project("session-a")

        as_session("session-a")
        assert get_active_project() == "global-choice"

    def test_the_map_is_bounded(self, as_session):
        """Sessions are ephemeral and we are never told when one ends."""
        for i in range(context._SESSION_LIMIT + 25):
            as_session(f"s{i}")
            set_active_project(f"p{i}")

        assert len(context._session_projects) <= context._SESSION_LIMIT

    def test_the_newest_sessions_survive_eviction(self, as_session):
        for i in range(context._SESSION_LIMIT + 5):
            as_session(f"s{i}")
            set_active_project(f"p{i}")

        last = context._SESSION_LIMIT + 4
        as_session(f"s{last}")
        assert get_active_project() == f"p{last}"

    def test_session_id_lookup_never_raises(self, monkeypatch):
        """It runs on every resolution, including where there is no context."""
        import memory_mcp.context as ctx

        assert ctx.current_session_id() is None  # no MCP request in flight here
