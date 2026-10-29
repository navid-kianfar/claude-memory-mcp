"""v0.11.10: the daemon heals an invalidated database instead of serving it dead.

Incident (2026-10-29, kalagh-monorepo.duckdb, duckdb 1.5.2): `record_plan`'s
purge failed with "Failed to delete all rows from index. Only deleted 0 out of
1 rows" on the SECONDARY index idx_task_plans_hash. DuckDB invalidated the
database, and with one long-lived handle per file every call on that project
failed until a restart. The repair probe of v0.11.9 never looked at secondary
indexes, so `repair --check` could not have seen it coming.
"""

import json
import threading
import time

import duckdb
import pytest

import memory_mcp.db.connection as conn_mod
from memory_mcp import server
from memory_mcp.config import settings
from memory_mcp.container import container
from memory_mcp.db import repair
from memory_mcp.db.connection import connect, is_fatal, transaction
from memory_mcp.db.schema import (
    CURRENT_SCHEMA_VERSION, create_schema, get_schema_version, run_migrations,
)

INVALIDATED = (
    "FATAL Error: Failed: database has been invalidated because of a previous "
    "fatal error. The database must be restarted prior to being used again."
)


@pytest.fixture
def project(project_slug):
    container.project_service.init_project(project_slug, "Self heal")
    return project_slug


@pytest.fixture
def repairs(monkeypatch):
    """repair_project stubbed: the heal's job is to call it, not to re-test it."""
    calls = []

    def fake(slug, check_only=False):
        calls.append(slug)
        return {"project": slug, "status": "repaired", "stale": [{"table": "task_plans"}],
                "rebuilt": [{"table": "task_plans", "rows": 1}]}

    monkeypatch.setattr(repair, "repair_project", fake)
    return calls


def _handle(project):
    return conn_mod._databases.get(str(conn_mod._resolve_db_path(project)))


class TestProbeCoversEveryIndex:
    def test_secondary_unique_and_composite_keys_are_all_probed(self, project):
        with connect(project) as conn:
            conn.execute("CREATE TABLE pairs (a VARCHAR, b INTEGER, PRIMARY KEY (a, b))")
            conn.execute("CREATE TABLE uniq (id INTEGER, code VARCHAR UNIQUE)")
            conn.execute("CREATE TABLE plain (id INTEGER, h VARCHAR, n INTEGER)")
            conn.execute("CREATE INDEX idx_plain_h ON plain (h)")
            conn.execute("CREATE INDEX idx_plain_hn ON plain (h, n)")
            conn.execute("CREATE TABLE bare (id INTEGER)")
            tables = {t: (cols, names) for t, cols, names in repair._indexed_tables(conn)}
        assert tables["pairs"][0] == ["a", "b"]
        assert tables["uniq"][0] == ["code"]
        assert tables["plain"][0] == ["h", "n"]
        assert set(tables["plain"][1]) == {"idx_plain_h", "idx_plain_hn"}
        assert "bare" not in tables, "a table with no index has nothing to go stale"
        # v0.11.9 saw only one-column primary keys: none of these three.

    def test_healthy_project_probes_clean(self, project):
        with connect(project) as conn:
            conn.execute("CREATE TABLE plain (id INTEGER, h VARCHAR)")
            conn.execute("CREATE INDEX idx_plain_h ON plain (h)")
            conn.execute("INSERT INTO plain VALUES (1, 'x'), (2, 'y')")
        assert repair.probe(conn_mod._resolve_db_path(project)) == []

    def test_a_failing_child_names_the_table_and_its_indexes(self, project, monkeypatch):
        """A stale ART entry cannot be made on demand, so the child is told to
        fail the way DuckDB does for one table: the verdict is what is tested."""
        with connect(project) as conn:
            conn.execute("CREATE TABLE plain (id INTEGER, h VARCHAR)")
            conn.execute("CREATE INDEX idx_plain_h ON plain (h)")
        monkeypatch.setattr(repair, "_PROBE", (
            "import json, sys\n"
            "for arg in sys.argv[2:]:\n"
            "    t, cols = json.loads(arg)\n"
            "    print('begin', t, flush=True)\n"
            "    if t == 'plain':\n"
            "        sys.exit('duckdb.InternalException: INTERNAL Error: Failed to delete "
            "all rows from index. Only deleted 0 out of 1 rows')\n"
            "    print('ok', t, flush=True)\n"
            "print('closed', flush=True)\n"
        ))
        findings = repair.probe(conn_mod._resolve_db_path(project))
        assert [f["table"] for f in findings] == ["plain"]
        assert findings[0]["indexes"] == ["idx_plain_h"]
        assert findings[0]["columns"] == ["h"]
        assert findings[0]["error"].startswith("INTERNAL Error: Failed to delete")

    def test_the_walk_resumes_after_a_dead_table(self, project, monkeypatch):
        """Two stale tables, one child each time it dies, and the tables after
        them still probed - not one copy per table, and nothing skipped."""
        with connect(project) as conn:
            for name in ("aa", "bb", "cc"):
                conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")
        runs = []
        real_run = repair.subprocess.run

        def counting(*args, **kwargs):
            runs.append(1)
            return real_run(*args, **kwargs)

        monkeypatch.setattr(repair.subprocess, "run", counting)
        monkeypatch.setattr(repair, "_PROBE", (
            "import json, sys, os\n"
            "for arg in sys.argv[2:]:\n"
            "    t, cols = json.loads(arg)\n"
            "    print('begin', t, flush=True)\n"
            "    if t in ('aa', 'cc'):\n"
            "        os.abort()\n"
            "    print('ok', t, flush=True)\n"
            "print('closed', flush=True)\n"
        ))
        findings = repair.probe(conn_mod._resolve_db_path(project))
        assert [f["table"] for f in findings] == ["aa", "cc"]
        assert findings[0]["returncode"] != 0
        assert len(runs) == 3, "one child to start, one more per table that killed it"

    def test_rebuilding_a_secondary_indexed_table_keeps_its_index(self, project):
        with connect(project) as conn:
            conn.execute("CREATE TABLE plain (id INTEGER, h VARCHAR)")
            conn.execute("CREATE INDEX idx_plain_h ON plain (h)")
            conn.execute("INSERT INTO plain VALUES (1, 'x')")
            assert repair.rebuild_table(conn, "plain") == 1
            assert conn.execute(
                "SELECT count(*) FROM duckdb_indexes() WHERE index_name = 'idx_plain_h'"
            ).fetchone()[0] == 1


