"""Unit tests for SessionRepository."""

import uuid

import pytest

from memory_mcp.repositories import SessionRepository, ProjectRepository
from memory_mcp.db.connection import get_connection


@pytest.fixture
def repo():
    return SessionRepository()


@pytest.fixture
def project(project_slug) -> str:
    pr = ProjectRepository()
    pr.register(project_slug, "Test")
    conn = get_connection(project_slug)
    conn.close()
    return project_slug


class TestLifecycle:
    def test_insert_and_end(self, repo, project):
        sid = str(uuid.uuid4())
        repo.insert(project, sid)
        # No exception = success
        repo.end(project, sid, "summary text", memories_created=3, memories_accessed=5)
        last = repo.last_with_summary(project)
        assert last is not None
        assert last.summary == "summary text"
        assert last.memories_created == 3

    def test_orphaned_detects_unended(self, repo, project):
        sid1 = str(uuid.uuid4())
        sid2 = str(uuid.uuid4())
        repo.insert(project, sid1)
        repo.insert(project, sid2)
        repo.end(project, sid1, "done")

        orphans = repo.orphaned(project)
        assert sid2 in orphans
        assert sid1 not in orphans

    def test_last_with_summary_filters(self, repo, project):
        s1 = str(uuid.uuid4())
        s2 = str(uuid.uuid4())
        repo.insert(project, s1)
        repo.end(project, s1, "[auto-closed]")
        repo.insert(project, s2)
        repo.end(project, s2, "real work")

        last_excluding = repo.last_with_summary(project, exclude_summary="[auto-closed]")
        assert last_excluding.summary == "real work"


class TestWhoTheSessionBelongsTo:
    """`sessions.metadata` existed unused until `{"agent": ...}` gave it a job:
    telling a dispatched agent's session from the lead's. Nothing else can - a
    subagent shares the lead's MCP connection, so there is no other signal."""

    def test_an_agents_session_round_trips_its_metadata(self, repo, project):
        sid = str(uuid.uuid4())
        repo.insert(project, sid, metadata={"agent": "test", "mcp_session": "m1"})

        assert repo.metadata(project, sid) == {"agent": "test", "mcp_session": "m1"}

    def test_a_session_with_no_metadata_has_none(self, repo, project):
        sid = str(uuid.uuid4())
        repo.insert(project, sid)

        assert repo.metadata(project, sid) is None

    def test_an_unknown_session_has_no_metadata(self, repo, project):
        assert repo.metadata(project, str(uuid.uuid4())) is None

    def test_open_lead_sessions_excludes_the_agents(self, repo, project):
        lead = str(uuid.uuid4())
        agent = str(uuid.uuid4())
        repo.insert(project, lead, metadata={"agent": None, "mcp_session": "m1"})
        repo.insert(project, agent, metadata={"agent": "python", "mcp_session": "m1"})

        assert repo.open_lead_sessions(project) == [lead]

    def test_a_session_written_before_metadata_existed_counts_as_a_lead(
        self, repo, project,
    ):
        """Every row written before this change has a NULL metadata, and every one
        of them was a lead. They must keep reading that way."""
        sid = str(uuid.uuid4())
        repo.insert(project, sid)

        assert repo.open_lead_sessions(project) == [sid]

    def test_an_ended_lead_session_is_no_longer_open(self, repo, project):
        sid = str(uuid.uuid4())
        repo.insert(project, sid)
        repo.end(project, sid, "done")

        assert repo.open_lead_sessions(project) == []

    def test_several_leads_are_returned_oldest_first(self, repo, project):
        first = str(uuid.uuid4())
        second = str(uuid.uuid4())
        repo.insert(project, first)
        repo.insert(project, second)

        assert repo.open_lead_sessions(project) == [first, second]
