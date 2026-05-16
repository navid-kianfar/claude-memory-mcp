"""Unit tests for ClaudeMdService - CLAUDE.md import + stub-rewrite."""

import pytest

from memory_mcp.container import Container
from memory_mcp.db.connection import get_connection

SAMPLE_MD = """\
# My Project

A short overview of the project.

## Architecture

Layered: repositories, services, container.

## Mandatory Rules

- Always run the test suite before committing
- Always use type hints

## Forbidden

- Never commit secrets
- Never force-push to main

## Deployment

Deployed to AWS via GitHub Actions.
"""


@pytest.fixture
def container():
    return Container()


@pytest.fixture
def project(container, project_slug):
    container.project_repo.register(project_slug, "Test")
    conn = get_connection(project_slug)
    conn.close()
    return project_slug


class TestClaudeMdImport:
    def test_import_categorizes_sections(self, container, project, tmp_path):
        md = tmp_path / "CLAUDE.md"
        md.write_text(SAMPLE_MD)

        result = container.claude_md_service.import_file(project, str(md))
        assert result["status"] == "ok"

        cats = [m["category"] for m in result["memories"]]
        # Two mandatory + two forbidden rules, split per bullet.
        assert cats.count("mandatory_rules") == 2
        assert cats.count("forbidden_rules") == 2
        assert "architecture" in cats
        assert "devops" in cats

    def test_import_accepts_directory(self, container, project, tmp_path):
        (tmp_path / "CLAUDE.md").write_text(SAMPLE_MD)
        result = container.claude_md_service.import_file(project, str(tmp_path))
        assert result["imported"] > 0

    def test_missing_file_raises(self, container, project, tmp_path):
        with pytest.raises(ValueError):
            container.claude_md_service.import_file(project, str(tmp_path / "nope.md"))

    def test_rules_are_searchable_after_import(self, container, project, tmp_path):
        md = tmp_path / "CLAUDE.md"
        md.write_text(SAMPLE_MD)
        container.claude_md_service.import_file(project, str(md))

        rules = container.rules_service.get_rules(project)
        assert len(rules.mandatory_rules) == 2
        assert len(rules.forbidden_rules) == 2

    def test_stub_rewrite_backs_up_original(self, container, project, tmp_path):
        md = tmp_path / "CLAUDE.md"
        md.write_text(SAMPLE_MD)

        result = container.claude_md_service.import_file(
            project, str(md), stub_rewrite=True,
        )
        assert "stub" in result
        backup = tmp_path / "CLAUDE.imported-bak.md"
        assert backup.exists()
        assert backup.read_text() == SAMPLE_MD
        assert "Memory MCP" in md.read_text()
        assert project in md.read_text()
