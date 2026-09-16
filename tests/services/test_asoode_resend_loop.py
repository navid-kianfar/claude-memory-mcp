"""The mirror must not put the same Done on the wire twice.

On 2026-09-16 two SMG cards each got change-state Done + reposition roughly
every 1.2s for ten hours, ~5,000 pairs per card. The cards were already Done and
already in the Done column. Root cause: the project's task_outbox storage stopped
matching `WHERE id = ?`, so `resolve()` deleted nothing and `fail()` updated
nothing, both silently - the same `state` row came back on every flush, with
zero attempts spent. These tests pin each layer of the fix:

- the outbox proves a write hit its row, and rebuilds the table when it did not;
- `_apply_state` never re-sends the state the board already holds by our record,
  whatever the outbox does;
- a failure that spends no attempt is capped too.
"""

import duckdb
import pytest

import memory_mcp.db.schema as schema_mod
from memory_mcp.container import container
from memory_mcp.db.connection import connect
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.models import CreateTaskRequest
from memory_mcp.providers import TransientProviderError
from memory_mcp.repositories import OutboxRepository
from memory_mcp.repositories.task_repository import (
    MAX_OUTBOX_ATTEMPTS, MAX_TRANSIENT_OUTBOX_FAILURES, OutboxDamagedError,
)
from memory_mcp.services.task_bridge import TaskBridge
from memory_mcp.services.task_service import TaskService
from tests.providers.fakes import FakeProvider

BOARD_LISTS = {"todo": "l-todo", "in_progress": "l-doing", "done": "l-done"}


@pytest.fixture
def project():
    slug = "resend-loop"
    container.project_service.init_project(slug, "Resend Loop")
    upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id="wp1", label="board", is_default=True,
        default_list_id="l-todo", state_list_map=BOARD_LISTS,
    )
    return slug


def _provider(fail=None):
    p = FakeProvider(fail=fail)
    p.seed(container_id="wp1", title="Board", space_id="p1",
           groups=(("l-todo", "To Do"), ("l-doing", "In Progress"), ("l-done", "Done")))
    return p


class _OutboxThatCannotDelete(OutboxRepository):
    """The live symptom, exactly: resolve and fail return and change nothing."""

    def resolve(self, project, row_id):
        return None

    def fail(self, project, row_id, error, *, count=True):
        return False


def _stack(slug, client, outbox=None):
    outbox = outbox or OutboxRepository()
    tasks = TaskService(
        container.task_repo, container.provenance_repo, container.project_repo,
        container.session_repo, outbox_repo=outbox,
    )
    return tasks, TaskBridge(
        container.project_service, tasks, client, outbox_repo=outbox
    ), outbox


class TestAStateRowThatComesBackIsNotResent:
    def test_an_undeletable_done_row_is_sent_once_not_every_flush(self, project):
        client = _provider()
        tasks, bridge, _ = _stack(project, client, _OutboxThatCannotDelete())
        task = tasks.create(CreateTaskRequest(project=project, title="Top-bar filters"))
        bridge.flush(project)
        tasks.done(project, task.id)

        for _ in range(10):
            bridge.flush(project)

        assert client.states.count(("r1", "done")) == 1
        assert client.moves.count(("r1", "l-done")) == 1

    def test_a_duplicate_state_row_sends_nothing(self, project):
        client = _provider()
        tasks, bridge, outbox = _stack(project, client)
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        bridge.flush(project)
        tasks.done(project, task.id)
        bridge.flush(project)
        sent = (list(client.states), list(client.moves))

        outbox.enqueue(project, task.id, "state", {"state": "done"})
        result = bridge.flush(project)

        assert (client.states, client.moves) == sent
        assert result["skipped"] == 1
        assert outbox.depth(project) == 0

    def test_a_real_change_after_it_still_goes(self, project):
        client = _provider()
        tasks, bridge, _ = _stack(project, client)
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        bridge.flush(project)
        tasks.done(project, task.id)
        bridge.flush(project)
        tasks.start(project, task.id)
        bridge.flush(project)
        tasks.done(project, task.id)
        bridge.flush(project)

        assert [s for s in client.states if s[0] == "r1"] == [
            ("r1", "done"), ("r1", "in_progress"), ("r1", "done"),
        ]

    def test_push_is_the_repair_path_and_resends_anyway(self, project):
        client = _provider()
        tasks, bridge, _ = _stack(project, client)
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        bridge.flush(project)
        tasks.done(project, task.id)
        bridge.flush(project)

        bridge.push(project)

        assert client.states.count(("r1", "done")) == 2


