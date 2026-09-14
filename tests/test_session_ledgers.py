"""The three things the daemon now remembers about a Claude Code session.

`client_sessions`, `session_dispatches` and `session_edits` live in the SQLite
registry, keyed on the session id every hook payload carries. They exist so a
session can be told what it already did: which agents it dispatched, which files
it edited, and where its transcript is.

Two properties matter more than the round trips. First, NOTHING here may fail a
hook - every accessor swallows its own errors, because the alternative is a gate
that blocks an edit over a locked database. Second, `first_seen` survives: a
session is seen dozens of times per turn and the first sighting is the one that
says when it started.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from memory_mcp.db.registry import (
    client_session, dispatches_for, edits_for, prune_session_ledgers,
    record_dispatch, record_edit, registry_conn, touch_client_session,
)


def _age_row(table: str, column: str, days: int) -> None:
    """Backdate everything in a ledger, so prune has something old to find."""
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with registry_conn() as conn:
        conn.execute(f"UPDATE {table} SET {column} = ?", (old,))


class TestClientSessions:
    def test_a_session_round_trips(self):
        touch_client_session(
            "s1", slug="proj", cwd="/repo", transcript_path="/t/s1.jsonl",
        )
        row = client_session("s1")

        assert row["slug"] == "proj"
        assert row["cwd"] == "/repo"
        assert row["transcript_path"] == "/t/s1.jsonl"
        assert row["first_seen"] == row["last_seen"]

    def test_an_unknown_session_is_None(self):
        assert client_session("never-seen") is None
        assert client_session("") is None

    def test_touching_again_keeps_first_seen_and_moves_last_seen(self):
        touch_client_session("s1", slug="proj", cwd="/repo")
        first = client_session("s1")["first_seen"]
        _age_row("client_sessions", "last_seen", days=0)

        touch_client_session("s1", slug="proj", cwd="/repo")
        row = client_session("s1")

        assert row["first_seen"] == first
        assert row["last_seen"] >= first

    def test_a_later_hook_with_less_information_erases_nothing(self):
        """The gate knows the cwd but not the transcript; the prompt hook knows
        both. Whichever fires second must not blank what the other learned."""
        touch_client_session("s1", slug="proj", cwd="/repo", transcript_path="/t.jsonl")
        touch_client_session("s1")

        row = client_session("s1")
        assert row["transcript_path"] == "/t.jsonl"
        assert row["cwd"] == "/repo"
        assert row["slug"] == "proj"

    def test_a_later_hook_fills_in_what_the_first_did_not_know(self):
        touch_client_session("s1", cwd="/repo")
        touch_client_session("s1", slug="proj", transcript_path="/t.jsonl")

        row = client_session("s1")
        assert (row["slug"], row["transcript_path"]) == ("proj", "/t.jsonl")

    def test_no_session_id_records_nothing(self):
        touch_client_session("", slug="proj", cwd="/repo")

        with registry_conn() as conn:
            assert conn.execute("SELECT count(*) FROM client_sessions").fetchone()[0] == 0


class TestDispatchLedger:
    def test_a_dispatch_round_trips(self):
        record_dispatch(
            "s1", slug="proj", agent_type="python", tool_use_id="t1",
            description="Do the thing (python)",
        )
        rows = dispatches_for("s1")

        assert len(rows) == 1
        assert rows[0]["agent_type"] == "python"
        assert rows[0]["tool_use_id"] == "t1"
        assert rows[0]["description"] == "Do the thing (python)"
        assert rows[0]["slug"] == "proj"

    def test_the_same_agent_twice_is_two_rows_in_order(self):
        """Append-only: the question asked of this table is 'was R ever dispatched
        this session', and how often is worth keeping too."""
        record_dispatch("s1", agent_type="python")
        record_dispatch("s1", agent_type="react")
        record_dispatch("s1", agent_type="python")

        assert [r["agent_type"] for r in dispatches_for("s1")] == [
            "python", "react", "python",
        ]

    def test_sessions_do_not_see_each_others_dispatches(self):
        record_dispatch("s1", agent_type="python")
        record_dispatch("s2", agent_type="react")

        assert [r["agent_type"] for r in dispatches_for("s1")] == ["python"]
        assert [r["agent_type"] for r in dispatches_for("s2")] == ["react"]

    def test_an_unknown_session_has_no_dispatches(self):
        assert dispatches_for("nope") == []
        assert dispatches_for("") == []

    def test_a_dispatch_with_no_session_or_no_agent_type_is_dropped(self):
        record_dispatch("", agent_type="python")
        record_dispatch("s1", agent_type="")

        with registry_conn() as conn:
            assert conn.execute(
                "SELECT count(*) FROM session_dispatches"
            ).fetchone()[0] == 0


