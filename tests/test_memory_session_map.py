"""The MCP-session -> memory-session mapping survives its own bounds.

Regression (2026-11-03, tg-portal): removing the active-project machinery in
v0.17.0 deleted _SESSION_LIMIT, which remember_memory_session still used to
bound its map - a NameError on every memory_session_start call, live, while
the full suite stayed green, because nothing covered this helper directly.
These tests ARE that coverage: the tool layer's context helpers are an
integration surface, not implementation detail.
"""

import pytest

from memory_mcp import context


@pytest.fixture
def mcp_session(monkeypatch):
    """Pretend calls arrive from a named MCP session."""
    holder = {"id": "mcp-1"}
    monkeypatch.setattr(context, "current_session_id", lambda: holder["id"])
    return holder


class TestRememberMemorySession:
    def test_round_trip(self, mcp_session):
        context.remember_memory_session("mem-1")
        assert context.current_memory_session() == "mem-1"
        context.forget_memory_session("mem-1")
        assert context.current_memory_session() is None

    def test_without_an_mcp_session_nothing_is_stored(self, monkeypatch):
        monkeypatch.setattr(context, "current_session_id", lambda: None)
        context.remember_memory_session("mem-2")
        assert context.current_memory_session() is None

    def test_the_map_is_bounded(self, mcp_session):
        # 300 entries against a 256 cap: the oldest fall off, the newest live.
        for i in range(300):
            mcp_session["id"] = f"mcp-{i}"
            context.remember_memory_session(f"mem-{i}")
        mcp_session["id"] = "mcp-299"
        assert context.current_memory_session() == "mem-299"
        mcp_session["id"] = "mcp-0"
        assert context.current_memory_session() is None

    def test_forget_only_the_matching_session(self, mcp_session):
        context.remember_memory_session("mem-a")
        context.forget_memory_session("mem-different")
        assert context.current_memory_session() == "mem-a"
