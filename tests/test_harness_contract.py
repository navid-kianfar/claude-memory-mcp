"""The harness contract: what every adapter owes, and what claude must not do.

The adapters are the wiring layer for multiple AI harnesses on one daemon core.
Two things are pinned here: the SURFACE (registry order, protocol methods,
capability honesty) and the CONFLICT RULE - an MCP entry named `memory` that
points somewhere else is reported, never silently rewritten, because one
machine quietly serving two memory servers took a week to diagnose once.
"""

import json

import pytest

import memory_mcp.harnesses as registry
from memory_mcp.harnesses.base import (
    ChangeReport,
    Harness,
    HarnessCapabilities,
    selected,
)


@pytest.fixture
def claude_json(tmp_path, monkeypatch):
    path = tmp_path / "claude.json"
    import memory_mcp.harnesses.claude as claude_mod

    monkeypatch.setattr(claude_mod, "claude_json_path", lambda: path)
    return path


class TestTheRegistry:
    def test_claude_is_registered_first(self):
        assert registry.ADAPTERS, "no adapters registered"
        assert registry.ADAPTERS[0].name == "claude"

    def test_every_adapter_satisfies_the_protocol(self):
        for adapter in registry.ADAPTERS:
            assert isinstance(adapter, Harness), adapter.name
            assert callable(adapter.detect)
            assert callable(adapter.capabilities)
            assert callable(adapter.configure_mcp)
            assert callable(adapter.install_hooks)
            assert callable(adapter.install_agents)

    def test_capabilities_are_declared_not_invented(self):
        for adapter in registry.ADAPTERS:
            caps = adapter.capabilities()
            assert isinstance(caps, HarnessCapabilities)
            # A capability is a claim the adapter is willing to defend; an
            # adapter that cannot hook must say hooks=False, not leave the
            # default. Both defaults are False, so True is always explicit.
            assert caps.hooks in (True, False)

    @staticmethod
    def _undetect_all(monkeypatch):
        """Detection off everywhere - the machine this suite runs on has real
        harness installs, and 'auto' must reflect detection, not the host."""
        for adapter in registry.ADAPTERS:
            monkeypatch.setattr(adapter, "detect", lambda: False)

    def test_selected_auto_uses_detection(self, monkeypatch):
        self._undetect_all(monkeypatch)
        assert selected("auto") == []

    def test_selected_all_ignores_detection(self, monkeypatch):
        self._undetect_all(monkeypatch)
        assert selected("all") == ["claude", "zcode"]

    def test_unknown_name_fails_loudly(self):
        with pytest.raises(ValueError, match="unknown harness"):
            selected("no-such-harness-xyz")


class TestClaudeMCPConfig:
    def test_writes_the_local_daemon_entry(self, claude_json):
        report = registry.get("claude").configure_mcp("http://127.0.0.1:8765/mcp")
        config = json.loads(claude_json.read_text())
        assert config["mcpServers"]["memory"] == {
            "type": "http", "url": "http://127.0.0.1:8765/mcp",
        }
        assert not report.conflicts

    def test_repointing_the_top_level_entry_is_reported(self, claude_json):
        claude_json.write_text(json.dumps({
            "mcpServers": {"memory": {"type": "http", "url": "http://127.0.0.1:9999/mcp"}},
        }))
        report = registry.get("claude").configure_mcp("http://127.0.0.1:8765/mcp")
        assert any("9999" in c for c in report.conflicts)
        assert json.loads(claude_json.read_text())["mcpServers"]["memory"]["url"] \
            == "http://127.0.0.1:8765/mcp"

    def test_a_project_scoped_override_is_reported_never_edited(self, claude_json):
        claude_json.write_text(json.dumps({
            "mcpServers": {"memory": {"type": "http", "url": "http://127.0.0.1:8765/mcp"}},
            "projects": {
                "/some/other/repo": {
                    "mcpServers": {
                        "memory": {"type": "http", "url": "http://127.0.0.1:9876/mcp"},
                    },
                },
            },
        }))
        report = registry.get("claude").configure_mcp("http://127.0.0.1:8765/mcp")
        assert any("shadows" in c and "/some/other/repo" in c for c in report.conflicts)
        # The user's override is theirs: the write below must not touch it.
        config = json.loads(claude_json.read_text())
        assert config["projects"]["/some/other/repo"]["mcpServers"]["memory"]["url"] \
            == "http://127.0.0.1:9876/mcp"

    def test_remote_mode_carries_the_token_header(self, claude_json):
        report = registry.get("claude").configure_mcp(
            "http://ignored:1/mcp", remote_url="https://mem.example.com", token="tok-1"
        )
        entry = json.loads(claude_json.read_text())["mcpServers"]["memory"]
        assert entry["url"] == "https://mem.example.com/mcp"
        assert entry["headers"] == {"Authorization": "Bearer tok-1"}
        assert not report.conflicts

    def test_invalid_json_is_skipped_not_clobbered(self, claude_json):
        claude_json.write_text("{not json")
        report = registry.get("claude").configure_mcp("http://127.0.0.1:8765/mcp")
        assert "not valid JSON" in report.actions[0]
        assert claude_json.read_text() == "{not json"


class TestChangeReport:
    def test_an_adapter_that_does_nothing_reports_it(self):
        report = ChangeReport("x")
        report.print()  # must not raise; silence is the thing under test
        assert report.actions == [] and report.conflicts == []
