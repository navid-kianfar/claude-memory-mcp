"""Only the lead dispatches, and it dispatches a few agents at a time.

Stated by the user on 2026-09-14, after a session hit its usage limit: "i say
around 15+ review agents each with 300k, 180k, etc... token usage running for 30
minutes. you must be intelligent about the agents you run." The measured cause:
a reviewer spawned six "angle" reviewers, one of those four more - subagents
could dispatch. Three guards, tested here from the ledger up:

- a SUBAGENT's dispatch is denied at the hook (the second guard; the first is
  `disallowedTools: Agent` in every definition, tested in test_agent_install);
- a third agent while two are RUNNING asks the user;
- running = dispatched - stopped, and a prompted dispatch is not counted as
  running, so a declined prompt cannot leave a phantom agent behind.
"""

import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memory_mcp.db.registry import (
    RUNNING_WINDOW_MINUTES, dispatches_for, prune_session_ledgers, record_dispatch,
    record_subagent_stop, registry_conn, running_dispatches,
)
from memory_mcp.enforcement import (
    MAX_RUNNING_AGENTS, combine_gates, concurrency_gate, nested_dispatch_denial,
)
from tests.test_require_task_gate import _stub_daemon

STOP_HOOK = (
    Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "record-subagent-stop.sh"
)


def _backdate(table: str, minutes: int) -> None:
    old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with registry_conn() as conn:
        conn.execute(f"UPDATE {table} SET at = ?", (old,))


# ---------- the ledger: how many are running ----------


class TestRunningDispatches:
    def test_dispatched_minus_stopped(self):
        for _ in range(3):
            record_dispatch("s1", agent_type="python")
        record_subagent_stop("s1", agent_id="a1")

        assert running_dispatches("s1") == 2

    def test_it_never_goes_negative(self):
        record_subagent_stop("s1")
        record_subagent_stop("s1")

        assert running_dispatches("s1") == 0

    def test_sessions_do_not_count_each_other(self):
        record_dispatch("s1", agent_type="python")
        record_dispatch("s2", agent_type="python")

        assert running_dispatches("s1") == 1

    def test_a_dispatch_older_than_the_window_is_presumed_finished(self):
        """Bounds a missed SubagentStop: it costs at most an hour, not forever."""
        record_dispatch("s1", agent_type="python")
        _backdate("session_dispatches", RUNNING_WINDOW_MINUTES + 5)

        assert running_dispatches("s1") == 0

    def test_a_prompted_dispatch_is_history_but_not_running(self):
        """PreToolUse fires before the user answers. A declined prompt must not
        leave a phantom agent that makes every dispatch for an hour prompt."""
        record_dispatch("s1", agent_type="backend", asked=True)

        assert running_dispatches("s1") == 0
        assert [r["agent_type"] for r in dispatches_for("s1")] == ["backend"]

    def test_no_session_is_zero(self):
        assert running_dispatches("") == 0

    def test_prune_drops_old_stops_too(self):
        record_subagent_stop("s1")
        with registry_conn() as conn:
            conn.execute(
                "UPDATE session_subagent_stops SET at = ?",
                ((datetime.now(timezone.utc) - timedelta(days=9)).isoformat(),),
            )

        assert prune_session_ledgers(days=7) >= 1
        with registry_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM session_subagent_stops").fetchone()[0] == 0


