"""The asoode outbox must stay readable on the storage shape a queue produces.

On 2026-09-14 the kalagh outbox held 558 rows and `OutboxRepository.pending()`
returned none of them: DuckDB 1.5's `row_group_pruner` answers
`ORDER BY created_at LIMIT n` from row-group statistics that still count
DELETED rows. The flusher read an empty queue, recorded no error, and the board
silently stopped updating for three days.

The shape is built here the way the daemon builds it: the flusher holds a delete
open while another connection enqueues, which starts a new, tiny row group; once
the oldest groups are fully deleted, the pruner discards the groups holding the
live rows.
"""

import logging

import duckdb
import pytest

import memory_mcp.db.connection as conn_mod
from memory_mcp.container import container
from memory_mcp.db.connection import get_connection
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.repositories import OutboxRepository
from memory_mcp.repositories.task_repository import OUTBOX_PENDING_SQL

SLUG = "outbox-shape"
BARE_ORDER = "SELECT id FROM task_outbox ORDER BY created_at ASC LIMIT ?"


@pytest.fixture
def project():
    container.project_service.init_project(SLUG, "Outbox Shape")
    return SLUG


def _fragment(slug: str, outbox: OutboxRepository, dead_rounds: int = 40, live: int = 20) -> list[str]:
    """Leave the outbox in the shape that broke the mirror; return the live ids, oldest first.

    Each round deletes the oldest row inside an OPEN transaction on one
    connection while a second, long-lived connection appends two rows - the
    flusher and task writes overlapping, as they do in the daemon's threads.
    Then everything from that phase is flushed, and `live` rows arrive that
    nobody flushes. Both connections are plain: the store's optimizer guard
    must not be what shapes the storage.
    """
    path = str(conn_mod._resolve_db_path(slug))
    flusher = duckdb.connect(path)
    writer = duckdb.connect(path)
    counter = iter(range(10_000))

    def append(task: str) -> str:
        row_id = f"row-{next(counter):05d}"
        writer.execute(
            "INSERT INTO task_outbox (id, task_id, op, payload) VALUES (?, ?, 'state', '{}')",
            [row_id, task],
        )
        return row_id

    try:
        append("t-seed")
        append("t-seed")
        for i in range(dead_rounds):
            oldest = flusher.execute(
                "SELECT id FROM task_outbox ORDER BY epoch_us(created_at), rowid LIMIT 1"
            ).fetchone()[0]
            flusher.execute("BEGIN")
            flusher.execute("DELETE FROM task_outbox WHERE id = ?", [oldest])
            append(f"t-dead-{i}")
            append(f"t-dead-{i}")
            flusher.execute("COMMIT")
            if i % 5 == 0:
                writer.execute("CHECKPOINT")
        for (row_id,) in flusher.execute("SELECT id FROM task_outbox").fetchall():
            flusher.execute("DELETE FROM task_outbox WHERE id = ?", [row_id])
        live_ids = [append(f"t-live-{i}") for i in range(live)]
        writer.execute("CHECKPOINT")
    finally:
        flusher.close()
        writer.close()
    return live_ids


def _plain_connection(slug: str) -> duckdb.DuckDBPyConnection:
    """A connection WITHOUT the store's optimizer guard - what the bug sees."""
    return duckdb.connect(str(conn_mod._resolve_db_path(slug)))


