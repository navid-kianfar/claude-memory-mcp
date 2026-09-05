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

    def test_an_agent_that_extends_a_base_is_composed_at_install(self, agent_dirs):
        """Claude Code has no inheritance; the installer provides it. The base is
        never installed on its own, and `extends` never reaches the artefact."""
        source, dest = agent_dirs
        _write(source, "_base.md",
               "---\nabstract: true\nmodel: claude-opus-5\neffort: high\n---\n"
               "{{EXTENSION}}\n\n## Shared contract\n\nAlways pass project=.\n")
        _write(source, "backend.md",
               "---\nname: backend\ndescription: Server work.\nextends: _base\n"
               "effort: xhigh\n---\nYou are backend.\n")

        setup_mod.setup_agents()

        assert sorted(p.name for p in dest.glob("*.md")) == ["backend.md"]
        text = (dest / "backend.md").read_text()
        front, body = setup_mod.parse_agent(text)
        assert front == {"name": "backend", "description": "Server work.",
                         "model": "claude-opus-5", "effort": "xhigh"}
        assert "extends" not in text and "abstract" not in text
        assert "{{EXTENSION}}" not in text
        assert body.index("You are backend.") < body.index("## Shared contract")

    def test_a_chain_composes_in_order_and_leaves_no_marker(self, agent_dirs):
        """dotnet -> backend -> _base: the expert layer lands where backend put
        its marker, between backend's craft and the shared contract."""
        source, dest = agent_dirs
        _write(source, "_base.md",
               "---\nabstract: true\nmodel: claude-opus-5\n---\n"
               "{{EXTENSION}}\n\n## Shared\n")
        _write(source, "backend.md",
               "---\nname: backend\ndescription: b\nextends: _base\nisolation: worktree\n---\n"
               "## Backend craft\n{{EXTENSION}}\n")
        _write(source, "dotnet.md",
               "---\nname: dotnet\ndescription: d\nextends: backend\n---\n"
               "## The .NET layer\n")

        setup_mod.setup_agents()

        dotnet = (dest / "dotnet.md").read_text()
        front, body = setup_mod.parse_agent(dotnet)
        assert front["isolation"] == "worktree", "inherited through the chain"
        assert front["name"] == "dotnet"
        assert body.index("## Backend craft") < body.index("## The .NET layer") < body.index("## Shared")
        assert "{{EXTENSION}}" not in dotnet
        assert "{{EXTENSION}}" not in (dest / "backend.md").read_text()

    def test_a_missing_base_is_an_error_naming_both_files(self, agent_dirs):
        source, dest = agent_dirs
        _write(source, "dotnet.md", "---\nname: dotnet\nextends: nope\n---\nx\n")
        with pytest.raises(setup_mod.AgentCompositionError, match="dotnet.md.*nope"):
            setup_mod.setup_agents()

    def test_a_cycle_is_rejected(self, agent_dirs):
        source, dest = agent_dirs
        _write(source, "a.md", "---\nname: a\nextends: b\n---\nA\n")
        _write(source, "b.md", "---\nname: b\nextends: a\n---\nB\n")
        with pytest.raises(setup_mod.AgentCompositionError, match="cannot extend itself"):
            setup_mod.setup_agents()

    def test_composition_is_idempotent(self, agent_dirs):
        source, dest = agent_dirs
        _write(source, "_base.md", "---\nabstract: true\nmodel: m\n---\n{{EXTENSION}}\n\nShared\n")
        _write(source, "x.md", "---\nname: x\ndescription: d\nextends: _base\n---\nMine\n")
        setup_mod.setup_agents()
        first = (dest / "x.md").read_text()
        setup_mod.setup_agents()
        assert (dest / "x.md").read_text() == first

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
        "dotnet", "nodejs", "react", "app",
    }

    @staticmethod
    def _definitions():
        """Every INSTALLABLE definition as it will be installed - composed, so a
        rule the base carries counts for the agents that extend it."""
        by_stem = {
            p.stem: p for p in setup_mod.AGENTS_DIR.glob("*.md")
            if p.name.lower() != "readme.md"
        }
        found = {}
        for stem, path in by_stem.items():
            if setup_mod.is_abstract_agent(path):
                continue
            parsed = setup_mod.parse_agent(setup_mod.compose_agent(path, by_stem))
            assert parsed, f"{path.name} has no frontmatter block"
            found[stem] = parsed
        return found

    def test_the_team_is_all_twelve_agents(self):
        assert set(self._definitions()) == self.REQUIRED

    def test_the_shared_base_is_not_installed_on_its_own(self):
        assert (setup_mod.AGENTS_DIR / "_base.md").exists()
        assert setup_mod.is_abstract_agent(setup_mod.AGENTS_DIR / "_base.md")

    def test_every_agent_carries_the_task_lifecycle(self):
        """The audit of 2026-09-04 found no definition mentioned the clock at all:
        agents commented on tasks and never started, finished or clocked off one."""
        for stem, (_, body) in self._definitions().items():
            for tool in ("memory_session_start", "memory_task_start", "memory_task_done",
                         "memory_session_end", "session_id"):
                assert tool in body, f"{stem}.md never mentions {tool}"
            assert "stop the clock" in body.lower(), f"{stem}.md never says to stop the clock"

    def test_every_definition_declares_name_description_and_model(self):
        for stem, (front, _) in self._definitions().items():
            for field in ("name", "description", "model"):
                assert front.get(field), f"{stem}.md is missing {field}"
            assert front["name"] == stem, f"{stem}.md declares name={front['name']!r}"

    def test_frontmatter_is_valid_yaml_a_strict_reader_can_parse(self):
        """`parse_agent` splits on the first colon, and so does Claude Code, so a
        description reading `Interface and UX decisions: design tokens` composed and
        dispatched fine while being invalid YAML - a strict parser reads the second
        colon as a nested mapping. Six definitions shipped that way. Parse the real
        frontmatter here, the way any other reader would.
        """
        yaml = pytest.importorskip("yaml")
        by_stem = {
            p.stem: p for p in setup_mod.AGENTS_DIR.glob("*.md")
            if p.name.lower() != "readme.md"
        }
        for path in sorted(by_stem.values()):
            # the abstract base is never installed, but a child inherits its keys
            text = path.read_text() if setup_mod.is_abstract_agent(path) \
                else setup_mod.installable_agent_text(path, by_stem)
            end = text.find("\n---\n", 3)
            assert text.startswith("---\n") and end > 0, f"{path.name} has no frontmatter block"
            try:
                front = yaml.safe_load(text[4:end])
            except yaml.YAMLError as exc:
                raise AssertionError(f"{path.name} frontmatter is not valid YAML: {exc}") from exc
            assert isinstance(front, dict), f"{path.name} frontmatter is not a mapping"

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
            "dotnet": "xhigh", "nodejs": "xhigh", "react": "xhigh", "app": "xhigh",
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


    def test_designer_does_not_preload_skills(self):
        """`skills:` pulls each skill's full content in on EVERY dispatch.

        The designer needs one or two per job out of six, so they are invoked on
        demand with the Skill tool instead. Removed 2026-09-04 after the user
        pushed back on agents loading what a session does not need.
        """
        front, body = self._definitions()["designer"]
        assert "skills" not in front, (
            "designer must invoke design skills on demand, not preload them"
        )
        assert "/design" in body or "`design`" in body, (
            "designer must be told which skill to reach for"
        )

    def test_no_agent_references_a_skill_that_is_not_installed(self):
        """A definition naming a removed skill sends the agent after nothing."""
        import re
        from pathlib import Path as _Path

        installed = {p.name for p in (_Path.home() / ".claude" / "skills").iterdir()} \
            if (_Path.home() / ".claude" / "skills").is_dir() else set()
        if not installed:
            import pytest as _pytest
            _pytest.skip("no user skills installed on this machine")
        for stem, (_, body) in self._definitions().items():
            for name in re.findall(r"`([a-z][a-z0-9-]{3,})`", body):
                if name.endswith("-design") or name in {"design", "design-system", "ui-styling", "brand", "slides"}:
                    assert name in installed, (
                        f"{stem}.md names skill {name!r}, which is not installed"
                    )

