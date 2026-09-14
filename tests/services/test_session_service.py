"""Unit tests for SessionService."""

import pytest

from memory_mcp.container import Container
from memory_mcp.db.connection import get_connection
from memory_mcp.models import StoreMemoryRequest, MemoryCategory
from memory_mcp.services.session_service import AUTO_CLOSE_SUMMARY


@pytest.fixture
def container():
    return Container()


@pytest.fixture
def project(container, project_slug):
    container.project_repo.register(project_slug, "Test")
    conn = get_connection(project_slug)
    conn.close()
    return project_slug


class TestSession:
    def test_start_returns_context(self, container, project):
        container.memory_service.store(StoreMemoryRequest(
            project=project, category=MemoryCategory.MANDATORY_RULES,
            title="R", content="always test",
        ))
        container.memory_service.store(StoreMemoryRequest(
            project=project, category=MemoryCategory.SPRINT,
            title="Sprint 1", content="auth module",
        ))
        ctx = container.session_service.start(project)
        assert ctx.session_id
        assert len(ctx.mandatory_rules) == 1
        assert len(ctx.active_sprint) == 1

    def test_end_stores_summary(self, container, project):
        ctx = container.session_service.start(project)
        container.session_service.end(project, ctx.session_id, "all done")

        ctx2 = container.session_service.start(project)
        assert ctx2.last_session_summary == "all done"

    def test_orphaned_sessions_auto_closed(self, container, project):
        # Each start closes any unended sessions first, so:
        # ctx1 starts (0 orphans)
        # ctx2 starts, closes ctx1 (1 orphan)
        # ctx3 starts, closes ctx2 (1 orphan)
        ctx1 = container.session_service.start(project)
        assert ctx1.orphaned_sessions_closed == 0
        ctx2 = container.session_service.start(project)
        assert ctx2.orphaned_sessions_closed == 1
        ctx3 = container.session_service.start(project)
        assert ctx3.orphaned_sessions_closed == 1

    def test_last_summary_skips_auto_close(self, container, project):
        ctx1 = container.session_service.start(project)
        container.session_service.end(project, ctx1.session_id, "real work")

        # Now simulate an orphaned one
        ctx2 = container.session_service.start(project)
        ctx3 = container.session_service.start(project)  # closes ctx2 with auto-close

        assert ctx3.last_session_summary == "real work"


class TestWhichAgentStartedIt:
    """A dispatched agent names itself; the lead passes nothing. Self-reported on
    purpose - there is no way to ask a session what dispatched it - and the
    failure mode is chosen: an agent that forgets looks like a second lead, which
    makes a caller ambiguous rather than wrong."""

    def test_an_agent_records_its_type(self, container, project):
        ctx = container.session_service.start(project, agent="test")

        meta = container.session_repo.metadata(project, ctx.session_id)
        assert meta["agent"] == "test"

    def test_the_lead_records_no_agent(self, container, project):
        ctx = container.session_service.start(project)

        meta = container.session_repo.metadata(project, ctx.session_id)
        assert meta["agent"] is None

    def test_the_mcp_session_is_recorded_beside_it(self, container, project):
        """Not an identity - subagents share it with the lead - but the only link
        back to the connection, and unrecoverable later."""
        ctx = container.session_service.start(project)

        assert "mcp_session" in container.session_repo.metadata(project, ctx.session_id)

    def test_is_lead_session_tells_them_apart(self, container, project):
        lead = container.session_service.start(project)
        agent = container.session_service.start(project, agent="python")

        assert container.session_service.is_lead_session(project, lead.session_id)
        assert not container.session_service.is_lead_session(project, agent.session_id)

    def test_an_unknown_session_reads_as_a_lead(self, container, project):
        """The answer that changes nothing. A missing row is not evidence of a
        subagent, and treating it as one would silently drop a real lead."""
        assert container.session_service.is_lead_session(project, "never-started")

    def test_no_session_id_is_not_a_lead(self, container, project):
        assert not container.session_service.is_lead_session(project, "")

    def test_a_dispatched_agent_does_not_auto_close_the_lead(self, container, project):
        """The bug this parameter would otherwise have walked into. `start` closes
        every unended session as a presumed crash - so the first subagent of a
        session ended the only lead there was, and `open_lead_sessions` went empty
        exactly when it was needed."""
        lead = container.session_service.start(project)

        container.session_service.start(project, agent="python")

        assert container.session_service.open_lead_sessions(project) == [lead.session_id]
        assert container.session_repo.metadata(project, lead.session_id)["agent"] is None

    def test_several_agents_leave_the_lead_open(self, container, project):
        lead = container.session_service.start(project)

        for name in ("python", "react", "reviewer"):
            container.session_service.start(project, agent=name)

        assert container.session_service.open_lead_sessions(project) == [lead.session_id]

    def test_a_new_LEAD_session_still_auto_closes_the_old_one(
        self, container, project,
    ):
        """The crash-recovery behaviour is unchanged for the case it was written
        for: a second lead means the first one is gone."""
        first = container.session_service.start(project)

        second = container.session_service.start(project)

        assert second.orphaned_sessions_closed == 1
        assert container.session_service.open_lead_sessions(project) == [
            second.session_id
        ]
        assert first.session_id not in container.session_repo.orphaned(project)

    def test_an_agent_session_reports_no_orphans_closed(self, container, project):
        container.session_service.start(project)

        ctx = container.session_service.start(project, agent="python")

        assert ctx.orphaned_sessions_closed == 0

    def test_an_open_agent_session_is_not_an_open_lead_session(
        self, container, project,
    ):
        """Started on its own so nothing else is open: the agent's session IS
        unended, and still must not be offered as a lead's."""
        ctx = container.session_service.start(project, agent="python")

        assert container.session_repo.orphaned(project) == [ctx.session_id]
        assert container.session_service.open_lead_sessions(project) == []

    def test_an_open_lead_session_is_listed(self, container, project):
        ctx = container.session_service.start(project)

        assert container.session_service.open_lead_sessions(project) == [ctx.session_id]
