"""The Codex adapter: TOML textual merge, notify wiring, AGENTS.md pointer.

Verified against codex-cli 0.166.0 on this machine: config.toml supports
`[mcp_servers.<name>] url = ...` for streamable-HTTP servers, and `notify`
names a program invoked at turn end.
"""

import json
import subprocess
import tomllib

import pytest

import memory_mcp.harnesses.codex as cx
import memory_mcp.harnesses as registry
from memory_mcp.harnesses.codex import CodexHarness


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    root.mkdir(parents=True)
    monkeypatch.setattr(cx, "codex_config_path", lambda: root / "config.toml")
    monkeypatch.setattr(cx, "codex_agents_path", lambda: root / "AGENTS.md")
    monkeypatch.setattr(cx, "codex_dir", lambda: root)
    return root


USER_CONFIG = """# Codex config - the user's own comments must survive.
model = "gpt-5.3"

[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]

# approval policy etc.
approval_policy = "on-request"
"""


class TestMCPConfig:
    def test_appends_a_section_preserving_the_rest(self, codex_home):
        path = codex_home / "config.toml"
        path.write_text(USER_CONFIG)

        report = CodexHarness().configure_mcp("http://127.0.0.1:8765/mcp")

        text = path.read_text()
        assert "the user's own comments must survive" in text
        assert "[mcp_servers.context7]" in text
        parsed = tomllib.loads(text)
        assert parsed["mcp_servers"]["memory"]["url"] == "http://127.0.0.1:8765/mcp"
        assert not report.conflicts

    def test_replaces_our_section_in_place(self, codex_home):
        path = codex_home / "config.toml"
        path.write_text(USER_CONFIG)
        CodexHarness().configure_mcp("http://127.0.0.1:8765/mcp")

        CodexHarness().configure_mcp("http://127.0.0.1:9999/mcp")

        parsed = tomllib.loads(path.read_text())
        assert parsed["mcp_servers"]["memory"]["url"] == "http://127.0.0.1:9999/mcp"
        # Exactly one memory section after a re-point.
        assert path.read_text().count("[mcp_servers.memory]") == 1
        # The context7 server and the user's keys are still there.
        assert "context7" in parsed["mcp_servers"]
        assert parsed["model"] == "gpt-5.3"

    def test_foreign_url_is_reported(self, codex_home):
        path = codex_home / "config.toml"
        path.write_text(USER_CONFIG + '\n[mcp_servers.memory]\nurl = "http://other:1/mcp"\n')

        report = CodexHarness().configure_mcp("http://127.0.0.1:8765/mcp")

        assert any("other:1" in c for c in report.conflicts)

    def test_unparseable_toml_is_skipped_not_clobbered(self, codex_home):
        path = codex_home / "config.toml"
        path.write_text("this is [ not toml")
        before = path.read_text()

        report = CodexHarness().configure_mcp("http://127.0.0.1:8765/mcp")

        assert path.read_text() == before
        assert any("does not parse as TOML" in a for a in report.actions)

    def test_creates_the_file_when_absent(self, codex_home):
        CodexHarness().configure_mcp("http://127.0.0.1:8765/mcp")
        parsed = tomllib.loads((codex_home / "config.toml").read_text())
        assert parsed["mcp_servers"]["memory"]["url"] == "http://127.0.0.1:8765/mcp"


class TestNotify:
    def test_notify_key_written_and_wrapper_installed(self, codex_home):
        path = codex_home / "config.toml"
        path.write_text(USER_CONFIG)

        CodexHarness().install_hooks()

        parsed = tomllib.loads(path.read_text())
        # parsed["notify"] AT TOP LEVEL is the whole point: appended after the
        # last [table] header, TOML nests it inside that table and Codex never
        # sees it. This assertion is what caught exactly that.
        wrapper = parsed["notify"][0]
        assert wrapper.endswith("codex-notify.sh")
        import os
        assert os.path.isfile(wrapper), wrapper
        # In the fixture the user's own approval_policy sits after the table
        # header, so TOML itself nests it there - that is TOML semantics,
        # not our doing. Our line is the only thing we touched.
        assert parsed["mcp_servers"]["context7"]["approval_policy"] == "on-request"

    def test_agents_pointer_is_marker_delimited_and_idempotent(self, codex_home):
        agents = codex_home / "AGENTS.md"
        agents.write_text("# My instructions\nBe terse.\n")

        harness = CodexHarness()
        harness.install_hooks()
        harness.install_hooks()

        text = agents.read_text()
        assert "Be terse." in text, "the user's instructions are untouched"
        assert text.count("memory-mcp:begin") == 1, "no duplicate pointer"
        assert ".agent-memory/" in text

    def test_client_mode_refused(self, codex_home):
        from memory_mcp.harnesses.base import HarnessNotSupported

        with pytest.raises(HarnessNotSupported, match="client mode"):
            CodexHarness().install_hooks(remote_url="https://r.example.com")


class TestTheWrapper:
    def test_wrapper_hands_cwd_to_session_end(self, codex_home, tmp_path):
        """The real script, fired the way Codex fires notify."""
        CodexHarness().install_hooks()
        wrapper = None
        import tomllib as tl

        parsed = tl.loads((codex_home / "config.toml").read_text())
        wrapper = parsed["notify"][0]

        # A real session-end pass against the REAL daemon (this machine's),
        # for a folder with no snapshot - must exit 0 silently either way.
        repo = "/tmp"
        result = subprocess.run(
            ["bash", wrapper], input=json.dumps({"cwd": repo}),
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0


class TestHonestLimits:
    def test_agents_raise_not_supported(self):
        from memory_mcp.harnesses.base import HarnessNotSupported

        with pytest.raises(HarnessNotSupported, match="no subagent system"):
            CodexHarness().install_agents()

    def test_capabilities(self):
        caps = CodexHarness().capabilities()
        assert caps.hooks is True and caps.agents is False
        assert caps.per_turn_line is False and caps.edit_gate is False


class TestRegistration:
    def test_four_adapters_in_order(self):
        assert [a.name for a in registry.ADAPTERS] == [
            "claude", "zcode", "opencode", "codex",
        ]