class TestRetireDefaultAgent:
    """`agent: pm` was removed in favour of the hook-injected lead brief.

    Two reasons, both observed: the setting is silently ignored by some clients
    (it was set on this machine and the desktop app started an ordinary session
    anyway), and running it alongside the hook would give a CLI session the same
    instructions twice plus pm's tools forced onto every one-off session.

    The test that matters is the last one: never delete a value someone chose.
    """

    @pytest.fixture
    def settings_file(self, tmp_path, monkeypatch):
        path = tmp_path / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(setup_mod, "claude_settings_path", lambda: path)
        return path

    def _write(self, path, data):
        path.write_text(json.dumps(data))

    def _read(self, path):
        return json.loads(path.read_text())

    def test_our_own_value_is_removed(self, settings_file):
        self._write(settings_file, {"agent": "pm", "theme": "dark"})

        setup_mod.retire_default_agent()

        data = self._read(settings_file)
        assert "agent" not in data
        assert data["theme"] == "dark", "unrelated settings must survive"

    def test_a_hand_set_agent_is_never_removed(self, settings_file):
        """Someone choosing a different agent made a deliberate choice."""
        self._write(settings_file, {"agent": "my-own-agent"})

        setup_mod.retire_default_agent()

        assert self._read(settings_file)["agent"] == "my-own-agent"

    def test_it_is_idempotent(self, settings_file):
        self._write(settings_file, {"agent": "pm"})

        setup_mod.retire_default_agent()
        setup_mod.retire_default_agent()

        assert "agent" not in self._read(settings_file)

    def test_a_missing_settings_file_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            setup_mod, "claude_settings_path", lambda: tmp_path / "nope.json"
        )

        setup_mod.retire_default_agent()  # must not raise

    def test_invalid_settings_json_is_left_alone(self, settings_file):
        settings_file.write_text("{ not json")

        setup_mod.retire_default_agent()

        assert settings_file.read_text() == "{ not json"


