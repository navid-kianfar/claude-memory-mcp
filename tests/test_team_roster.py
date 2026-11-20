"""Which team the brief names, per calling harness, and which roles a task may carry.

Before this, `installed_agents()` read `~/.claude/agents` whatever harness was
calling, so a machine with ZCode and no Claude Code got an empty team brief.
Every test points each harness's folder at a temp directory: a test that read
the developer's real agent folders would pass or fail by machine.
"""

import pytest

from memory_mcp import enforcement
from memory_mcp.harnesses import claude as claude_mod
from memory_mcp.harnesses import zcode as zcode_mod


def _write_team(directory, *names, extra=""):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: The {name} agent.\n{extra}---\nbody\n"
        )
    return directory


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Claude, ZCode and the shipped folder, each empty and each a temp path."""
    claude_home = tmp_path / "claude-home"
    zcode_agents = tmp_path / "zcode-home" / "agents"
    shipped = tmp_path / "shipped-agents"
    monkeypatch.setattr(enforcement, "AGENT_TEAM_DIR", claude_home / "agents")
    monkeypatch.setattr(zcode_mod, "zcode_agents_dir", lambda: zcode_agents)
    monkeypatch.setattr(claude_mod, "AGENTS_DIR", shipped)
    return {"claude": claude_home, "zcode": zcode_agents, "shipped": shipped}


def _names(harness="claude", include_lead=False):
    return [name for name, _ in enforcement.installed_agents(include_lead, harness)]


class TestTheCallingHarnessesTeam:
    def test_zcode_reads_its_own_folder_with_no_claude_installed(self, homes):
        _write_team(homes["zcode"], "python", "reviewer")

        assert _names("zcode") == ["python", "reviewer"]

    def test_zcode_does_not_borrow_claudes_team(self, homes):
        _write_team(homes["claude"] / "agents", "python")

        assert _names("zcode") == []

    def test_claude_reads_its_own_folder(self, homes):
        _write_team(homes["claude"] / "agents", "go")
        _write_team(homes["shipped"], "python")

        assert _names("claude") == ["go"]

    def test_claude_installed_without_a_team_has_no_team(self, homes):
        """An installed Claude Code loads only its own folder; naming a role
        that is not there would advertise an agent nobody can dispatch."""
        homes["claude"].mkdir()
        _write_team(homes["shipped"], "python")

        assert _names("claude") == []

    def test_a_caller_with_no_claude_home_gets_the_shipped_team(self, homes):
        """Codex and OpenCode send no harness name and have no agent files."""
        _write_team(homes["shipped"], "python", "rust")

        assert _names("claude") == ["python", "rust"]

    def test_deepseek_prefers_its_role_briefs(self, homes, temp_data_dir):
        _write_team(temp_data_dir / "agents", "python")
        _write_team(homes["shipped"], "python", "rust")

        assert _names("deepseek") == ["python"]

    def test_deepseek_without_briefs_gets_the_shipped_team(self, homes):
        _write_team(homes["claude"] / "agents", "go")
        _write_team(homes["shipped"], "python", "rust")

        assert _names("deepseek") == ["python", "rust"]

    def test_bases_in_the_shipped_folder_are_not_agents(self, homes):
        _write_team(homes["shipped"], "python")
        _write_team(homes["shipped"], "base", extra="abstract: true\n")
        (homes["shipped"] / "_code.md").write_text("---\nname: _code\n---\n")

        assert _names("deepseek") == ["python"]

    def test_the_lead_is_named_only_when_asked_for(self, homes):
        _write_team(homes["zcode"], "pm", "python")

        assert _names("zcode") == ["python"]
        assert _names("zcode", include_lead=True) == ["pm", "python"]

    def test_the_zcode_brief_names_its_team(self, homes):
        _write_team(homes["zcode"], "python", "reviewer")

        text = enforcement.agent_team_intro(harness="zcode")

        assert "python" in text
        assert "reviewer" in text


class TestKnownRoles:
    def test_it_is_the_union_of_every_team_and_the_shipped_folder(self, homes, temp_data_dir):
        _write_team(homes["claude"] / "agents", "custom")
        _write_team(homes["zcode"], "go")
        _write_team(temp_data_dir / "agents", "rust")
        _write_team(homes["shipped"], "pm", "python")

        assert enforcement.known_roles() == frozenset(
            {"custom", "go", "rust", "pm", "python"}
        )

    def test_it_is_none_when_no_team_exists_anywhere(self, homes):
        homes["claude"].mkdir()

        assert enforcement.known_roles() is None
