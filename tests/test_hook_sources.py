"""Where install_hooks finds the hook scripts, and what it does when it finds none.

From the installed (non-editable) runtime, REPO_DIR resolves inside
site-packages, where `.claude/hooks/` does not exist. Every adapter used to
wire nothing and write that emptiness over a working config. Now each adapter
reads the scripts from the checkout, else the repo an earlier install
recorded, else the copies already in the data dir - and when there are none
anywhere, reports a conflict and leaves its config file exactly as it was.

The checks run for every harness at once: a harness change ships to all of
them (the project rule of 2026-11-03).
"""

import json
from pathlib import Path

import pytest

from memory_mcp.db.registry import get_setting, set_setting
from memory_mcp.harnesses import claude as claude_mod
from memory_mcp.harnesses import codex as codex_mod
from memory_mcp.harnesses import deepseek as deepseek_mod
from memory_mcp.harnesses import opencode as opencode_mod
from memory_mcp.harnesses import zcode as zcode_mod

REAL_HOOKS = Path(__file__).resolve().parents[1] / ".claude" / "hooks"
REAL_REPO = REAL_HOOKS.parents[1]
WORKING_CONFIG = '{"keep": "me"}'


class _NoRunner:
    def __call__(self, argv):
        raise AssertionError(f"no process may run here: {argv}")


@pytest.fixture
def runtime(tmp_path, monkeypatch, temp_data_dir):
    """An installed runtime: the module paths point into a fake site-packages."""
    site = tmp_path / "site-packages"
    monkeypatch.setattr(claude_mod, "REPO_DIR", site)
    monkeypatch.setattr(claude_mod, "HOOKS_DIR", site / ".claude" / "hooks")
    monkeypatch.setattr(claude_mod, "AGENTS_DIR", site / "agents")
    return site


@pytest.fixture
def configs(tmp_path, monkeypatch):
    """Every harness's config file, each holding a working config of its own."""
    claude_settings = tmp_path / "claude" / "settings.json"
    zcode_root = tmp_path / "zcode"
    codex_root = tmp_path / "codex"
    opencode_root = tmp_path / "opencode"
    monkeypatch.setattr(claude_mod, "claude_settings_path", lambda: claude_settings)
    monkeypatch.setattr(zcode_mod, "zcode_dir", lambda: zcode_root)
    monkeypatch.setattr(zcode_mod, "zcode_config_path", lambda: zcode_root / "config.json")
    monkeypatch.setattr(codex_mod, "codex_config_path", lambda: codex_root / "config.toml")
    monkeypatch.setattr(codex_mod, "codex_agents_path", lambda: codex_root / "AGENTS.md")
    monkeypatch.setattr(codex_mod, "codex_dir", lambda: codex_root)
    monkeypatch.setattr(opencode_mod, "opencode_plugins_dir", lambda: opencode_root / "plugins")
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    monkeypatch.setattr(deepseek_mod, "DEFAULT_DESKTOP_APP", tmp_path / "No Such.app")

    files = {
        "claude": claude_settings,
        "zcode": zcode_root / "config.json",
        "deepseek": deepseek_mod.hooks_config_path(),
        "codex": codex_root / "config.toml",
    }
    for name, path in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('keep = "me"\n' if name == "codex" else WORKING_CONFIG)
    return files


def _adapters():
    return {
        "claude": claude_mod.ClaudeHarness(),
        "zcode": zcode_mod.ZCodeHarness(),
        "deepseek": deepseek_mod.DeepSeekHarness(runner=_NoRunner(), which=lambda _: None),
        "codex": codex_mod.CodexHarness(),
        "opencode": opencode_mod.OpenCodeHarness(),
    }


class TestNoScriptsAnywhere:
    @pytest.mark.parametrize("name", ["claude", "zcode", "deepseek", "codex", "opencode"])
    def test_it_refuses_and_leaves_the_working_config_alone(self, runtime, configs, name):
        before = {key: path.read_text() for key, path in configs.items()}

        report = _adapters()[name].install_hooks()

        assert report.conflicts, f"{name} must report the refusal, not succeed quietly"
        assert "no hook scripts were found" in report.conflicts[0]
        assert {key: path.read_text() for key, path in configs.items()} == before

    def test_opencode_installs_no_plugin_with_no_scripts_behind_it(self, runtime, configs, tmp_path):
        opencode_mod.OpenCodeHarness().install_hooks()

        assert not (tmp_path / "opencode" / "plugins" / "memory-mcp.js").exists()


class TestTheInstalledRuntimeFindsTheScripts:
    def test_through_the_repo_an_earlier_install_recorded(self, runtime, configs, temp_data_dir):
        set_setting(claude_mod.REPO_DIR_KEY, str(REAL_REPO))

        report = claude_mod.ClaudeHarness().install_hooks()

        assert not report.conflicts
        hooks = json.loads(configs["claude"].read_text())["hooks"]
        assert hooks["PreToolUse"] and hooks["UserPromptSubmit"]
        assert (temp_data_dir / "hooks" / "inject-rules.sh").is_file()

    def test_the_recorded_repo_is_not_overwritten_with_site_packages(self, runtime, configs):
        set_setting(claude_mod.REPO_DIR_KEY, str(REAL_REPO))

        claude_mod.ClaudeHarness().install_hooks()

        assert get_setting(claude_mod.REPO_DIR_KEY) == str(REAL_REPO)

    @pytest.mark.parametrize("name", ["claude", "zcode", "deepseek"])
    def test_through_the_copies_already_in_the_data_dir(self, runtime, configs, temp_data_dir, name):
        installed = temp_data_dir / "hooks"
        installed.mkdir(parents=True, exist_ok=True)
        for script in claude_mod.all_hook_scripts():
            (installed / script).write_text((REAL_HOOKS / script).read_text())

        report = _adapters()[name].install_hooks()

        assert not report.conflicts
        assert "inject-rules.sh" in configs[name].read_text()
        assert (installed / "inject-rules.sh").read_text() == (
            REAL_HOOKS / "inject-rules.sh"
        ).read_text()

    def test_a_partial_set_is_wired_and_the_missing_ones_are_named(self, runtime, configs, temp_data_dir):
        installed = temp_data_dir / "hooks"
        installed.mkdir(parents=True, exist_ok=True)
        (installed / "inject-rules.sh").write_text("#!/bin/bash\n")

        report = claude_mod.ClaudeHarness().install_hooks()

        assert not report.conflicts
        warning = next(line for line in report.actions if "not found" in line)
        assert "require-task.sh" in warning
        hooks = json.loads(configs["claude"].read_text())["hooks"]
        assert hooks["UserPromptSubmit"]
        assert not hooks.get("PreToolUse")


class TestAgentsFromTheInstalledRuntime:
    def test_the_team_installs_from_the_recorded_repo(self, runtime, tmp_path, monkeypatch):
        dest = tmp_path / "claude-agents"
        monkeypatch.setattr(claude_mod, "claude_agents_dir", lambda: dest)
        set_setting(claude_mod.REPO_DIR_KEY, str(REAL_REPO))

        claude_mod.ClaudeHarness().install_agents()

        assert (dest / "python.md").is_file()
