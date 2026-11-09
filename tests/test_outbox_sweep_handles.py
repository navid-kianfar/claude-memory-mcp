"""The minute outbox sweep must not undo the idle LRU.

It visits every linked project. Reading each one's outbox opened more files
than `db_max_open_handles` keeps, so every minute the overflow was closed
(CHECKPOINT) and reopened (connect, LOAD vss, CHECKPOINT) under the global
handle lock. A sweep over projects whose files are closed and whose outboxes
nothing has written since must open nothing.
"""

import pytest

import memory_mcp.db.connection as conn_mod
from memory_mcp.container import container
from memory_mcp.db.connection import close_idle_databases, connect, open_database_count
from memory_mcp.repositories.project_repository import ProjectRepository

PROJECT_COUNT = 12
HANDLE_CAP = 4


@pytest.fixture
def linked_projects(monkeypatch):
    """More linked projects than the handle cap, each with a fresh schema."""
    repo = ProjectRepository()
    slugs = tuple(f"sweep-project-{i:02d}" for i in range(PROJECT_COUNT))
    for slug in slugs:
        repo.register(slug, slug.title())
        with connect(slug) as conn:
            conn.execute("SELECT 1").fetchall()
    monkeypatch.setattr("memory_mcp.db.registry.linked_slugs", lambda: list(slugs))
    return slugs


@pytest.fixture
def nudged(monkeypatch):
    """The projects the sweep asked to flush, without running a flush."""
    calls: list[str] = []
    monkeypatch.setattr(container, "_mirror_soon", calls.append)
    return calls


@pytest.fixture
def opens(monkeypatch):
    """Every DuckDB file this process opens from here on."""
    calls: list[str] = []
    real_connect = conn_mod.duckdb.connect

    def counting_connect(database, *args, **kwargs):
        calls.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(conn_mod.duckdb, "connect", counting_connect)
    return calls


def _enqueue(slug: str) -> None:
    container.outbox_repo.enqueue(slug, "task-1", "update")


class TestTheSweepLeavesClosedFilesClosed:
    def test_a_sweep_over_more_projects_than_the_cap_opens_nothing(
        self, linked_projects, nudged, opens
    ):
        container.sweep_outboxes()
        close_idle_databases(max_handles=HANDLE_CAP)
        assert open_database_count() <= HANDLE_CAP
        opens.clear()

        container.sweep_outboxes()

        assert opens == []
        assert open_database_count() <= HANDLE_CAP
        assert nudged == []

    def test_a_closed_project_with_pending_rows_is_still_nudged(
        self, linked_projects, nudged, opens
    ):
        _enqueue(linked_projects[0])
        container.sweep_outboxes()
        close_idle_databases(max_handles=0)
        opens.clear()
        nudged.clear()

        container.sweep_outboxes()

        assert nudged == [linked_projects[0]]
        assert opens == [], "the nudge decides on the remembered depth"


class TestAWriteIsNeverMissed:
    def test_a_row_queued_after_the_last_sweep_is_seen_after_its_file_closes(
        self, linked_projects, nudged
    ):
        container.sweep_outboxes()
        _enqueue(linked_projects[3])
        close_idle_databases(max_handles=0)

        container.sweep_outboxes()

        assert nudged == [linked_projects[3]]

    def test_a_drained_outbox_stops_being_nudged(self, linked_projects, nudged):
        slug = linked_projects[1]
        row_id = container.outbox_repo.enqueue(slug, "task-1", "update")
        container.sweep_outboxes()
        container.outbox_repo.resolve(slug, row_id)
        close_idle_databases(max_handles=0)
        nudged.clear()

        container.sweep_outboxes()

        assert nudged == []

    def test_an_open_file_is_read_afresh(self, linked_projects, nudged):
        slug = linked_projects[2]
        container.sweep_outboxes()
        with connect(slug) as conn:
            conn.execute(
                "INSERT INTO task_outbox (id, task_id, op, payload) "
                "VALUES ('raw-row', 'task-1', 'update', '{}')"
            )

        container.sweep_outboxes()

        assert nudged == [slug]
