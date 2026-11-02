"""The OpenCode adapter: JSONC-preserving MCP config, a lifecycle plugin,
honest NotSupported for agents.

The JSONC tests are the load-bearing ones: this machine's real opencode.jsonc
carries comments, and a naive json.load+dump would rewrite the user's file
into noise.
"""

import json

import pytest

from memory_mcp.harnesses.base import HarnessNotSupported

import memory_mcp.harnesses.opencode as oc
from memory_mcp.harnesses.opencode import (
    OpenCodeHarness,
    _strip_jsonc,
    _top_level_key_span,
)


@pytest.fixture
def oc_home(tmp_path, monkeypatch):
    root = tmp_path / "opencode"
    root.mkdir(parents=True)
    monkeypatch.setattr(oc, "opencode_config_path", lambda: root / "opencode.jsonc")
    monkeypatch.setattr(oc, "opencode_plugins_dir", lambda: root / "plugins")
    return root


USER_FILE = """{
  // The user's own comments survive the common case.
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    // local ollama models
    "ollama": { "npm": "@ai-sdk/openai-compatible" } // trailing comment
  }
}
"""


class TestJsoncScanner:
    def test_strip_blanks_comments_but_keeps_strings(self):
        text = '{ "url": "http://x//y", // line\n /* block */ "a": 1 }'
        stripped = _strip_jsonc(text)
        assert len(stripped) == len(text), "positions preserved"
        assert '"http://x//y"' in stripped, "string contents kept (a // inside one is not a comment)"
        assert "// line" not in stripped and "/* block */" not in stripped
        assert '"mcp"' not in text and json.loads(_strip_jsonc(text))["a"] == 1

    def test_top_level_span_found_and_nested_ignored(self):
        text = '{ "a": 1, "mcp": { "memory": { "url": "u" } } }'
        stripped = _strip_jsonc(text)
        span = _top_level_key_span(stripped, "mcp")
        assert span is not None
        assert json.loads(stripped[span[0]:span[1]])["memory"]["url"] == "u"

        nested = '{ "provider": { "mcp": { "deep": true } } }'
        assert _top_level_key_span(_strip_jsonc(nested), "mcp") is None


class TestMCPConfig:
    def test_insertion_preserves_comments_byte_for_byte(self, oc_home):
        path = oc_home / "opencode.jsonc"
        path.write_text(USER_FILE)

        report = OpenCodeHarness().configure_mcp("http://127.0.0.1:8765/mcp")

        text = path.read_text()
        assert "The user's own comments survive" in text
        assert "trailing comment" in text
        assert '"type": "remote"' in text
        assert '"http://127.0.0.1:8765/mcp"' in text
        assert "comments preserved" in report.actions[0]
        # The merged file still parses after comment-stripping.
        config = json.loads(_strip_jsonc(text))
        assert config["mcp"]["memory"]["enabled"] is True

    def test_existing_block_updates_and_warns_about_comments(self, oc_home):
        path = oc_home / "opencode.jsonc"
        path.write_text('{\n  // a comment\n  "mcp": { "memory": {"type":"remote","url":"http://old:1/mcp"} }\n}\n')

        report = OpenCodeHarness().configure_mcp("http://127.0.0.1:8765/mcp")

        assert any("old:1" in c for c in report.conflicts)
        assert any("comments" in w for w in report.actions)
        config = json.loads(path.read_text())  # comments are gone; JSON now
        assert config["mcp"]["memory"]["url"] == "http://127.0.0.1:8765/mcp"

    def test_no_change_leaves_the_file_untouched(self, oc_home):
        path = oc_home / "opencode.jsonc"
        path.write_text('{ "mcp": { "memory": {"type":"remote","url":"http://127.0.0.1:8765/mcp","enabled":true} } }')
        before = path.read_text()

        OpenCodeHarness().configure_mcp("http://127.0.0.1:8765/mcp")

        assert path.read_text() == before

    def test_unparseable_file_is_skipped_not_clobbered(self, oc_home):
        path = oc_home / "opencode.jsonc"
        path.write_text("{ this is not jsonc at all ]")
        before = path.read_text()

        report = OpenCodeHarness().configure_mcp("http://127.0.0.1:8765/mcp")

        assert path.read_text() == before
        assert any("does not parse" in a for a in report.actions)


class TestLifecycle:
    def test_plugin_installed_to_the_global_plugins_dir(self, oc_home):
        report = OpenCodeHarness().install_hooks()
        target = oc_home / "plugins" / "memory-mcp.js"
        assert target.is_file()
        source = (oc.PLUGIN_SOURCE).read_text()
        assert target.read_text() == source
        assert any("session.created" in a for a in report.actions)

    def test_reinstall_is_idempotent(self, oc_home):
        OpenCodeHarness().install_hooks()
        first = (oc_home / "plugins" / "memory-mcp.js").read_text()
        OpenCodeHarness().install_hooks()
        assert (oc_home / "plugins" / "memory-mcp.js").read_text() == first


class TestHonestLimits:
    def test_agents_raise_not_supported(self):
        from memory_mcp.harnesses.base import HarnessNotSupported

        with pytest.raises(HarnessNotSupported, match="not verified"):
            OpenCodeHarness().install_agents()

    def test_client_mode_is_refused_not_faked(self):
        with pytest.raises(HarnessNotSupported, match="client mode"):
            OpenCodeHarness().install_hooks(remote_url="https://r.example.com")

    def test_capabilities_declare_the_gaps(self):
        caps = OpenCodeHarness().capabilities()
        assert caps.hooks is True
        assert caps.agents is False
        assert caps.per_turn_line is False
        assert caps.edit_gate is False


class TestRegistration:
    def test_registry_order(self):
        import memory_mcp.harnesses as registry

        names = [a.name for a in registry.ADAPTERS]
        assert names.index("claude") < names.index("zcode") < names.index("opencode")