def _row_ids(slug):
    with connect(slug) as conn:
        return [r[0] for r in conn.execute(
            "SELECT id FROM task_outbox ORDER BY epoch_us(created_at), rowid").fetchall()]


class TestTheOutboxProvesItsWritesLand:
    def test_a_write_that_misses_a_present_row_rebuilds_then_raises(self, project, monkeypatch):
        outbox = OutboxRepository()
        ids = [outbox.enqueue(project, f"t{i}", "state", {}) for i in range(3)]
        rebuilds = []
        real = schema_mod.rebuild_outbox
        monkeypatch.setattr(schema_mod, "rebuild_outbox",
                            lambda conn: (rebuilds.append(1), real(conn)))

        # A filter that can never match stands in for storage that stopped
        # matching `id = ?`: the row is there, the write touches nothing.
        with pytest.raises(OutboxDamagedError):
            outbox._write_row(
                project, ids[1],
                "DELETE FROM task_outbox WHERE id || '#' = ? RETURNING id", [ids[1]],
            )

        assert rebuilds == [1]
        assert _row_ids(project) == ids, "the rebuild keeps every row, in order"

    def test_the_flusher_stops_loudly_instead_of_looping(self, project, monkeypatch):
        client = _provider()
        tasks, bridge, outbox = _stack(project, client)
        tasks.create(CreateTaskRequest(project=project, title="X"))

        def damaged(slug, row_id):
            raise OutboxDamagedError("cannot delete")
        monkeypatch.setattr(outbox, "resolve", damaged)

        result = bridge.flush(project)

        assert result["failed"] == 1
        assert "cannot delete" in result["reason"]
        assert len(client.created_tasks) == 1, "one pass, not twenty"

    def test_resolving_a_row_already_gone_is_not_an_error(self, project):
        outbox = OutboxRepository()
        row = outbox.enqueue(project, "t", "state", {})
        outbox.resolve(project, row)
        outbox.resolve(project, row)
        assert outbox.depth(project) == 0

    def test_upgrading_rebuilds_the_outbox_without_indexes(self, project):
        outbox = OutboxRepository()
        ids = [outbox.enqueue(project, f"t{i}", "state", {}) for i in range(3)]
        with connect(project) as conn:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_id ON task_outbox(id)")
            conn.execute("DELETE FROM schema_version WHERE version = 18")
            assert schema_mod.run_migrations(conn) == 18
            indexes = conn.execute(
                "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'task_outbox'"
            ).fetchall()
        assert indexes == []
        assert _row_ids(project) == ids
        outbox.resolve(project, ids[0])
        assert _row_ids(project) == ids[1:]

    def test_the_rebuild_works_on_a_pre_v18_table(self, tmp_path):
        conn = duckdb.connect(str(tmp_path / "old.duckdb"))
        try:
            conn.execute("""
                CREATE TABLE task_outbox (
                    id VARCHAR NOT NULL, task_id VARCHAR NOT NULL, link_id INTEGER,
                    op VARCHAR NOT NULL, payload VARCHAR,
                    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
                    attempts INTEGER NOT NULL DEFAULT 0, last_error VARCHAR)
            """)
            conn.execute("CREATE INDEX idx_outbox_id ON task_outbox(id)")
            conn.execute("INSERT INTO task_outbox (id, task_id, op, attempts) "
                         "VALUES ('a', 't', 'state', 2)")
            schema_mod.rebuild_outbox(conn)
            assert conn.execute(
                "SELECT id, attempts, transient_failures FROM task_outbox"
            ).fetchall() == [("a", 2, 0)]
        finally:
            conn.close()


class TestUncountedFailuresAreCapped:
    def test_an_outage_row_is_eventually_given_up_on(self, project):
        client = _provider(fail=TransientProviderError("503"))
        tasks, bridge, outbox = _stack(project, client)
        tasks.create(CreateTaskRequest(project=project, title="X"))

        for _ in range(MAX_TRANSIENT_OUTBOX_FAILURES):
            bridge.flush(project)
        row = outbox.pending(project)[0]
        assert row["attempts"] == 0, "an outage spends no attempt, up to the cap"

        results = [bridge.flush(project) for _ in range(MAX_OUTBOX_ATTEMPTS)]
        assert outbox.depth(project) == 0
        assert results[-1]["abandoned"] == 1