def _indexes(conn, table):
    return {r[0] for r in conn.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name = ?", [table]
    ).fetchall()}


class TestSchemaV20:
    def test_queue_like_tables_carry_no_index(self, project):
        with connect(project) as conn:
            for table in ("task_plans", "attachment_inbox", "task_outbox"):
                assert _indexes(conn, table) == set(), table

    def test_v19_database_loses_them_on_upgrade(self, tmp_path):
        conn = duckdb.connect(str(tmp_path / "v19.duckdb"))
        try:
            create_schema(conn)
            conn.execute("CREATE INDEX idx_task_plans_hash ON task_plans (request_hash)")
            conn.execute("CREATE INDEX idx_attach_inbox_id ON attachment_inbox (id)")
            conn.execute("INSERT INTO task_plans (id, request_hash, task_ids) VALUES "
                         "('p', 'h', ['t'])")
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (19)")
            assert get_schema_version(conn) == 19
            assert run_migrations(conn) == CURRENT_SCHEMA_VERSION >= 20
            assert _indexes(conn, "task_plans") == set()
            assert _indexes(conn, "attachment_inbox") == set()
            assert conn.execute("SELECT task_ids FROM task_plans").fetchall() == [(["t"],)]
            assert run_migrations(conn) == CURRENT_SCHEMA_VERSION, "idempotent"
        finally:
            conn.close()


class TestIsFatal:
    def test_the_shapes_duckdb_raises(self):
        assert is_fatal(duckdb.FatalException("x"))
        assert is_fatal(duckdb.InternalException("Failed to delete all rows from index"))
        assert is_fatal(duckdb.Error(INVALIDATED))
        assert not is_fatal(duckdb.ConstraintException("duplicate key"))
        assert not is_fatal(ValueError("nope"))

    def test_a_wrapped_one_counts(self):
        try:
            try:
                raise duckdb.FatalException("x")
            except duckdb.FatalException as inner:
                raise RuntimeError("the plan was ROLLED BACK") from inner
        except RuntimeError as outer:
            assert is_fatal(outer)


