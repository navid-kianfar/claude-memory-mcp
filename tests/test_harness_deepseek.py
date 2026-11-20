"""The DeepSeek Harness (dsh) adapter.

Pinned from the dsh source (deepseek-harness @ ca33fd68): new rows go inside
an `- insert:` item of `$DSH_HOME/cordis.patch.yml`; the `mcp-manager` row is
never patched (that replaces the user's whole servers map); the hooks bridge
reads one Claude-format hooks.json; dsh's tools are `edit|write|apply_patch`
and `subagent|subagent_fork`. No test here starts a real process.
"""

import json
import subprocess

import pytest
import yaml

import memory_mcp.harnesses as registry
import memory_mcp.harnesses.deepseek as deepseek_mod
from memory_mcp.harnesses.deepseek import (
    HOOK_PROTOCOL_PACKAGE,
    HOOKS_PLUGIN_PACKAGE,
    DeepSeekHarness,
    hooks_config_path,
    remove_block,
)

URL = "http://127.0.0.1:8765/mcp"

USER_PATCH = """\
# my own tweaks
- id: mcp-manager
  config:
    servers:
      github: {transport: stdio, command: gh-mcp}

- insert:
    - id: my-plugin
      name: './mine.js'
"""


class FakeRunner:
    """Stands in for `dsh --version`, the only process the adapter runs."""

    def __init__(self, stdout: str = "0.2.2-rc.3\n", returncode: int = 0,
                 raises: Exception | None = None):
        self.calls: list[tuple[str, ...]] = []
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(list(argv), self.returncode, self.stdout, "")


@pytest.fixture
def dsh_home(tmp_path, monkeypatch):
    home = tmp_path / "dsh-home"
    monkeypatch.setenv("DSH_HOME", str(home))
    # The machine running this suite may have the real Desktop app.
    monkeypatch.setattr(deepseek_mod, "DEFAULT_DESKTOP_APP", tmp_path / "No Such.app")
    return home


def add_profile(home, name, bridge=False):
    profile = home / "profiles" / name
    profile.mkdir(parents=True)
    if bridge:
        package = profile / "node_modules" / HOOKS_PLUGIN_PACKAGE
        package.mkdir(parents=True)
        (package / "package.json").write_text("{}")
    return profile


def harness(runner=None, on_path=True):
    return DeepSeekHarness(
        runner=runner or FakeRunner(),
        which=lambda name: "/opt/no-such-npm/bin/dsh" if on_path else None,
    )


def rows(path):
    """The patch file parsed - which also proves it is valid YAML."""
    return yaml.safe_load(path.read_text())


def inserted(path):
    return {
        row["id"]: row
        for item in rows(path) if "insert" in item
        for row in item["insert"]
    }


class TestRegistryAndDetection:
    def test_registered(self):
        assert "deepseek" in [a.name for a in registry.ADAPTERS]

    def test_detected_by_home_dir(self, dsh_home):
        dsh_home.mkdir()
        assert harness(on_path=False).detect() is True

    def test_detected_by_binary_on_path(self, dsh_home):
        assert harness(on_path=True).detect() is True

    def test_not_detected_without_either(self, dsh_home):
        assert harness(on_path=False).detect() is False

    def test_capabilities_say_no_agent_definitions(self):
        caps = harness().capabilities()
        assert (caps.hooks, caps.per_turn_line, caps.edit_gate, caps.agents) == (
            True, True, True, False,
        )