class TestEditLedger:
    def test_an_edit_round_trips(self):
        record_edit("s1", slug="proj", path="/repo/src/x.py", tool="Write")
        rows = edits_for("s1")

        assert len(rows) == 1
        assert rows[0]["path"] == "/repo/src/x.py"
        assert rows[0]["tool"] == "Write"
        assert rows[0]["by_agent"] is None

    def test_an_agents_edit_names_the_agent(self):
        record_edit("s1", path="/repo/a.py", tool="Edit", by_agent="python")

        assert edits_for("s1")[0]["by_agent"] == "python"

    def test_the_leads_edits_are_the_ones_with_a_null_by_agent(self):
        record_edit("s1", path="/repo/a.py", tool="Edit")
        record_edit("s1", path="/repo/b.py", tool="Edit", by_agent="python")

        lead = [r["path"] for r in edits_for("s1") if r["by_agent"] is None]
        assert lead == ["/repo/a.py"]

    def test_an_edit_with_no_path_is_dropped(self):
        record_edit("s1", path="", tool="Write")

        assert edits_for("s1") == []


class TestPruning:
    def test_old_rows_go_and_recent_ones_stay(self):
        record_dispatch("old", agent_type="python")
        record_edit("old", path="/repo/a.py", tool="Edit")
        touch_client_session("old", cwd="/repo")
        _age_row("session_dispatches", "at", days=30)
        _age_row("session_edits", "at", days=30)
        _age_row("client_sessions", "last_seen", days=30)

        record_dispatch("new", agent_type="react")
        record_edit("new", path="/repo/b.py", tool="Write")
        touch_client_session("new", cwd="/repo")

        deleted = prune_session_ledgers(days=7)

        assert deleted == 3
        assert dispatches_for("old") == []
        assert edits_for("old") == []
        assert client_session("old") is None
        assert len(dispatches_for("new")) == 1
        assert len(edits_for("new")) == 1
        assert client_session("new") is not None

    def test_pruning_an_empty_registry_is_a_no_op(self):
        assert prune_session_ledgers() == 0


class TestFailingSoft:
    """A hook runs before every edit. A ledger may lose a row; it may never raise."""

    def test_a_registry_that_will_not_open_does_not_raise(self, monkeypatch):
        """sqlite cannot connect at all - a wrong permission, a missing dir."""
        def _no_connect(*_args, **_kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr("memory_mcp.db.registry.sqlite3.connect", _no_connect)

        touch_client_session("s1", cwd="/repo")
        record_dispatch("s1", agent_type="python")
        record_edit("s1", path="/repo/a.py", tool="Edit")
        assert client_session("s1") is None
        assert dispatches_for("s1") == []
        assert edits_for("s1") == []
        assert prune_session_ledgers() == 0

    def test_a_locked_registry_does_not_raise(self, monkeypatch):
        """The real-world failure: the daemon holds a write while a hook fires."""
        monkeypatch.setattr("memory_mcp.db.registry.registry_conn", _exploding_conn)

        touch_client_session("s1", cwd="/repo")
        record_dispatch("s1", agent_type="python")
        record_edit("s1", path="/repo/a.py", tool="Edit")
        assert client_session("s1") is None
        assert dispatches_for("s1") == []
        assert prune_session_ledgers() == 0


def _exploding_conn(*_args, **_kwargs):
    raise sqlite3.OperationalError("database is locked")
