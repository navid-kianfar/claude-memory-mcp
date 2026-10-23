"""The crash fixes of v0.11.9: one database handle per file, the stale-index
repair, schema v19, and telling a session its folder is not a memory project.

Diagnosis (2026-10-17): the daemon died 81 times in a week - 24 segfaults
inside `duckdb.connect` while another thread was inside DuckDB, and 57 aborts
on "Failed to append to PRIMARY_tasks_0: duplicate key" from the stale index
entries those races left behind.
"""

import random
import threading
import uuid
from pathlib import Path

import duckdb
import pytest

import memory_mcp.db.connection as conn_mod
from memory_mcp.container import container
from memory_mcp.db import repair
from memory_mcp.db.connection import connect
from memory_mcp.models import CreateTaskRequest, TaskFilter
from memory_mcp.db.schema import (
    CURRENT_SCHEMA_VERSION, create_schema, get_schema_version, run_migrations,
)
from memory_mcp.web import hooks


@pytest.fixture
def project(project_slug):
    container.project_service.init_project(project_slug, "Lifecycle")
    return project_slug


class TestOneHandlePerFile:
    def test_operations_share_one_database_instance(self, project):
        with connect(project) as a:
            a.execute("CREATE TABLE probe (x INTEGER)")
        assert len(conn_mod._databases) == 1
        with connect(project) as b:
            b.execute("INSERT INTO probe VALUES (1)")
        with connect(project) as c:
            assert c.execute("SELECT count(*) FROM probe").fetchone()[0] == 1
        assert len(conn_mod._databases) == 1, "a cursor must not open a second handle"

    def test_closing_a_connection_keeps_the_database_open(self, project):
        with connect(project) as conn:
            conn.execute("SELECT 1")
        db = next(iter(conn_mod._databases.values()))
        db.execute("SELECT 1")  # raises ConnectionException if it was closed

    def test_sparse_concurrent_operations_never_race_the_open(self, project):
        """The exact shape that crashed the daemon: few threads, gaps between
        operations, so the last connection keeps closing while another thread
        opens. Per-op `duckdb.connect` raises "Unique file handle conflict"
        here; cursors of one handle never do."""
        with connect(project) as conn:
            conn.execute("CREATE TABLE t (id VARCHAR PRIMARY KEY, n INTEGER)")
        errors: list[BaseException] = []

        def work():
            rng = random.Random()
            for _ in range(150):
                try:
                    with connect(project) as c:
                        if rng.random() < 0.5:
                            c.execute("INSERT INTO t VALUES (?, 0)", [str(uuid.uuid4())])
                        else:
                            c.execute("SELECT count(*) FROM t").fetchone()
                except duckdb.TransactionException:
                    pass  # an ordinary write-write conflict, not the bug
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                threading.Event().wait(rng.random() * 0.001)

        threads = [threading.Thread(target=work) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors[:3]

    def test_release_and_reopen(self, project):
        with connect(project) as conn:
            conn.execute("CREATE TABLE kept (x INTEGER)")
            conn.execute("INSERT INTO kept VALUES (7)")
        assert conn_mod.release_project_database(project) is True
        assert conn_mod._databases == {}
        with connect(project) as conn:
            assert conn.execute("SELECT x FROM kept").fetchone()[0] == 7

    def test_invalidating_a_path_releases_its_handle(self, project):
        with connect(project) as conn:
            conn.execute("SELECT 1")
        assert conn_mod._databases
        conn_mod.invalidate_path_cache(project)
        assert conn_mod._databases == {}

    def test_transaction_still_joins_one_connection(self, project):
        with connect(project) as conn:
            conn.execute("CREATE TABLE tx (x INTEGER)")
        with pytest.raises(RuntimeError):
            with conn_mod.transaction(project):
                with connect(project) as c:
                    c.execute("INSERT INTO tx VALUES (1)")
                raise RuntimeError("roll back")
        with connect(project) as conn:
            assert conn.execute("SELECT count(*) FROM tx").fetchone()[0] == 0


class TestSchemaV19:
    def test_tasks_carry_no_secondary_index(self, project):
        with connect(project) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'tasks'"
            ).fetchall()}
        assert not names & set(repair_names()), names

    def test_v18_database_loses_them_on_upgrade(self, tmp_path):
        db = tmp_path / "v18.duckdb"
        conn = duckdb.connect(str(db))
        create_schema(conn)
        conn.execute("CREATE INDEX idx_tasks_state ON tasks (state)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT current_timestamp)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (18)")
        assert get_schema_version(conn) == 18
        assert run_migrations(conn) == CURRENT_SCHEMA_VERSION == 19
        left = conn.execute(
            "SELECT count(*) FROM duckdb_indexes() WHERE index_name = 'idx_tasks_state'"
        ).fetchone()[0]
        conn.close()
        assert left == 0


def _task(project, title):
    return container.task_service.create(CreateTaskRequest(project=project, title=title))


def repair_names():
    from memory_mcp.db.schema import _TASK_SECONDARY_INDEXES

    return _TASK_SECONDARY_INDEXES


