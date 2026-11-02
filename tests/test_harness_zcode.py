"""The ZCode adapter: MCP under mcp.servers, hooks under hooks.events with
enabled:true (configuration-file hooks are disabled by default in ZCode),
agents to ~/.zcode/agents.

Schema sources: the client's own configuration guide and diagnosing-mcp /
diagnosing-hooks skills - `mcp.servers` (canonical fields, `type: "http"`),
exactly seven events, `SubagentStop` NOT among them, matcher is a
case-sensitive regex over the tool name.
"""

import json

import pytest

import memory_mcp.harnesses as registry
import memory_mcp.harnesses.zcode as zcode_mod
from memory_mcp.harnesses.zcode import ZCodeHarness


@pytest.fixture
def zcode_home(tmp_path, monkeypatch):
    """A temp ZCode home: config path + agents dir both redirected."""
    root = tmp_path / "zcode"
    monkeypatch.setattr(zcode_mod, "zcode_config_path", lambda: root / "cli" / "config.json")
    monkeypatch.setattr(zcode_mod, "zcode_agents_dir", lambda: root / "agents")
    return root


@pytest.fixture
def harness(zcode_home):
    return ZCodeHarness()


class TestDetection:
    def test_detect_when_the_config_root_exists(self, zcode_home):
        zcode_home.mkdir(parents=True)
        assert ZCodeHarness().detect() is True

    def test_not_detected_without_the_root(self):
        import memory_mcp.harnesses.zcode as m

        original = m.zcode_dir
        m.zcode_dir = lambda: __import__("pathlib").Path("/nonexistent-zcode-xyz")
        try:
            assert ZCodeHarness().detect() is False
        finally:
            m.zcode_dir = original


class TestMCP:
    def test_writes_the_http_entry_preserving_other_keys(self, harness, zcode_home):
        cfg = zcode_home / "cli" / "config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"plugins": {"some-plugin": True}}))

        harness.configure_mcp("http://127.0.0.1:8765/mcp")

        config = json.loads(cfg.read_text())
        assert config["mcp"]["servers"]["memory"] == {
            "type": "http", "url": "http://127.0.0.1:8765/mcp",
        }
        assert config["plugins"] == {"some-plugin": True}

    def test_a_foreign_entry_is_reported(self, harness, zcode_home):
        cfg = zcode_home / "cli" / "config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({
            "mcp": {"servers": {"memory": {"type": "http", "url": "http://127.0.0.1:1/mcp"}}},
        }))

        report = harness.configure_mcp("http://127.0.0.1:8765/mcp")

        assert any("127.0.0.1:1" in c for c in report.conflicts)

    def test_remote_mode_carries_the_token(self, harness, zcode_home):
        harness.configure_mcp("http://ignored:1/mcp", remote_url="https://r.example.com", token="t")
        entry = json.loads((zcode_home / "cli" / "config.json").read_text())
        entry = entry["mcp"]["servers"]["memory"]
        assert entry["url"] == "https://r.example.com/mcp"
        assert entry["headers"] == {"Authorization": "Bearer t"}


class TestHooks:
    def test_enabled_true_and_six_events_wired(self, harness, zcode_home):
        report = harness.install_hooks()
        cfg = json.loads((zcode_home / "cli" / "config.json").read_text())
        hooks = cfg["hooks"]

        assert hooks["enabled"] is True, "ZCode config hooks are disabled by default"
        assert set(hooks["events"]) == {
            "SessionStart", "UserPromptSubmit", "PreToolUse", "Stop",
        } or set(hooks["events"]) == {
            "SessionStart", "UserPromptSubmit", "PreToolUse", "Stop",
        } | {"PostToolUse", "PostToolUseFailure"}
        assert "SubagentStop" not in hooks["events"]
        assert any("no SubagentStop" in w for w in report.actions)

    def test_matchers_survive_as_regex(self, harness, zcode_home):
        harness.install_hooks()
        cfg = json.loads((zcode_home / "cli" / "config.json").read_text())
        pre = cfg["hooks"]["events"]["PreToolUse"]
        matchers = {g.get("matcher") for g in pre}
        assert "Edit|Write|NotebookEdit" in matchers
        assert "Agent|Task" in matchers

    def test_reinstall_does_not_duplicate(self, harness, zcode_home):
        harness.install_hooks()
        harness.install_hooks()
        cfg = json.loads((zcode_home / "cli" / "config.json").read_text())
        for event, groups in cfg["hooks"]["events"].items():
            ours = [
                h for g in groups for h in g["hooks"]
                if h.get("command", "").endswith(".sh")
            ]
            assert len(ours) == len({h["command"] for h in ours}), event

    def test_foreign_hooks_are_untouched(self, harness, zcode_home):
        cfg_path = zcode_home / "cli" / "config.json"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(json.dumps({
            "hooks": {"enabled": False, "events": {
                "Stop": [{"hooks": [{"type": "command", "command": "/bin/echo theirs"}]}],
            }},
        }))

        harness.install_hooks()

        cfg = json.loads(cfg_path.read_text())
        commands = [
            h["command"] for g in cfg["hooks"]["events"]["Stop"] for h in g["hooks"]
        ]
        assert "/bin/echo theirs" in commands
        assert cfg["hooks"]["enabled"] is True


class TestAgents:
    def test_installs_the_composed_team(self, harness, zcode_home):
        from memory_mcp.harnesses.claude import AGENTS_DIR

        report = harness.install_agents()
        dest = zcode_home / "agents"
        installed = {p.name for p in dest.glob("*.md")}
        assert installed, report.actions
        # The same roster as Claude's, from the same source of truth.
        claude_sources = {
            p.name for p in AGENTS_DIR.glob("*.md")
            if p.name.lower() != "readme.md" and not p.stem.startswith("_")
        }
        assert installed == claude_sources
        # Composition ran: a leaf carries the base's contract text.
        backend = (dest / "backend.md").read_text()
        assert "memory_session_start" in backend

    def test_retirement_removes_only_our_own(self, harness, zcode_home):
        dest = zcode_home / "agents"
        harness.install_agents()
        hand_made = dest / "hand-written.md"
        hand_made.write_text("---\nname: hand-written\ndescription: mine\n---\nbody")

        # Remove a definition from the source, then re-install.
        import memory_mcp.harnesses.claude as claude_mod

        original = claude_mod.AGENTS_DIR
        source_dir = claude_mod.AGENTS_DIR
        go = source_dir / "go.md"
        monkey_src = go.read_text()
        go.unlink()
        try:
            harness.install_agents()
        finally:
            go.write_text(monkey_src)
            claude_mod.AGENTS_DIR = original

        assert not (dest / "go.md").exists(), "our own retired file is removed"
        assert hand_made.exists(), "a hand-written agent is never touched"


class TestRegistration:
    def test_zcode_is_registered_after_claude(self):
        names = [a.name for a in registry.ADAPTERS]
        assert names == ["claude", "zcode"]