class TestAnOlderRegistry:
    def test_a_ledger_without_the_asked_column_gains_it(self, tmp_path):
        """A release candidate created session_dispatches before `asked` existed.
        Without the column every INSERT fails - silently, since the accessors
        swallow errors - and the ledger would simply stop filling."""
        from memory_mcp.db import registry

        with registry_conn() as conn:
            conn.execute("DROP TABLE session_dispatches")
            conn.execute(
                "CREATE TABLE session_dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT NOT NULL, slug TEXT, agent_type TEXT NOT NULL, "
                "tool_use_id TEXT, description TEXT, at TEXT NOT NULL)"
            )
            registry._ensure_columns(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(session_dispatches)")}

        assert "asked" in cols
        record_dispatch("s1", agent_type="python")
        assert running_dispatches("s1") == 1


# ---------- the gates ----------


class TestNestedDispatch:
    def test_a_subagent_is_denied(self):
        answer = nested_dispatch_denial("agent-abc")

        assert answer["decision"] == "deny"
        assert "Only the lead" in answer["reason"]

    def test_the_lead_is_not(self):
        assert nested_dispatch_denial("") == {}
        assert nested_dispatch_denial(None) == {}
        assert nested_dispatch_denial("   ") == {}


class TestConcurrency:
    def test_below_the_limit_it_is_quiet(self):
        for _ in range(MAX_RUNNING_AGENTS - 1):
            record_dispatch("s1", agent_type="python")

        assert concurrency_gate("s1") == {}

    def test_at_the_limit_the_next_dispatch_asks(self):
        for _ in range(MAX_RUNNING_AGENTS):
            record_dispatch("s1", agent_type="reviewer")

        answer = concurrency_gate("s1")

        assert answer["decision"] == "ask"
        assert answer["running"] == MAX_RUNNING_AGENTS
        assert f"{MAX_RUNNING_AGENTS} agents are already running" in answer["reason"]
        assert answer["context"]

    def test_a_finished_agent_frees_a_slot(self):
        for _ in range(MAX_RUNNING_AGENTS):
            record_dispatch("s1", agent_type="python")
        record_subagent_stop("s1")

        assert concurrency_gate("s1") == {}

    def test_no_session_no_prompt(self):
        assert concurrency_gate(None) == {}


class TestCombineGates:
    def test_a_deny_wins(self):
        deny = {"decision": "deny", "reason": "d"}
        ask = {"decision": "ask", "reason": "a", "context": "c"}

        assert combine_gates(ask, deny) is deny

    def test_two_asks_merge_their_text(self):
        merged = combine_gates(
            {"decision": "ask", "reason": "too many", "context": "wait"},
            {"decision": "ask", "reason": "wrong agent", "context": "use python"},
        )

        assert merged["decision"] == "ask"
        assert merged["reason"] == "too many wrong agent"
        assert merged["context"] == "wait use python"

    def test_nothing_is_nothing(self):
        assert combine_gates({}, {}) == {}


# ---------- the route, end to end ----------


class _Body:
    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.cookies = {}
        self.scope = {"type": "http", "headers": []}

    async def json(self):
        return self._body


def _dispatch(**body):
    from memory_mcp.web import hooks

    return json.loads(asyncio.run(hooks._hook_dispatch(_Body(body))).body)


class TestTheRoute:
    def test_a_subagents_dispatch_is_denied_and_not_recorded(self):
        answer = _dispatch(
            session_id="s1", cwd="/nowhere", subagent_type="reviewer",
            agent_id="agent-1", agent_type="reviewer", event="PreToolUse",
        )

        assert answer["decision"] == "deny"
        assert dispatches_for("s1") == [], "a refused agent never runs, so it is not history"

    def test_the_third_concurrent_dispatch_asks(self):
        answers = [
            _dispatch(session_id="s1", cwd="/nowhere", subagent_type="python")
            for _ in range(MAX_RUNNING_AGENTS + 1)
        ]

        assert answers[:MAX_RUNNING_AGENTS] == [{}] * MAX_RUNNING_AGENTS
        assert answers[-1]["decision"] == "ask"
        assert len(dispatches_for("s1")) == MAX_RUNNING_AGENTS + 1
        assert running_dispatches("s1") == MAX_RUNNING_AGENTS, "the prompted one is not running"

    def test_subagent_stop_is_counted_and_gates_nothing(self):
        for _ in range(MAX_RUNNING_AGENTS):
            _dispatch(session_id="s1", cwd="/nowhere", subagent_type="python")

        answer = _dispatch(
            session_id="s1", cwd="/nowhere", agent_id="agent-1",
            agent_type="python", event="SubagentStop",
        )

        assert answer == {}
        assert running_dispatches("s1") == MAX_RUNNING_AGENTS - 1
        assert _dispatch(session_id="s1", cwd="/nowhere", subagent_type="python") == {}


# ---------- the SubagentStop script ----------


def _run_stop(payload, env):
    return subprocess.run(
        ["bash", str(STOP_HOOK)], input=json.dumps(payload), text=True,
        capture_output=True, timeout=30, env=env,
    )


class TestTheStopScript:
    def test_it_posts_the_stop(self):
        with _stub_daemon("{}") as (port, seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            result = _run_stop(
                {"session_id": "s1", "cwd": "/repo", "agent_id": "a1",
                 "agent_type": "python", "hook_event_name": "SubagentStop"},
                env,
            )

        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert seen[0]["path"] == "/api/hook/dispatch"
        assert json.loads(seen[0]["body"]) == {
            "session_id": "s1", "cwd": "/repo", "agent_id": "a1",
            "agent_type": "python", "event": "SubagentStop",
        }

    def test_no_session_means_no_request(self):
        with _stub_daemon("{}") as (port, seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            result = _run_stop({"cwd": "/repo"}, env)

        assert result.returncode == 0
        assert seen == []

    def test_no_daemon_is_silent(self):
        env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": "9"}
        result = _run_stop({"session_id": "s1"}, env)

        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")

    def test_the_payload_is_data_not_shell(self):
        canary = Path(os.environ.get("TMPDIR", "/tmp")) / "memory-mcp-stop-canary"
        canary.unlink(missing_ok=True)
        with _stub_daemon("{}") as (port, _seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            _run_stop({"session_id": f"$(touch {canary})", "agent_id": f"`touch {canary}`"}, env)

        assert not canary.exists()
