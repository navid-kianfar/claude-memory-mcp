"""One transaction across connection-per-operation repositories.

The repositories each open their own `connect(project)` and close it - that is
the whole design, and it is why an operation spanning several of them could be
half-applied. `transaction(slug)` publishes an ambient connection that every
`connect(slug)` in the same context joins, so they become atomic together
without a single repository signature changing.

The properties asserted here are the ones the design rests on; several were
measured against duckdb 1.5.1 before being relied on.
"""

import threading

import pytest

from memory_mcp.container import container
from memory_mcp.db.connection import (
    after_commit, connect, get_connection, in_transaction, transaction,
)


@pytest.fixture
def project():
    slug = "txn-test"
    container.project_service.init_project(slug, "Transaction Test")
    with connect(slug) as conn:
        conn.execute("CREATE TABLE probe (id INTEGER)")
    return slug


def rows(slug: str) -> list[int]:
    with connect(slug) as conn:
        return [r[0] for r in conn.execute("SELECT id FROM probe ORDER BY id").fetchall()]


class TestAtomicity:
    def test_a_committed_block_persists(self, project):
        with transaction(project) as conn:
            conn.execute("INSERT INTO probe VALUES (1)")
            conn.execute("INSERT INTO probe VALUES (2)")
        assert rows(project) == [1, 2]

    def test_a_failure_rolls_the_whole_block_back(self, project):
        with pytest.raises(RuntimeError, match="halfway"):
            with transaction(project) as conn:
                conn.execute("INSERT INTO probe VALUES (1)")
                conn.execute("INSERT INTO probe VALUES (2)")
                raise RuntimeError("halfway")
        assert rows(project) == [], "a partial write is exactly what this prevents"

    def test_separate_connect_calls_join_the_transaction(self, project):
        """The point of the whole design: repositories keep `with connect(...)`."""
        with transaction(project) as outer:
            with connect(project) as a:
                a.execute("INSERT INTO probe VALUES (1)")
            with connect(project) as b:
                b.execute("INSERT INTO probe VALUES (2)")
                # Same physical connection, so the second sees the first's
                # uncommitted row. next_position depends on precisely this: on a
                # separate connection it would be blind to uncommitted siblings
                # and hand every task in a plan the same position.
                assert b.execute("SELECT count(*) FROM probe").fetchone()[0] == 2
            assert a is outer and b is outer
        assert rows(project) == [1, 2]

    def test_a_joined_connect_does_not_close_the_transaction(self, project):
        """`connect` closing the ambient connection would break everything after it."""
        with transaction(project) as conn:
            with connect(project):
                pass
            conn.execute("INSERT INTO probe VALUES (1)")
        assert rows(project) == [1]

    def test_rollback_does_not_touch_another_project(self, project):
        other = "txn-test-other"
        container.project_service.init_project(other, "Other")
        with connect(other) as conn:
            conn.execute("CREATE TABLE probe (id INTEGER)")

        with pytest.raises(RuntimeError):
            with transaction(project) as conn:
                conn.execute("INSERT INTO probe VALUES (1)")
                with connect(other) as unrelated:
                    unrelated.execute("INSERT INTO probe VALUES (99)")
                raise RuntimeError("boom")

        assert rows(project) == []
        assert rows(other) == [99], "one project's rollback is not another's"

    def test_it_is_re_entrant(self, project):
        """DuckDB raises TransactionException on a nested BEGIN, so we must join."""
        with transaction(project) as outer:
            with transaction(project) as inner:
                inner.execute("INSERT INTO probe VALUES (1)")
            assert inner is outer
            assert in_transaction(project)
        assert rows(project) == [1]

    def test_an_inner_block_rolls_back_with_the_outer_one(self, project):
        with pytest.raises(RuntimeError):
            with transaction(project):
                with transaction(project) as inner:
                    inner.execute("INSERT INTO probe VALUES (1)")
                raise RuntimeError("boom")
        assert rows(project) == [], "the inner block did not commit on its own"

    def test_in_transaction_is_scoped_to_the_slug(self, project):
        assert in_transaction(project) is False
        with transaction(project):
            assert in_transaction(project) is True
            assert in_transaction("some-other-project") is False
        assert in_transaction(project) is False

    def test_get_connection_never_joins(self, project):
        """It hands ownership to a caller who closes it; closing ours mid-flight
        would be a new bug."""
        with transaction(project) as ambient:
            own = get_connection(project)
            try:
                assert own is not ambient
            finally:
                own.close()
            ambient.execute("INSERT INTO probe VALUES (1)")
        assert rows(project) == [1]


class TestTheFlusherThreadCannotStealTheConnection:
    """Why a contextvar and not a thread-local or a module global.

    Container._mirror_soon spawns a daemon thread on every task mutation, and a
    DuckDB connection is not safe to share across threads. A new thread starts
    from an empty context, so it can never pick ours up.
    """

    def test_a_new_thread_gets_its_own_connection(self, project):
        seen = {}

        def worker():
            seen["in_transaction"] = in_transaction(project)
            with connect(project) as conn:
                seen["conn"] = conn
                seen["visible"] = conn.execute("SELECT count(*) FROM probe").fetchone()[0]

        with transaction(project) as ambient:
            ambient.execute("INSERT INTO probe VALUES (1)")
            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=10)
            assert not t.is_alive(), "the flusher must not block on our transaction"

        assert seen["in_transaction"] is False
        assert seen["conn"] is not ambient
        assert seen["visible"] == 0, "uncommitted rows are ours alone until commit"


class TestAfterCommit:
    """For side effects a ROLLBACK cannot undo - an HTTP call, a spawned thread."""

    def test_it_declines_outside_a_transaction(self):
        assert after_commit(lambda: None) is False, "caller should just run it now"

    def test_it_runs_after_the_commit_not_during(self, project):
        order = []
        with transaction(project) as conn:
            assert after_commit(lambda: order.append("hook")) is True
            conn.execute("INSERT INTO probe VALUES (1)")
            order.append("still inside")
        assert order == ["still inside", "hook"]

    def test_a_hook_sees_committed_rows(self, project):
        seen = []
        with transaction(project) as conn:
            conn.execute("INSERT INTO probe VALUES (1)")
            after_commit(lambda: seen.append(rows(project)))
        assert seen == [[1]], "the rows are real by the time the side effect runs"

    def test_a_rollback_runs_nothing(self, project):
        ran = []
        with pytest.raises(RuntimeError):
            with transaction(project) as conn:
                conn.execute("INSERT INTO probe VALUES (1)")
                after_commit(lambda: ran.append(1))
                raise RuntimeError("boom")
        assert ran == [], "the rows never existed; neither may the side effect"

    def test_a_key_de_duplicates(self, project):
        ran = []
        with transaction(project):
            for _ in range(9):
                after_commit(lambda: ran.append(1), key=("mirror", project))
        assert ran == [1], "a nine-task plan nudges the mirror once, not nine times"

    def test_a_failing_hook_does_not_undo_the_commit(self, project):
        def explode():
            raise RuntimeError("mirror down")

        with transaction(project) as conn:
            conn.execute("INSERT INTO probe VALUES (1)")
            after_commit(explode)
        assert rows(project) == [1]