class TestHealing:
    def test_a_fatal_error_replaces_the_handle_and_repairs(self, project, repairs):
        with connect(project) as conn:
            conn.execute("CREATE TABLE keep (n INTEGER)")
            conn.execute("INSERT INTO keep VALUES (7)")
        dead = _handle(project)
        with pytest.raises(duckdb.FatalException):
            with connect(project):
                raise duckdb.FatalException(INVALIDATED)
        assert repairs == [project]
        with connect(project) as conn:
            assert conn.execute("SELECT n FROM keep").fetchall() == [(7,)]
        assert _handle(project) is not dead, "the dead handle must not be served again"

    def test_a_fatal_commit_heals_too(self, project, repairs):
        with pytest.raises(duckdb.InternalException):
            with transaction(project):
                raise duckdb.InternalException("Failed to delete all rows from index")
        assert repairs == [project]

    def test_an_ordinary_error_heals_nothing(self, project, repairs):
        with connect(project):
            pass
        handle = _handle(project)
        with pytest.raises(duckdb.ConstraintException):
            with connect(project):
                raise duckdb.ConstraintException("duplicate key")
        assert repairs == []
        assert _handle(project) is handle

    def test_it_is_logged_loudly_with_the_table(self, project, repairs, caplog):
        with caplog.at_level("CRITICAL"), pytest.raises(duckdb.InternalException):
            with connect(project):
                raise duckdb.InternalException(
                    "INTERNAL Error: Failed to append to PRIMARY_tasks_0: duplicate key")
        text = caplog.text
        assert "DATABASE INVALIDATED" in text and project in text
        assert "on tasks" in text
        assert "rebuilt ['task_plans']" in text

    def test_the_fatal_is_recorded_for_the_next_startup(self, project, repairs):
        with pytest.raises(duckdb.FatalException):
            with connect(project):
                raise duckdb.FatalException("idx_task_plans_hash: Failed to delete")
        events = json.loads((settings.data_dir / repair.FATAL_MARKER).read_text())
        assert events[-1]["project"] == project
        assert events[-1]["where"] == "idx_task_plans_hash"

    def test_the_reopen_waits_for_cursors_still_on_the_dead_handle(
        self, project, repairs, monkeypatch,
    ):
        """DuckDB hands back the SAME invalidated instance while any connection
        to it is open, so the heal must not reopen before they close."""
        monkeypatch.setattr(conn_mod, "HEAL_DRAIN_SECONDS", 5.0)
        holding, release = threading.Event(), threading.Event()
        closed_at = []

        def straggler():
            with connect(project):
                holding.set()
                release.wait(5)
            closed_at.append(time.monotonic())

        t = threading.Thread(target=straggler)
        t.start()
        holding.wait(5)
        threading.Timer(0.5, release.set).start()
        with pytest.raises(duckdb.FatalException):
            with connect(project):
                raise duckdb.FatalException(INVALIDATED)
        healed_at = time.monotonic()
        t.join(5)
        assert closed_at and closed_at[0] <= healed_at
        assert repairs == [project]

    def test_new_operations_wait_for_the_heal(self, project, monkeypatch):
        started, finish = threading.Event(), threading.Event()

        def slow_repair(slug, check_only=False):
            started.set()
            finish.wait(5)
            return {"project": slug, "status": "ok", "stale": [], "rebuilt": []}

        monkeypatch.setattr(repair, "repair_project", slow_repair)

        def fail():
            with pytest.raises(duckdb.FatalException):
                with connect(project):
                    raise duckdb.FatalException(INVALIDATED)

        healer = threading.Thread(target=fail)
        healer.start()
        started.wait(5)
        threading.Timer(0.4, finish.set).start()
        before = time.monotonic()
        with connect(project) as conn:
            conn.execute("SELECT 1")
        assert time.monotonic() - before >= 0.3, "it ran against the handle being healed"
        healer.join(5)


