"""Dispatch guidance and gates, per harness.

The contract is one; the dispatch surface is not. Claude Code dispatches with
`Agent` + subagent_type, ZCode with `Agent` + subagent_type and built-in
`Explore`/`Code Search`, DeepSeek Harness with `subagent`/`subagent_fork`
(+ `name`, no type). The harness arrives as `harness` on hook calls and as the
X-Memory-Harness header on MCP; absent means Claude, whose wording must not
change by one byte.
"""

import json
import os
import subprocess

import pytest

from memory_mcp import enforcement
from memory_mcp.constants import normalize_harness
from memory_mcp.db.registry import dispatches_for, running_dispatches
from memory_mcp.enforcement import (
    MAX_RUNNING_PER_KIND, agent_team_intro, agent_team_line, dispatch_role,
    division_of_work,
)
from memory_mcp.web.hooks import identity_from_body
from tests.test_dispatch_limits import _dispatch
from tests.test_require_task_gate import DISPATCH_HOOK, _stub_daemon


@pytest.fixture
def roster(tmp_path, monkeypatch):
    directory = tmp_path / "agents"
    directory.mkdir()
    for name in ("python", "reviewer", "test", "backend"):
        (directory / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: The {name} agent.\n---\nbody\n"
        )
    monkeypatch.setattr(enforcement, "AGENT_TEAM_DIR", directory)
    return directory


class TestHarnessIdentity:
    @pytest.mark.parametrize("raw, expected", [
        (None, "claude"), ("", "claude"), ("claude", "claude"),
        ("ZCode", "zcode"), (" deepseek ", "deepseek"), ("codex", "claude"),
    ])
    def test_unknown_or_absent_is_claude(self, raw, expected):
        assert normalize_harness(raw) == expected

    def test_a_hook_body_carries_it(self):
        assert identity_from_body({"harness": "deepseek"}).harness == "deepseek"
        assert identity_from_body({}).harness == "claude"


class TestClaudeWordingIsUnchanged:
    def test_default_is_the_claude_tuple(self):
        assert division_of_work() is enforcement._DIVISION_OF_WORK
        assert division_of_work("claude") is enforcement._DIVISION_OF_WORK

    def test_intro_without_a_harness_equals_claudes(self, roster):
        assert agent_team_intro() == agent_team_intro(harness="claude")
        assert "NEVER pass `isolation` to the Agent tool" in agent_team_intro()

    def test_line_without_a_harness_equals_claudes(self, roster):
        assert agent_team_line() == agent_team_line(harness="claude")
        assert agent_team_line().endswith("Name the agent type in each dispatch description.")


class TestZCodeWording:
    def test_names_its_tool_and_built_in_types_and_drops_isolation(self, roster):
        text = agent_team_intro(harness="zcode")
        assert "HOW TO DISPATCH IN ZCODE" in text
        assert "`subagent_type` set to the role" in text
        assert "`Explore` or `Code Search`" in text
        assert "isolation" not in text
        assert "worktree" not in text
        assert "reports no agent finishing" in text

    def test_per_turn_line(self, roster):
        assert "Explore/Code Search" in agent_team_line(harness="zcode")
        assert len(agent_team_line(harness="zcode")) < 400


class TestDeepSeekWording:
    def test_role_rides_the_name_and_the_brief_file(self, roster, temp_data_dir):
        text = agent_team_intro(harness="deepseek")
        assert "HOW TO DISPATCH IN DEEPSEEK HARNESS" in text
        assert "Set `name` to the role" in text
        assert f"{temp_data_dir / 'agents'}/<role>.md" in text
        assert "isolation" not in text
        assert "No installed agent has the Agent tool" not in text

    def test_it_teaches_fork_versus_fresh_versus_follow_up(self, roster):
        text = agent_team_intro(harness="deepseek")
        assert "`subagent_fork` starts the child with your whole conversation" in text
        assert "`subagent` starts clean" in text
        assert "`send_message`" in text
        assert "Never spawn a fresh `subagent` to re-read" in text
        assert "use the id IT gets" in text

    def test_reads_the_written_role_briefs_when_present(self, tmp_path, monkeypatch, temp_data_dir):
        monkeypatch.setattr(enforcement, "AGENT_TEAM_DIR", tmp_path / "no-claude-agents")
        briefs = temp_data_dir / "agents"
        briefs.mkdir()
        (briefs / "python.md").write_text("---\nname: python\ndescription: Py.\n---\nx\n")

        assert enforcement.installed_agents(harness="deepseek") == [("python", "Py.")]
        assert enforcement.installed_agents() == []

    def test_per_turn_line(self, roster):
        line = agent_team_line(harness="deepseek")
        assert "subagent_fork" in line
        assert len(line) < 400


