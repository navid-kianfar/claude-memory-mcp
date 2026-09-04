"""setup_agents(): agents/ is the source, ~/.claude/agents/ is the artefact.

The behaviour worth protecting is the retirement rule. Removing an agent from
agents/ must remove its installed copy, but the same directory also holds agents
the user wrote by hand, and deleting one of those would be destroying work this
installer never created. The manifest is what separates the two.
"""

import json

import pytest

from memory_mcp import setup as setup_mod
from memory_mcp.config import settings


@pytest.fixture
def agent_dirs(tmp_path, monkeypatch):
    """Point the installer at a temp source folder and a temp ~/.claude/agents."""
    source = tmp_path / "agents"
    source.mkdir()
    dest = tmp_path / "home" / ".claude" / "agents"
    monkeypatch.setattr(setup_mod, "AGENTS_DIR", source)
    monkeypatch.setattr(setup_mod, "claude_agents_dir", lambda: dest)
    return source, dest


def _write(path, name, body="---\nname: x\n---\n"):
    (path / name).write_text(body)


class TestAgentInstall:
    def test_definitions_are_copied_to_the_claude_agents_directory(self, agent_dirs):
        source, dest = agent_dirs
        _write(source, "pm.md")
        _write(source, "backend.md")

        setup_mod.setup_agents()

        assert sorted(p.name for p in dest.glob("*.md")) == ["backend.md", "pm.md"]

    def test_readme_documents_the_folder_and_is_not_an_agent(self, agent_dirs):
        source, dest = agent_dirs
        _write(source, "pm.md")
        _write(source, "README.md", "# not an agent\n")

        setup_mod.setup_agents()

        assert not (dest / "README.md").exists()
        assert (dest / "pm.md").exists()

    def test_reinstall_overwrites_an_edited_copy(self, agent_dirs):
        """The installed copy is an artefact: edits belong upstream in agents/."""
        source, dest = agent_dirs
        _write(source, "pm.md", "the source\n")
        setup_mod.setup_agents()
        (dest / "pm.md").write_text("edited in place\n")

        setup_mod.setup_agents()

        assert (dest / "pm.md").read_text() == "the source\n"

    def test_retiring_an_agent_removes_its_installed_copy(self, agent_dirs):
        source, dest = agent_dirs
        _write(source, "pm.md")
        _write(source, "e2e.md")
        setup_mod.setup_agents()

        (source / "e2e.md").unlink()
        setup_mod.setup_agents()

        assert (dest / "pm.md").exists()
        assert not (dest / "e2e.md").exists()

    def test_an_agent_this_installer_never_wrote_is_left_alone(self, agent_dirs):
        """The whole reason the manifest exists. ~/.claude/agents/ is shared."""
        source, dest = agent_dirs
        _write(source, "pm.md")
        setup_mod.setup_agents()
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "my-own-agent.md").write_text("hand written\n")

        # Retire everything we installed; the user's agent must survive it.
        (source / "pm.md").unlink()
        setup_mod.setup_agents()

        assert not (dest / "pm.md").exists()
        assert (dest / "my-own-agent.md").read_text() == "hand written\n"

    def test_manifest_records_what_was_installed(self, agent_dirs):
        source, _ = agent_dirs
        _write(source, "pm.md")

        setup_mod.setup_agents()

        manifest = settings.data_dir / "agents-installed.json"
        assert json.loads(manifest.read_text()) == ["pm.md"]

    def test_a_missing_source_folder_is_not_an_error(self, tmp_path, monkeypatch):
        """Setup runs on machines where the repo layout may differ."""
        monkeypatch.setattr(setup_mod, "AGENTS_DIR", tmp_path / "nope")
        monkeypatch.setattr(
            setup_mod, "claude_agents_dir", lambda: tmp_path / "dest"
        )

        setup_mod.setup_agents()  # must not raise

        assert not (tmp_path / "dest").exists()