class TestFragmentedOutbox:
    def test_the_fixture_really_reproduces_the_duckdb_bug(self, project):
        # Guards the other tests from passing vacuously: if this DuckDB no
        # longer mis-prunes, there is nothing left for them to prove.
        outbox = OutboxRepository()
        _fragment(project, outbox)
        conn = _plain_connection(project)
        try:
            live = conn.execute("SELECT count(*) FROM task_outbox").fetchone()[0]
            first = conn.execute(BARE_ORDER, [1]).fetchall()
        finally:
            conn.close()
        assert live == 20
        if first:
            pytest.skip(f"duckdb {duckdb.__version__} no longer mis-prunes this shape")
        assert first == []

    def test_pending_returns_the_oldest_live_rows(self, project):
        outbox = OutboxRepository()
        live_ids = _fragment(project, outbox)

        rows = outbox.pending(project, 10)

        assert [r["id"] for r in rows] == live_ids[:10]
        assert outbox.unreadable(project) == 0

    def test_pending_reads_the_whole_queue_in_order(self, project):
        outbox = OutboxRepository()
        live_ids = _fragment(project, outbox)

        assert [r["id"] for r in outbox.pending(project, 200)] == live_ids

    def test_the_pending_query_is_correct_even_without_the_connection_guard(self, project):
        # Two independent defences: this one holds on a plain connection.
        outbox = OutboxRepository()
        live_ids = _fragment(project, outbox)
        conn = _plain_connection(project)
        try:
            rows = conn.execute(OUTBOX_PENDING_SQL, [10]).fetchall()
        finally:
            conn.close()
        assert [r[0] for r in rows] == live_ids[:10]

    def test_every_project_connection_is_guarded_not_only_the_outbox_query(self, project):
        # The same pruner would break any `ORDER BY <column> LIMIT` on a table
        # that sees deletes, so the store's connections disable it outright.
        outbox = OutboxRepository()
        live_ids = _fragment(project, outbox)
        conn = get_connection(project)
        try:
            rows = conn.execute(BARE_ORDER, [5]).fetchall()
        finally:
            conn.close()
        assert [r[0] for r in rows] == live_ids[:5]


class TestABlindReadIsLoud:
    @pytest.fixture
    def linked(self, project):
        upsert_project_link(
            project, base_url="https://api.asoode.com", remote_project_id="p1",
            remote_work_package_id="wp1", label="board", is_default=True,
            default_list_id="l-todo", state_list_map={"todo": "l-todo"},
        )
        return project

    def test_unreadable_is_zero_when_the_queue_reads_or_is_empty(self, project):
        outbox = OutboxRepository()
        assert outbox.unreadable(project) == 0
        outbox.enqueue(project, "t1", "state")
        assert outbox.unreadable(project) == 0

    def test_unreadable_counts_a_queue_the_read_cannot_return(self, project, monkeypatch):
        outbox = OutboxRepository()
        outbox.enqueue(project, "t1", "state")
        outbox.enqueue(project, "t2", "state")
        monkeypatch.setattr(outbox, "pending", lambda slug, limit=200: [])
        assert outbox.unreadable(project) == 2

    def test_the_flusher_logs_and_reports_a_stalled_read(self, linked, monkeypatch, caplog):
        from memory_mcp.services.task_bridge import TaskBridge
        from tests.providers.fakes import FakeProvider

        outbox = OutboxRepository()
        outbox.enqueue(linked, "t1", "state")
        monkeypatch.setattr(outbox, "pending", lambda slug, limit=200: [])
        bridge = TaskBridge(container.project_service, container.task_service, FakeProvider(),
                            outbox_repo=outbox)

        with caplog.at_level(logging.ERROR, logger="memory_mcp.services.task_bridge"):
            result = bridge.flush(linked)

        assert result["flushed"] == 0
        assert "no rows while 1 are queued" in result["reason"]
        assert any("stalled" in rec.getMessage() for rec in caplog.records)

    def test_the_mirror_report_says_stalled(self, linked, monkeypatch):
        from memory_mcp import server

        outbox = OutboxRepository()
        outbox.enqueue(linked, "t1", "state")
        monkeypatch.setattr(outbox, "pending", lambda slug, limit=200: [])
        monkeypatch.setattr(container, "outbox_repo", outbox, raising=False)

        report = server._mirror_report(linked)

        assert report["pending"] == 1
        assert "stalled" in report

    def test_a_healthy_mirror_report_has_no_stalled_key(self, linked, monkeypatch):
        from memory_mcp import server

        outbox = OutboxRepository()
        outbox.enqueue(linked, "t1", "state")
        monkeypatch.setattr(container, "outbox_repo", outbox, raising=False)

        assert "stalled" not in server._mirror_report(linked)