class TestDispatchRole:
    @pytest.mark.parametrize("subagent_type, name, tool, expected", [
        ("python", "", "Agent", "python"),
        ("", "", "Agent", "general-purpose"),
        ("", "python", "subagent", "python"),
        ("", "python-2", "subagent", "python"),
        ("", "Reviewer", "subagent_fork", "reviewer"),
        ("", "", "subagent", "general-purpose"),
        ("", "", "subagent_fork", None),
        ("react", "python", "subagent", "react"),
    ])
    def test_derivation(self, subagent_type, name, tool, expected):
        assert dispatch_role(subagent_type, name, tool) == expected


class TestTheRouteOnDsh:
    def test_named_children_are_counted_by_role(self):
        answers = [
            _dispatch(session_id="s1", cwd="/nowhere", tool_name="subagent",
                      name=f"python-{n}", harness="deepseek")
            for n in range(1, MAX_RUNNING_PER_KIND + 2)
        ]
        assert answers[:MAX_RUNNING_PER_KIND] == [{}] * MAX_RUNNING_PER_KIND
        assert answers[-1]["decision"] == "ask"
        assert running_dispatches("s1", "python") == MAX_RUNNING_PER_KIND

    def test_an_unnamed_fork_is_recorded_and_never_gated(self):
        answers = [
            _dispatch(session_id="s1", cwd="/nowhere", tool_name="subagent_fork",
                      harness="deepseek")
            for _ in range(MAX_RUNNING_PER_KIND + 2)
        ]
        assert answers == [{}] * (MAX_RUNNING_PER_KIND + 2)
        assert {row["agent_type"] for row in dispatches_for("s1")} == {"subagent_fork"}


class TestTheDispatchScriptOnDsh:
    def _post(self, payload, harness="deepseek"):
        with _stub_daemon("{}") as (port, seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port),
                   "MEMORY_MCP_HARNESS": harness}
            result = subprocess.run(
                ["bash", str(DISPATCH_HOOK)], input=json.dumps(payload), text=True,
                capture_output=True, timeout=30, env=env,
            )
        assert result.returncode == 0, result.stderr
        return json.loads(seen[0]["body"])

    def test_forwards_name_tool_and_harness_without_inventing_a_type(self):
        body = self._post({
            "cwd": "/repo", "session_id": "s1", "tool_name": "subagent",
            "tool_input": {"description": "Build it (python)", "prompt": "...",
                           "name": "python"},
        })
        assert body["subagent_type"] == ""
        assert body["name"] == "python"
        assert body["tool_name"] == "subagent"
        assert body["harness"] == "deepseek"

    def test_an_unnamed_fork_sends_no_type(self):
        body = self._post({
            "cwd": "/repo", "session_id": "s1", "tool_name": "subagent_fork",
            "tool_input": {"description": "Continue", "prompt": "..."},
        })
        assert (body["subagent_type"], body["name"]) == ("", "")


class TestTheRulesHookForwardsTheHarness:
    @pytest.mark.parametrize("harness, sent", [("deepseek", "deepseek"), (None, "")])
    def test_inject_rules(self, harness, sent):
        from tests.test_require_task_gate import HOOKS_DIR

        with _stub_daemon("") as (port, seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            env.pop("MEMORY_MCP_HARNESS", None)
            if harness:
                env["MEMORY_MCP_HARNESS"] = harness
            subprocess.run(
                ["bash", str(HOOKS_DIR / "inject-rules.sh")],
                input=json.dumps({"cwd": "/repo", "session_id": "s1"}), text=True,
                capture_output=True, timeout=30, env=env,
            )
        rules = next(r for r in seen if r["path"] == "/api/hook/rules")
        assert rules["query"]["harness"] == [sent]


class TestTheMcpHeader:
    @pytest.mark.parametrize("headers, expected", [
        ({}, "claude"),
        ({"x-memory-harness": "deepseek"}, "deepseek"),
        ({"x-memory-harness": "zcode"}, "zcode"),
    ])
    def test_session_start_words_the_brief_for_the_header(
        self, roster, monkeypatch, headers, expected,
    ):
        import fastmcp.server.dependencies as deps

        from memory_mcp import server

        monkeypatch.setattr(deps, "get_http_headers", lambda: headers)
        answer: dict = {}
        server._lead_briefing(answer, "p", None)

        assert answer["team_instructions"] == agent_team_intro(harness=expected).strip()