class TestLeadBrief:
    """The orchestration brief the hook injects, now that the session IS the lead."""

    def test_the_lead_is_not_offered_as_a_dispatch_target(self, monkeypatch):
        """Offering pm re-creates the relay layer this design rejected.

        Read the repo's own definitions, not ~/.claude/agents: that directory
        exists only where someone has run setup, so reading it made the assertion
        pass on a developer machine and fail on CI, where installed_agents()
        returns [] and `pm not in []` passes for the wrong reason.
        """
        from memory_mcp import enforcement

        monkeypatch.setattr(enforcement, "AGENT_TEAM_DIR", setup_mod.AGENTS_DIR)

        names = [n for n, _ in enforcement.installed_agents()]
        assert names, "the repo's agent definitions must be readable"
        assert enforcement.LEAD_AGENT not in names
        assert enforcement.LEAD_AGENT in [
            n for n, _ in enforcement.installed_agents(include_lead=True)
        ]

    def test_the_roster_carries_no_quote_characters(self, monkeypatch):
        """Quoting six descriptions to make the frontmatter valid YAML put literal
        quote characters into the brief injected at every SessionStart: the readers
        split on the first colon and never stripped them. The quotes are syntax.
        """
        from memory_mcp import enforcement

        monkeypatch.setattr(enforcement, "AGENT_TEAM_DIR", setup_mod.AGENTS_DIR)

        roster = enforcement.installed_agents(include_lead=True)
        assert roster, "the repo's agent definitions must be readable"
        for name, desc in roster:
            assert desc, f"{name} has no description"
            assert not desc.startswith(('"', "'")), f"{name} description starts with a quote: {desc[:60]!r}"
            assert not desc.endswith(('"', "'")), f"{name} description ends with a quote: {desc[-60:]!r}"

    def test_the_per_turn_line_stays_one_line(self):
        """It is injected on EVERY prompt; a long brief there is the waste itself."""
        from memory_mcp import enforcement

        line = enforcement.agent_team_line()
        if not line:
            pytest.skip("no agents installed on this machine")
        assert "\n" not in line
        assert len(line) < 400, f"per-turn line is {len(line)} chars"

    def test_no_agents_installed_means_no_injection(self, tmp_path, monkeypatch):
        """An unrelated machine must not get a roster of agents it does not have."""
        from memory_mcp import enforcement

        monkeypatch.setattr(enforcement, "AGENT_TEAM_DIR", tmp_path / "none")
        assert enforcement.agent_team_line() == ""
        assert enforcement.agent_team_intro() == ""