class TestMCPRow:
    def test_inserts_our_row_into_a_fresh_home(self, dsh_home):
        report = harness().configure_mcp(URL)

        row = inserted(dsh_home / "cordis.patch.yml")["memory-mcp"]
        assert row["name"] == "@deepseek-ai/dsh-mcp-client"
        assert row["config"]["serverName"] == "memory"
        assert row["config"]["transport"] == "streamable-http"
        assert row["config"]["url"] == URL
        assert row["config"]["headers"] == {"X-Memory-Harness": "deepseek"}
        assert not report.conflicts

    def test_user_rows_comments_and_mcp_manager_survive(self, dsh_home):
        dsh_home.mkdir()
        patch = dsh_home / "cordis.patch.yml"
        patch.write_text(USER_PATCH)

        harness().configure_mcp(URL)

        text = patch.read_text()
        assert text.startswith(USER_PATCH)
        parsed = rows(patch)
        manager = [r for r in parsed if r.get("id") == "mcp-manager"]
        assert manager == [{"id": "mcp-manager", "config": {"servers": {
            "github": {"transport": "stdio", "command": "gh-mcp"}}}}]
        assert set(inserted(patch)) == {"my-plugin", "memory-mcp"}

    def test_rerun_changes_nothing(self, dsh_home):
        add_profile(dsh_home, "web", bridge=True)
        patch = dsh_home / "cordis.patch.yml"
        patch.write_text(USER_PATCH)
        harness().configure_mcp(URL)
        harness().install_hooks()
        first = patch.read_text()

        harness().configure_mcp(URL)
        harness().install_hooks()

        assert patch.read_text() == first

    def test_repointing_replaces_only_our_block_in_place(self, dsh_home):
        add_profile(dsh_home, "web", bridge=True)
        harness().configure_mcp(URL)
        harness().install_hooks()
        harness().configure_mcp("http://127.0.0.1:9999/mcp")

        patch = dsh_home / "cordis.patch.yml"
        found = inserted(patch)
        assert found["memory-mcp"]["config"]["url"] == "http://127.0.0.1:9999/mcp"
        assert "memory-mcp-hooks" in found
        assert patch.read_text().count("id: memory-mcp\n") == 1

    def test_the_dsh_template_is_replaced_not_appended_to(self, dsh_home):
        dsh_home.mkdir()
        patch = dsh_home / "cordis.patch.yml"
        patch.write_text("# Your patch layer for this dsh profile\n[]\n")

        harness().configure_mcp(URL)

        assert "# Your patch layer" in patch.read_text()
        assert "memory-mcp" in inserted(patch)

    def test_remote_mode_carries_the_token(self, dsh_home):
        harness().configure_mcp("http://ignored/mcp", remote_url="https://r.example.com/", token="t")
        row = inserted(dsh_home / "cordis.patch.yml")["memory-mcp"]
        assert row["config"]["url"] == "https://r.example.com/mcp"
        assert row["config"]["headers"]["Authorization"] == "Bearer t"

    def test_a_flow_style_file_is_a_conflict_not_a_rewrite(self, dsh_home):
        dsh_home.mkdir()
        patch = dsh_home / "cordis.patch.yml"
        original = "[{id: x, disabled: true}]\n"
        patch.write_text(original)

        report = harness().configure_mcp(URL)

        assert report.conflicts
        assert patch.read_text() == original

    def test_a_hand_written_row_with_our_id_is_a_conflict(self, dsh_home):
        dsh_home.mkdir()
        patch = dsh_home / "cordis.patch.yml"
        original = "- insert:\n    - id: memory-mcp\n      name: x\n"
        patch.write_text(original)

        report = harness().configure_mcp(URL)

        assert any("by hand" in c for c in report.conflicts)
        assert patch.read_text() == original

    def test_a_broken_marker_is_a_conflict(self, dsh_home):
        dsh_home.mkdir()
        patch = dsh_home / "cordis.patch.yml"
        original = "# >>> memory-mcp:mcp (x)\n- insert: []\n"
        patch.write_text(original)

        report = harness().configure_mcp(URL)

        assert report.conflicts
        assert patch.read_text() == original