class TestShippedAgentDefinitions:
    """The five definitions in agents/ must stay valid and keep their constraints.

    These read the real files, not fixtures. A definition is a prompt, and a prompt
    silently losing the line that makes it correct is exactly the regression this
    folder exists to make reviewable.
    """

    REQUIRED = {
        "pm", "backend", "frontend", "designer",
        "test", "reviewer", "devops", "docs",
    }

    @staticmethod
    def _definitions():
        import re

        found = {}
        for path in setup_mod.AGENTS_DIR.glob("*.md"):
            if path.name.lower() == "readme.md":
                continue
            text = path.read_text()
            match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
            assert match, f"{path.name} has no frontmatter block"
            front = dict(
                (k.strip(), v.strip())
                for k, _, v in (
                    line.partition(":") for line in match.group(1).splitlines() if line.strip()
                )
            )
            found[path.stem] = (front, match.group(2))
        return found

    def test_the_team_is_all_five_agents(self):
        assert set(self._definitions()) == self.REQUIRED

    def test_every_definition_declares_name_description_and_model(self):
        for stem, (front, _) in self._definitions().items():
            for field in ("name", "description", "model"):
                assert front.get(field), f"{stem}.md is missing {field}"
            assert front["name"] == stem, f"{stem}.md declares name={front['name']!r}"

    def test_the_reviewer_cannot_edit_what_it_judges(self):
        """A reviewer that can fix its findings stops reviewing and starts agreeing."""
        front, _ = self._definitions()["reviewer"]
        banned = front.get("disallowedTools", "")
        assert "Edit" in banned and "Write" in banned, (
            "reviewer must be denied Edit/Write via disallowedTools"
        )
        assert "tools" not in front, (
            "reviewer must use disallowedTools, not an allowlist - an allowlist "
            "risks filtering out the inherited MCP tools it needs"
        )

    def test_pm_is_not_tool_restricted(self):
        """PM reads and writes. Fan-out protects its context; it is not a sandbox."""
        front, _ = self._definitions()["pm"]
        assert "disallowedTools" not in front, "pm must keep full tools"
        assert "tools" not in front, "pm must not be narrowed by an allowlist"

    def test_every_agent_pins_opus_5(self):
        for stem, (front, _) in self._definitions().items():
            assert front["model"] == "claude-opus-5", f"{stem}.md is on {front['model']}"

    def test_effort_levels_are_valid_and_deliberate(self):
        valid = {"low", "medium", "high", "xhigh", "max"}
        expected = {
            "pm": "max", "designer": "max", "test": "max", "reviewer": "max",
            "backend": "xhigh", "frontend": "xhigh", "devops": "xhigh",
            "docs": "high",
        }
        for stem, (front, _) in self._definitions().items():
            effort = front.get("effort")
            assert effort in valid, f"{stem}.md has effort={effort!r}"
            assert effort == expected[stem], (
                f"{stem}.md effort is {effort!r}, expected {expected[stem]!r}"
            )

    def test_every_agent_is_told_to_mind_tokens(self):
        """The user's standing constraint, not a nicety."""
        for stem, (_, body) in self._definitions().items():
            assert "Token discipline" in body or "token" in body.lower(), (
                f"{stem}.md says nothing about token cost"
            )

    def test_no_credential_is_committed_in_a_definition(self):
        """agents/ is version-controlled AND installs to ~/.claude/agents/."""
        import re

        for stem, (_, body) in self._definitions().items():
            for line in body.splitlines():
                if re.search(r'(password|secret|token|api[_-]?key)\s*[:=]\s*\S', line, re.I):
                    assert "test-credentials" in line or "never" in line.lower(), (
                        f"{stem}.md may contain a literal credential: {line[:60]!r}"
                    )

    def test_browser_agents_read_credentials_from_the_gitignored_file(self):
        for stem in ("frontend", "test"):
            _, body = self._definitions()[stem]
            assert "test-credentials.json" in body, f"{stem}.md has no credential source"

    def test_agents_that_share_the_repo_are_worktree_isolated(self):
        """frontend/backend/test can run at once; without this they collide."""
        definitions = self._definitions()
        for stem in ("frontend", "backend", "test"):
            assert definitions[stem][0].get("isolation") == "worktree", (
                f"{stem}.md must declare isolation: worktree"
            )

    def test_every_agent_is_told_to_load_its_own_context(self):
        """A subagent gets a prompt, not the transcript. Nothing reaches it unless it asks."""
        for stem, (_, body) in self._definitions().items():
            assert "memory_get_rules" in body, f"{stem}.md never loads the project rules"
            assert "memory_search" in body, f"{stem}.md never searches memory"

    def test_every_agent_carries_the_handoff_rule(self):
        """Agents share no conversation, so an unwritten handoff is a lost one."""
        for stem, (_, body) in self._definitions().items():
            assert "memory_task_comment" in body, f"{stem}.md has no task-comment handoff"
            assert "memory_store" in body, f"{stem}.md has no durable-memory handoff"

    def test_every_agent_passes_project_explicitly_on_writes(self):
        """A write that resolves its project implicitly has landed in the wrong one."""
        for stem, (_, body) in self._definitions().items():
            assert "project=" in body, f"{stem}.md never mentions passing project="


class TestDefaultSessionAgent:
    """`agent: pm` in ~/.claude/settings.json makes every session start as the lead.

    The value of these tests is the two refusals: never clobber a choice someone
    made by hand, and never point at an agent that is not installed.
    """

    @pytest.fixture
    def settings_file(self, tmp_path, monkeypatch):
        path = tmp_path / ".claude" / "settings.json"
        monkeypatch.setattr(setup_mod, "claude_settings_path", lambda: path)
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "pm.md").write_text("---\nname: pm\n---\n")
        monkeypatch.setattr(setup_mod, "AGENTS_DIR", agents)
        return path

    def _read(self, path):
        return json.loads(path.read_text())

    def test_sessions_are_pointed_at_pm(self, settings_file):
        setup_mod.setup_default_agent()

        assert self._read(settings_file)["agent"] == "pm"

    def test_existing_settings_are_preserved(self, settings_file):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps({"hooks": {"X": []}, "theme": "dark"}))

        setup_mod.setup_default_agent()

        data = self._read(settings_file)
        assert data["agent"] == "pm"
        assert data["theme"] == "dark" and "hooks" in data

    def test_a_hand_set_agent_is_never_clobbered(self, settings_file):
        """Someone choosing a different agent made a deliberate choice."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps({"agent": "my-own-agent"}))

        setup_mod.setup_default_agent()

        assert self._read(settings_file)["agent"] == "my-own-agent"

    def test_it_is_idempotent(self, settings_file):
        setup_mod.setup_default_agent()
        setup_mod.setup_default_agent()

        assert self._read(settings_file)["agent"] == "pm"

    def test_it_refuses_to_point_at_an_agent_that_is_not_installed(
        self, settings_file, monkeypatch, tmp_path
    ):
        """Otherwise every session starts as an agent that does not exist."""
        monkeypatch.setattr(setup_mod, "AGENTS_DIR", tmp_path / "empty")

        setup_mod.setup_default_agent()

        assert not settings_file.exists() or "agent" not in self._read(settings_file)

    def test_invalid_settings_json_is_left_alone(self, settings_file):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("{ not json")

        setup_mod.setup_default_agent()  # must not raise

        assert settings_file.read_text() == "{ not json"
