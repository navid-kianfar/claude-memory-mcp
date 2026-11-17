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
from memory_mcp.harnesses.deepseek import (
    HOOKS_PLUGIN_PACKAGE,
    DeepSeekHarness,
    hooks_config_path,
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
    def __init__(self, returncode: int = 0, stderr: str = "", raises: Exception | None = None):
        self.calls: list[tuple[str, ...]] = []
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(list(argv), self.returncode, "", self.stderr)


@pytest.fixture
def dsh_home(tmp_path, monkeypatch):
    home = tmp_path / "dsh-home"
    monkeypatch.setenv("DSH_HOME", str(home))
    return home


def harness(runner=None, on_path=True):
    return DeepSeekHarness(
        runner=runner or FakeRunner(),
        which=lambda name: "/usr/local/bin/dsh" if on_path else None,
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
        dsh_home.mkdir()
        patch = dsh_home / "cordis.patch.yml"
        patch.write_text(USER_PATCH)
        harness().configure_mcp(URL)
        harness().install_hooks()
        first = patch.read_text()

        harness().configure_mcp(URL)
        harness().install_hooks()

        assert patch.read_text() == first

    def test_repointing_replaces_only_our_block_in_place(self, dsh_home):
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
        assert "matcher" not in by_script["record-subagent-stop.sh"]
        for group in by_script.values():
            assert group["hooks"][0]["command"].startswith("env MEMORY_MCP_HARNESS=deepseek ")

    def test_bridge_row_points_at_the_hooks_json(self, dsh_home):
        harness().install_hooks()
        row = inserted(dsh_home / "cordis.patch.yml")["memory-mcp-hooks"]
        assert row["name"] == HOOKS_PLUGIN_PACKAGE
        assert row["config"] == {"configPath": str(hooks_config_path())}

    def test_plugin_installed_into_each_existing_profile(self, dsh_home):
        for name in ("web", "headless", ".web.staging-x"):
            (dsh_home / "profiles" / name).mkdir(parents=True)
        runner = FakeRunner()

        report = harness(runner).install_hooks()

        assert runner.calls == [
            ("/usr/local/bin/dsh", "plugin", "--profile", p, "add", HOOKS_PLUGIN_PACKAGE)
            for p in ("headless", "web")
        ]
        assert not report.conflicts

    def test_plugin_failure_is_a_warning_with_the_command(self, dsh_home):
        (dsh_home / "profiles" / "web").mkdir(parents=True)
        runner = FakeRunner(returncode=1, stderr="ERR_PNPM_NO_MATCHING_VERSION")

        report = harness(runner).install_hooks()

        warning = next(a for a in report.actions if "failed for profile 'web'" in a)
        assert "ERR_PNPM_NO_MATCHING_VERSION" in warning
        assert f"plugin --profile web add {HOOKS_PLUGIN_PACKAGE}" in warning
        assert hooks_config_path().exists()

    def test_plugin_runner_crash_is_a_warning(self, dsh_home):
        (dsh_home / "profiles" / "web").mkdir(parents=True)
        report = harness(FakeRunner(raises=subprocess.TimeoutExpired("dsh", 1))).install_hooks()
        assert any("failed for profile 'web'" in a for a in report.actions)

    def test_no_dsh_on_path_runs_nothing(self, dsh_home):
        (dsh_home / "profiles" / "web").mkdir(parents=True)
        runner = FakeRunner()

        report = harness(runner, on_path=False).install_hooks()

        assert runner.calls == []
        assert any("not on PATH" in a for a in report.actions)


class TestRoleBriefs:
    def test_briefs_are_files_not_definitions(self, temp_data_dir):
        report = harness().install_agents()
        assert (temp_data_dir / "agents" / "python.md").exists()
        assert any("no file-based agent definitions" in a for a in report.actions)