class TestHooks:
    def test_hooks_json_uses_dsh_matchers_and_marks_the_harness(self, dsh_home):
        harness().install_hooks()

        config = json.loads(hooks_config_path().read_text())["hooks"]
        assert "SessionEnd" not in config
        assert set(config) >= {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop", "SubagentStop"}
        by_script = {
            group["hooks"][0]["command"].rsplit("/", 1)[-1]: group
            for groups in config.values() for group in groups
        }
        assert by_script["require-task.sh"]["matcher"] == "edit|write|apply_patch"
        assert by_script["record-dispatch.sh"]["matcher"] == "subagent|subagent_fork"
        assert by_script["confirm-dispatch.sh"]["matcher"] == "subagent|subagent_fork"
        assert "matcher" not in by_script["record-subagent-stop.sh"]
        for group in by_script.values():
            assert group["hooks"][0]["command"].startswith("env MEMORY_MCP_HARNESS=deepseek ")

    def test_bridge_row_mounted_when_every_profile_has_the_bridge(self, dsh_home):
        add_profile(dsh_home, "web", bridge=True)
        add_profile(dsh_home, "headless", bridge=True)
        runner = FakeRunner()

        report = harness(runner).install_hooks()

        row = inserted(dsh_home / "cordis.patch.yml")["memory-mcp-hooks"]
        assert row["name"] == HOOKS_PLUGIN_PACKAGE
        assert row["config"] == {"configPath": str(hooks_config_path())}
        assert not report.conflicts
        assert runner.calls == [], "nothing to report, so not even --version runs"

    def test_no_row_and_a_pinned_command_when_the_bridge_is_missing(self, dsh_home):
        add_profile(dsh_home, "web")
        runner = FakeRunner()

        report = harness(runner).install_hooks()

        assert not (dsh_home / "cordis.patch.yml").exists()
        assert runner.calls == [("/opt/no-such-npm/bin/dsh", "--version")]
        warning = next(a for a in report.actions if "profile 'web'" in a)
        assert (
            f"dsh plugin --profile web add {HOOKS_PLUGIN_PACKAGE}@0.2.2-rc.3 "
            f"{HOOK_PROTOCOL_PACKAGE}@0.2.2-rc.3"
        ) in warning

    def test_never_runs_a_plugin_install(self, dsh_home):
        add_profile(dsh_home, "web")
        add_profile(dsh_home, "headless")
        runner = FakeRunner()

        harness(runner).install_hooks()

        assert all("plugin" not in call for call in runner.calls)

    def test_an_existing_row_is_removed_when_a_profile_lacks_the_bridge(self, dsh_home):
        add_profile(dsh_home, "web", bridge=True)
        harness().configure_mcp(URL)
        harness().install_hooks()
        add_profile(dsh_home, "headless")

        report = harness().install_hooks()

        patch = dsh_home / "cordis.patch.yml"
        assert set(inserted(patch)) == {"memory-mcp"}
        assert "memory-mcp:hooks" not in patch.read_text()
        assert any("profile 'headless'" in a for a in report.actions)
        assert not any("profile 'web'" in a for a in report.actions)

    def test_unreadable_version_falls_back_to_the_next_tag(self, dsh_home):
        add_profile(dsh_home, "web")
        report = harness(FakeRunner(stdout="dsh: oops\n", returncode=1)).install_hooks()
        warning = next(a for a in report.actions if "profile 'web'" in a)
        assert f"{HOOKS_PLUGIN_PACKAGE}@next {HOOK_PROTOCOL_PACKAGE}@next" in warning
        assert "unreadable" in warning

    def test_version_runner_crash_is_not_fatal(self, dsh_home):
        add_profile(dsh_home, "web")
        report = harness(FakeRunner(raises=subprocess.TimeoutExpired("dsh", 1))).install_hooks()
        assert any("@next" in a for a in report.actions)

    def test_desktop_profile_names_the_bundled_command(self, dsh_home, tmp_path, monkeypatch):
        add_profile(dsh_home, "desktop")
        app = tmp_path / "DeepSeek Harness.app"
        bundled = app.joinpath(*deepseek_mod.BUNDLED_DSH_SUFFIX)
        bundled.parent.mkdir(parents=True)
        bundled.write_text("#!/bin/sh\n")
        monkeypatch.setattr(deepseek_mod, "DEFAULT_DESKTOP_APP", app)
        runner = FakeRunner()

        # PATH dsh is an npm install: it cannot manage desktop.
        report = harness(runner).install_hooks()

        warning = next(a for a in report.actions if "profile 'desktop'" in a)
        assert f"'{bundled}' plugin --profile desktop add" in warning
        assert "Quit DeepSeek Harness Desktop first" in warning
        assert runner.calls == [(str(bundled), "--version")]

    def test_desktop_via_the_registered_bundled_link(self, dsh_home, tmp_path):
        add_profile(dsh_home, "desktop")
        app_bin = tmp_path / "X.app" / "Contents" / "Resources" / "runtime" / "cli" / "bin"
        app_bin.mkdir(parents=True)
        (app_bin / "dsh").write_text("")
        link = tmp_path / "dsh"
        link.symlink_to(app_bin / "dsh")
        h = DeepSeekHarness(runner=FakeRunner(), which=lambda name: str(link))

        report = h.install_hooks()

        assert any("dsh plugin --profile desktop add" in a for a in report.actions)

    def test_desktop_without_a_bundled_command_says_how_to_get_one(self, dsh_home):
        add_profile(dsh_home, "desktop")
        report = harness().install_hooks()
        warning = next(a for a in report.actions if "'desktop'" in a)
        assert "Manage dsh Command" in warning


class TestRemoveBlock:
    def test_a_file_left_empty_becomes_a_list_again(self):
        text = "# header\n# >>> memory-mcp:hooks (x)\n- insert: []\n# <<< memory-mcp:hooks\n"
        assert yaml.safe_load(remove_block(text, "hooks")) == []

    def test_absent_block_is_a_no_op(self):
        assert remove_block(USER_PATCH, "hooks") == USER_PATCH


class TestRoleBriefs:
    def test_briefs_are_files_not_definitions(self, temp_data_dir):
        report = harness().install_agents()
        assert (temp_data_dir / "agents" / "python.md").exists()
        assert any("no file-based agent definitions" in a for a in report.actions)
