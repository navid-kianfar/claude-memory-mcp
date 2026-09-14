"""What `memory-mcp-setup` writes into ~/.claude/settings.json.

The interesting part is PreToolUse. It now carries two scripts that must see
different tools: require-task.sh on Edit|Write|NotebookEdit, record-dispatch.sh on
Agent|Task. Get that wrong in either direction and something breaks quietly -
require-task.sh on an Agent call would gate delegation behind a task, and
record-dispatch.sh on an Edit would file every file as a dispatch.

Every test here redirects `claude_settings_path` at a temp file. A test that wrote
the developer's real settings would be a test that installed hooks.
"""

import json

import pytest

from memory_mcp import setup as setup_mod


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "claude" / "settings.json"
    monkeypatch.setattr(setup_mod, "claude_settings_path", lambda: path)
    return path


def _groups(path, event):
    return json.loads(path.read_text())["hooks"][event]


def _group_for(path, event, script):
    for group in _groups(path, event):
        for hook in group.get("hooks", []):
            if hook.get("command", "").endswith(script):
                return group
    raise AssertionError(f"{script} is not installed on {event}")


class TestTheMatcherTable:
    def test_it_is_keyed_by_script_not_by_event(self):
        """Two PreToolUse scripts need two matchers, so the event cannot be the
        key. This is the table `setup_hooks` looks each script up in."""
        assert setup_mod.HOOK_MATCHERS == {
            "require-task.sh": "Edit|Write|NotebookEdit",
            "record-dispatch.sh": "Agent|Task",
        }

    def test_every_matcher_names_a_script_that_is_actually_installed(self):
        """A matcher keyed on a name no event lists would silently do nothing."""
        installed = {name for names in setup_mod.HOOK_EVENTS.values() for name in names}

        assert set(setup_mod.HOOK_MATCHERS) <= installed

    def test_both_pretooluse_scripts_have_a_matcher(self):
        """A PreToolUse hook with no matcher runs on every Read and Grep: a round
        trip before each one, and a gate on exploration."""
        for script in setup_mod.HOOK_EVENTS["PreToolUse"]:
            assert setup_mod.HOOK_MATCHERS.get(script)


class TestInstalling:
    def test_pretooluse_gets_one_group_per_script_with_its_own_matcher(
        self, settings_file,
    ):
        setup_mod.setup_hooks()

        assert _group_for(settings_file, "PreToolUse", "require-task.sh")["matcher"] == (
            "Edit|Write|NotebookEdit"
        )
        assert _group_for(
            settings_file, "PreToolUse", "record-dispatch.sh",
        )["matcher"] == "Agent|Task"

    def test_the_two_scripts_are_in_different_groups(self, settings_file):
        """One group carries one matcher, so sharing a group would give one of
        them the wrong tools."""
        setup_mod.setup_hooks()

        gate = _group_for(settings_file, "PreToolUse", "require-task.sh")
        dispatch = _group_for(settings_file, "PreToolUse", "record-dispatch.sh")
        assert gate is not dispatch

    def test_the_other_events_are_unchanged(self, settings_file):
        setup_mod.setup_hooks()
        data = json.loads(settings_file.read_text())["hooks"]

        assert len(data["UserPromptSubmit"]) == 1
        assert len(data["SessionStart"]) == 1
        assert len(data["Stop"]) == 2  # session-end + auto-update
        # Counts agents out; no matcher, or it would be matched against the
        # agent's TYPE and never fire.
        assert len(data["SubagentStop"]) == 1
        for event in ("UserPromptSubmit", "SessionStart", "Stop", "SubagentStop"):
            for group in data[event]:
                assert "matcher" not in group

    def test_every_script_is_copied_and_executable(self, settings_file):
        setup_mod.setup_hooks()
        dest = setup_mod.settings.data_dir / "hooks"

        for names in setup_mod.HOOK_EVENTS.values():
            for name in names:
                copied = dest / name
                assert copied.is_file(), name
                assert copied.stat().st_mode & 0o111, name

    def test_the_installed_scripts_forward_the_session_identity(self, settings_file):
        """The installed copy is what actually runs. `run_update()` skips
        setup_hooks(), so a machine only gets this on a full setup - which is
        exactly why the assertion is on the copy and not on the repo file."""
        setup_mod.setup_hooks()
        dest = setup_mod.settings.data_dir / "hooks"

        for name in ("inject-rules.sh", "require-task.sh", "session-start.sh",
                     "session-end.sh", "record-dispatch.sh"):
            body = (dest / name).read_text()
            assert "session_id" in body, name

    def test_rerunning_setup_is_idempotent(self, settings_file):
        setup_mod.setup_hooks()
        first = settings_file.read_text()

        setup_mod.setup_hooks()

        assert settings_file.read_text() == first

    def test_an_install_that_predates_the_dispatch_script_picks_it_up(
        self, settings_file,
    ):
        """The upgrade path: a settings.json with only the old PreToolUse group."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Edit|Write|NotebookEdit",
                    "hooks": [{
                        "type": "command",
                        "command": "/old/path/require-task.sh",
                    }],
                }],
            },
        }))

        setup_mod.setup_hooks()

        groups = _groups(settings_file, "PreToolUse")
        commands = [
            h["command"] for g in groups for h in g["hooks"]
        ]
        # The stale repo-path entry is gone, both current ones are present.
        assert not any(c == "/old/path/require-task.sh" for c in commands)
        assert sum(c.endswith("require-task.sh") for c in commands) == 1
        assert sum(c.endswith("record-dispatch.sh") for c in commands) == 1

    def test_a_users_own_pretooluse_hook_is_left_alone(self, settings_file):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps({
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "/my/own/audit.sh"}],
                }],
            },
        }))

        setup_mod.setup_hooks()

        commands = [
            h["command"] for g in _groups(settings_file, "PreToolUse")
            for h in g["hooks"]
        ]
        assert "/my/own/audit.sh" in commands

    def test_client_mode_prefixes_every_command_with_the_remote_env(
        self, settings_file,
    ):
        setup_mod.setup_hooks(remote_url="https://memory.example.com/", token="tok")

        group = _group_for(settings_file, "PreToolUse", "record-dispatch.sh")
        command = group["hooks"][0]["command"]
        assert command.startswith("env MEMORY_MCP_URL=https://memory.example.com")
        assert "MEMORY_MCP_TOKEN=tok" in command
        # And the matcher still applies, which is the bit a prefix could break.
        assert group["matcher"] == "Agent|Task"

    def test_invalid_settings_json_is_left_untouched(self, settings_file):
        """Better to install no hooks than to overwrite somebody's config."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("{ not json")

        setup_mod.setup_hooks()

        assert settings_file.read_text() == "{ not json"