class TestToolsRetryOnce:
    def test_a_fatal_call_is_retried_once_after_the_heal(self):
        calls = []

        def body():
            calls.append(1)
            if len(calls) == 1:
                raise duckdb.FatalException(INVALIDATED)
            return {"ok": True}

        assert server._safe(body, _tool="memory_task_list") == {"ok": True}
        assert len(calls) == 2

    def test_only_once(self):
        calls = []

        def body():
            calls.append(1)
            raise duckdb.FatalException(INVALIDATED)

        answer = server._safe(body, _tool="memory_task_list")
        assert answer["type"] == "FatalException"
        assert len(calls) == 2

    def test_a_tool_of_several_writes_is_not_retried(self):
        """memory_task_comment commits the comment, then records and enqueues:
        re-running it after a fatal error on the second step doubles the
        comment, here and on the board."""
        calls = []

        def body():
            calls.append(1)
            raise duckdb.FatalException(INVALIDATED)

        answer = server._safe(body, _tool="memory_task_comment")
        assert len(calls) == 1
        assert "reopened and repaired" in answer["error"]

    def test_the_tool_name_comes_from_the_caller(self):
        calls = []

        def memory_task_get():
            def body():
                calls.append(1)
                raise duckdb.FatalException(INVALIDATED)
            return server._safe(body)

        memory_task_get()
        assert len(calls) == 2

    def test_an_ordinary_error_is_not_retried(self):
        calls = []

        def body():
            calls.append(1)
            raise ValueError("bad input")

        assert server._safe(body, _tool="memory_task_list") == {"error": "bad input", "type": "ValueError"}
        assert len(calls) == 1

    def test_a_plan_retried_after_a_fatal_record_creates_one_set(self, project, repairs):
        """End to end: the purge fails fatally, the plan rolls back whole, the
        retry creates exactly one set."""
        from memory_mcp.services.task_planner import TaskPlanner

        real = container.task_service
        failures = []

        class FirstRecordFails:
            def record_plan(self, *args, **kwargs):
                if not failures:
                    failures.append(1)
                    raise duckdb.InternalException("Failed to delete all rows from index")
                return real.record_plan(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(real, name)

        planner = TaskPlanner(FirstRecordFails())
        items = [{"title": "One", "description": "a"}, {"title": "Two", "description": "b"}]
        answer = server._safe(
            lambda: planner.plan(project, "one and two", items), _tool="memory_task_plan")
        assert answer.get("count") == 2, answer
        with connect(project) as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
            assert conn.execute("SELECT count(*) FROM task_plans").fetchone()[0] == 1


class TestStartupCheck:
    def test_a_clean_restart_checks_nothing(self):
        assert repair.startup_reason() is None, "first start: no sentinel, no marker"
        repair.mark_clean_shutdown()
        assert repair.startup_reason() is None

    def test_a_death_is_noticed(self):
        repair.startup_reason()  # this "process" starts...
        # ...and dies without mark_clean_shutdown.
        assert "did not shut down cleanly" in (repair.startup_reason() or "")

    def test_a_recorded_fatal_is_noticed(self):
        repair.record_fatal("kalagh", "InternalException: Failed to delete", "idx_task_plans_hash")
        assert "fatal error" in (repair.startup_reason() or "")

    def test_the_check_repairs_every_project_and_clears_the_marker(
        self, project, monkeypatch, caplog,
    ):
        repair.record_fatal(project, "x")
        verdicts = iter([[{"table": "tasks", "key": "id", "returncode": 1, "error": "x"}], []])
        monkeypatch.setattr(repair, "probe", lambda path: next(verdicts))
        with caplog.at_level("WARNING"):
            results = repair.startup_check([project])
        assert results[0]["status"] == "repaired"
        assert "rebuilt ['tasks']" in caplog.text
        assert not (settings.data_dir / repair.FATAL_MARKER).exists()

    def test_an_unfinished_check_keeps_the_marker(self, project, monkeypatch):
        repair.record_fatal(project, "x")
        monkeypatch.setattr(repair, "repair_project", lambda slug: {"status": "still_stale"})
        repair.startup_check([project])
        assert (settings.data_dir / repair.FATAL_MARKER).exists()


class TestReviewFindings:
    """The release review of v0.11.10, one test per finding it reproduced."""

    def test_a_death_after_the_last_table_blames_no_table(self, project, monkeypatch):
        """Dying on close used to blame pending[0] - every table, one by one."""
        monkeypatch.setattr(repair, "_PROBE", (
            "import json, os, sys\n"
            "for arg in sys.argv[2:]:\n"
            "    t, cols = json.loads(arg)\n"
            "    print('begin', t, flush=True); print('ok', t, flush=True)\n"
            "os._exit(1)\n"
        ))
        findings = repair.probe(conn_mod._resolve_db_path(project))
        assert [f["table"] for f in findings] == [None]
        result = repair.repair_project(project)
        assert result["status"] == "probe_error" and result["rebuilt"] == []

    def test_the_real_child_checkpoints_a_copied_wal_first(self, project):
        """The copy carries the WAL; the child's close must not be the first
        checkpoint after replaying it (duckdb#26106)."""
        assert "c.execute('CHECKPOINT')" in repair._PROBE
        assert repair._PROBE.index("CHECKPOINT") < repair._PROBE.index("for arg")

    def test_a_rebuild_waits_for_running_writers_and_loses_nothing(self, project):
        with connect(project) as conn:
            conn.execute("CREATE TABLE r (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO r VALUES (1), (2)")
        inside, release = threading.Event(), threading.Event()

        def writer():
            with connect(project) as conn:
                inside.set()
                release.wait(5)
                conn.execute("INSERT INTO r VALUES (3)")

        t = threading.Thread(target=writer)
        t.start()
        inside.wait(5)
        threading.Timer(0.3, release.set).start()
        path = conn_mod._resolve_db_path(project)
        with conn_mod.paused(path), transaction(project) as conn:
            repair.rebuild_table(conn, "r")
        t.join(5)
        with connect(project) as conn:
            assert [r[0] for r in conn.execute("SELECT id FROM r ORDER BY id").fetchall()] == [1, 2, 3]

    def test_new_writers_wait_for_the_rebuild(self, project):
        with connect(project) as conn:
            conn.execute("CREATE TABLE r (id INTEGER PRIMARY KEY)")
        path = conn_mod._resolve_db_path(project)
        done = []

        def writer():
            with connect(project) as conn:
                conn.execute("INSERT INTO r VALUES (9)")
            done.append(time.monotonic())

        with conn_mod.paused(path), transaction(project) as conn:
            t = threading.Thread(target=writer)
            t.start()
            time.sleep(0.3)
            assert not done, "a writer ran while the table was being swapped"
            repair.rebuild_table(conn, "r")
        t.join(5)
        with connect(project) as conn:
            assert conn.execute("SELECT id FROM r").fetchall() == [(9,)]

    def test_release_refuses_to_close_under_a_running_query(self, project, monkeypatch):
        monkeypatch.setattr(conn_mod, "CLOSE_DRAIN_SECONDS", 0.3)
        path = conn_mod._resolve_db_path(project)
        with connect(project) as conn:
            with pytest.raises(RuntimeError, match="busy"):
                conn_mod.release_database(path)
            assert conn.execute("SELECT 2").fetchone() == (2,)

    def test_a_busy_heal_never_serves_the_dead_handle(self, project, repairs, monkeypatch):
        monkeypatch.setattr(conn_mod, "HEAL_DRAIN_SECONDS", 0.3)
        holding, release = threading.Event(), threading.Event()

        def straggler():
            with connect(project):
                holding.set()
                release.wait(5)

        t = threading.Thread(target=straggler)
        t.start()
        holding.wait(5)
        dead = _handle(project)
        with pytest.raises(duckdb.FatalException):
            with connect(project):
                raise duckdb.FatalException(INVALIDATED)
        assert repairs == [], "no repair against a file still on its dead handle"
        assert _handle(project) is not dead
        release.set()
        t.join(5)

    def test_a_constraint_error_echoing_the_phrase_is_not_fatal(self, project):
        with connect(project) as conn:
            conn.execute("CREATE TABLE u (id VARCHAR PRIMARY KEY)")
            phrase = "database has been invalidated"
            conn.execute("INSERT INTO u VALUES (?)", [phrase])
            with pytest.raises(duckdb.ConstraintException) as exc:
                conn.execute("INSERT INTO u VALUES (?)", [phrase])
        assert not is_fatal(exc.value)

    def test_a_fatal_recorded_during_the_check_survives_it(self, project, monkeypatch):
        repair.record_fatal(project, "before")

        def repair_and_meanwhile_fail(slug, check_only=False):
            repair.record_fatal("other", "during")
            return {"project": slug, "status": "ok", "stale": [], "rebuilt": []}

        monkeypatch.setattr(repair, "repair_project", repair_and_meanwhile_fail)
        repair.startup_check([project])
        events = json.loads((settings.data_dir / repair.FATAL_MARKER).read_text())
        assert [e["project"] for e in events] == ["other"]

    def test_concurrent_fatals_both_land(self):
        threads = [threading.Thread(target=repair.record_fatal, args=(f"p{i}", "x"))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        events = json.loads((settings.data_dir / repair.FATAL_MARKER).read_text())
        assert sorted(e["project"] for e in events) == [f"p{i}" for i in range(8)]