class TestRepair:
    def test_healthy_project_reports_ok(self, project):
        _task(project, "a healthy task")
        result = repair.repair_project(project)
        assert result["status"] == "ok", result
        assert result["rebuilt"] == []

    def test_rebuild_keeps_rows_defaults_keys_and_indexes(self, project):
        with connect(project) as conn:
            conn.execute(
                "CREATE TABLE things (id VARCHAR PRIMARY KEY, n INTEGER DEFAULT 5, "
                "tags VARCHAR[])"
            )
            conn.execute("CREATE INDEX idx_things_n ON things (n)")
            conn.execute("INSERT INTO things (id, tags) VALUES ('a', ['x']), ('b', [])")
            assert repair.rebuild_table(conn, "things") == 2
            assert conn.execute(
                "SELECT id, n, tags FROM things ORDER BY id").fetchall() == [
                ("a", 5, ["x"]), ("b", 5, [])]
            conn.execute("INSERT INTO things (id) VALUES ('c')")
            assert conn.execute("SELECT n FROM things WHERE id='c'").fetchone()[0] == 5
            with pytest.raises(duckdb.ConstraintException):
                conn.execute("INSERT INTO things (id) VALUES ('a')")
            assert conn.execute(
                "SELECT count(*) FROM duckdb_indexes() WHERE index_name = 'idx_things_n'"
            ).fetchone()[0] == 1

    def test_flagged_table_is_rebuilt(self, project, monkeypatch):
        """A real stale index cannot be manufactured on demand, so the probe's
        verdict is stubbed: the point here is what repair does with it."""
        _task(project, "survives the rebuild")
        verdicts = iter([[{"table": "tasks", "key": "id", "returncode": -6,
                           "error": "INTERNAL Error: Failed to append"}], []])
        monkeypatch.setattr(repair, "probe", lambda path: next(verdicts))
        result = repair.repair_project(project)
        assert result["status"] == "repaired", result
        assert result["rebuilt"] == [{"table": "tasks", "rows": 1}]
        tasks, _, _ = container.task_repo.list_tasks(project, TaskFilter())
        assert [t.title for t in tasks] == ["survives the rebuild"]

    def test_check_only_rebuilds_nothing(self, project, monkeypatch):
        monkeypatch.setattr(repair, "probe", lambda path: [
            {"table": "tasks", "key": "id", "returncode": -6, "error": "x"}])
        called = []
        monkeypatch.setattr(repair, "rebuild_table", lambda c, t: called.append(t))
        result = repair.repair_project(project, check_only=True)
        assert result["status"] == "stale" and called == []

    def test_probe_runs_in_a_child_and_reads_a_copy(self, project):
        with connect(project) as conn:
            conn.execute("CREATE TABLE p (id VARCHAR PRIMARY KEY)")
            conn.execute("INSERT INTO p VALUES ('one')")
            conn.execute("CHECKPOINT")
        path = conn_mod._resolve_db_path(project)
        before = path.stat().st_mtime_ns
        assert repair.probe(path) == []
        assert path.stat().st_mtime_ns == before


class TestUnregisteredFolder:
    def test_intro_names_the_gap(self, tmp_path):
        folder = tmp_path / "fresh-idea"
        folder.mkdir()
        text = hooks._unregistered_intro(str(folder))
        assert "NOT a memory project" in text and "memory_init_project" in text

    def test_intro_is_silent_for_home_and_missing_folders(self, tmp_path):
        assert hooks._unregistered_intro(str(Path.home())) == ""
        assert hooks._unregistered_intro(str(tmp_path / "nope")) == ""
        assert hooks._unregistered_intro("") == ""

    def test_new_folder_beside_projects_is_a_project(self, tmp_path):
        workspace = tmp_path / "acme"
        (workspace / "api").mkdir(parents=True)
        (workspace / "new-app").mkdir()
        container.project_service.init_project(
            "api", "api", project_path=str(workspace / "api"))
        assert hooks._beside_projects(workspace / "new-app") is True
        assert hooks._beside_projects(workspace) is False, "a workspace is not a project"
        lone = tmp_path / "elsewhere" / "thing"
        lone.mkdir(parents=True)
        assert hooks._beside_projects(lone) is False


_SERVICE_STRESS = '''
import random, sys, tempfile, threading, time
from pathlib import Path
from memory_mcp.config import settings
d = Path(tempfile.mkdtemp(prefix="mmstress-"))
settings.data_dir = d / "m"
settings.asoode_auto_mirror = False
settings.ensure_dirs()
from memory_mcp.container import container
from memory_mcp.models import CreateTaskRequest
container.project_service.init_project("stress", "s")

def work(i):
    rng = random.Random(i)
    ids = []
    end = time.time() + 6
    while time.time() < end:
        try:
            r = rng.random()
            if r < 0.3 or not ids:
                ids.append(container.task_service.create(
                    CreateTaskRequest(project="stress", title=f"t{i}")).id)
            elif r < 0.55:
                container.task_service.start("stress", rng.choice(ids))
            elif r < 0.8:
                container.task_service.stop("stress", rng.choice(ids))
            else:
                container.task_service.list_tasks("stress", limit=50)
        except Exception:
            pass
        time.sleep(rng.random() * 0.003)

threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]
print("SURVIVED")
'''


def test_task_services_survive_concurrent_use():
    """What the test agent's stress run did to the installed daemon on
    2026-10-19: parallel task create/start/stop/list. It segfaulted the process
    3 runs in 3 while `SET disabled_optimizers` (a GLOBAL setting) ran on every
    operation. A child process, so a crash fails this test instead of pytest."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", _SERVICE_STRESS],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0 and "SURVIVED" in result.stdout, (
        result.returncode, result.stderr[-2000:])
