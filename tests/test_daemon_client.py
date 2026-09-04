"""Asking the daemon instead of fighting it for the DuckDB write lock.

The bug this closes: `memory-mcp asoode push` created tasks on the board and
then died recording the mapping, because the daemon holds the database open.
Half-done work with a traceback for an explanation.
"""

import duckdb
import pytest

from memory_mcp import daemon_client
from memory_mcp.daemon_client import DaemonError, DaemonUnavailable


class TestPreferringTheDaemon:
    def test_the_daemon_answers_and_nothing_is_done_locally(self, monkeypatch):
        called = []
        monkeypatch.setattr(daemon_client, "call", lambda *a, **k: {"ok": True})
        result, via = daemon_client.run("/x", "POST", {}, lambda: called.append(1))
        assert result == {"ok": True}
        assert via is True
        assert called == [], "the local path must not run as well"

    def test_no_daemon_falls_back_to_local(self, monkeypatch):
        def boom(*a, **k):
            raise DaemonUnavailable("connection refused")

        monkeypatch.setattr(daemon_client, "call", boom)
        result, via = daemon_client.run("/x", "POST", {}, lambda: {"local": True})
        assert result == {"local": True}
        assert via is False

    def test_a_daemon_failure_does_not_retry_locally(self, monkeypatch):
        """It reached the process that owns the lock and was refused there.

        Retrying locally would either fail identically or fail on the lock and
        report that instead of the real reason."""
        def boom(*a, **k):
            raise DaemonError("daemon returned 500: no link for this project")

        monkeypatch.setattr(daemon_client, "call", boom)
        with pytest.raises(DaemonError):
            daemon_client.run("/x", "POST", {}, lambda: {"local": True})

    def test_is_running_is_false_when_nothing_answers(self, monkeypatch):
        def boom(*a, **k):
            raise DaemonUnavailable("nope")

        monkeypatch.setattr(daemon_client, "call", boom)
        assert daemon_client.is_running() is False


class TestExplainingTheLock:
    """DuckDB names a PID and links to its concurrency docs; neither tells you
    the daemon is the holder or what to do about it."""

    def test_it_recognises_the_duckdb_wording(self):
        err = duckdb.IOException(
            'IO Error: Could not set lock on file "x.duckdb": Conflicting lock '
            "is held in /usr/bin/python3 (PID 86196) by user someone."
        )
        msg = daemon_client.lock_message(err)
        assert msg and "daemon" in msg

    def test_it_offers_both_ways_out(self):
        err = RuntimeError("Conflicting lock is held")
        msg = daemon_client.lock_message(err)
        assert "127.0.0.1" in msg and "launchctl" in msg

    def test_an_unrelated_error_is_left_alone(self):
        assert daemon_client.lock_message(RuntimeError("disk full")) is None

    def test_a_real_io_error_is_not_swallowed(self):
        """Only the lock is translated - a genuine disk problem must keep its
        own message."""
        assert daemon_client.lock_message(duckdb.IOException("No such file")) is None


class TestTheConnectionExplainsItself:
    """The floor: any path that still opens the database directly - a CLI
    command not routed through the daemon, a script - gets the explanation
    rather than a DuckDB traceback."""

    def test_a_lock_conflict_is_translated(self, monkeypatch):
        from memory_mcp.db import connection

        monkeypatch.setattr(connection, "_ensure_initialized", lambda p: None)

        def locked(*a, **k):
            raise duckdb.IOException(
                'IO Error: Could not set lock on file "p.duckdb": Conflicting '
                "lock is held in /usr/bin/python3 (PID 1) by user someone."
            )

        monkeypatch.setattr(connection.duckdb, "connect", locked)
        with pytest.raises(duckdb.IOException) as caught:
            connection.get_connection("anything")
        assert "daemon" in str(caught.value)
        assert "launchctl" in str(caught.value)

    def test_other_io_errors_keep_their_own_message(self, monkeypatch):
        from memory_mcp.db import connection

        monkeypatch.setattr(connection, "_ensure_initialized", lambda p: None)

        def broken(*a, **k):
            raise duckdb.IOException("IO Error: No space left on device")

        monkeypatch.setattr(connection.duckdb, "connect", broken)
        with pytest.raises(duckdb.IOException) as caught:
            connection.get_connection("anything")
        assert "No space left" in str(caught.value)
        assert "daemon" not in str(caught.value)
