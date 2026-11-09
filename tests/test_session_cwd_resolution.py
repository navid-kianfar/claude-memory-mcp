"""The session's project comes from the caller: cwd (manifest uid) or explicit slug.

The manifest in <cwd>/.agent-memory/ carries the project's stable identity -
verified live on tg-portal 2026-11-03 (slug + asoode link). An agent that only
knows where it is standing must never have to ask the user for the slug.
"""

import json
from pathlib import Path

import pytest

from memory_mcp.repositories.project_repository import ProjectRepository
from memory_mcp.server import _resolve_session_project


@pytest.fixture
def manifest_project(tmp_path):
    uid = "cf7ea1a5-1111-4222-8333-444455556666"
    snap = tmp_path / ".agent-memory"
    snap.mkdir()
    (snap / "manifest.json").write_text(json.dumps(
        {"version": 3, "project_id": uid, "slug": "cwd-sess"}))
    repo = ProjectRepository()
    repo.register("cwd-sess", "CWD Sess")
    repo.set_uid("cwd-sess", uid)
    return tmp_path


class TestCwdResolution:
    def test_cwd_resolves_through_the_manifest_uid(self, manifest_project):
        assert _resolve_session_project(None, str(manifest_project)) == "cwd-sess"

    def test_explicit_project_wins_over_cwd(self, manifest_project):
        assert _resolve_session_project("other", str(manifest_project)) == "other"

    def test_neither_is_a_teaching_error_naming_both_options(self):
        with pytest.raises(ValueError, match="cwd=.*project="):
            _resolve_session_project(None, None)

    def test_a_folder_with_no_project_names_the_path(self, tmp_path):
        with pytest.raises(ValueError, match="No memory project found"):
            _resolve_session_project(None, str(tmp_path))


@pytest.fixture
def home_with_repo(tmp_path):
    """A home folder holding one repo that carries a registered portable database.

    The shape that made session_start from the home folder answer with that
    repo's project: the home path is a substring of the repo's db_path.
    """
    home = tmp_path / "home"
    repo_dir = home / "Desktop" / "DEV" / "the-repo"
    repo_dir.mkdir(parents=True)
    portable_db = repo_dir / ".memory-mcp.duckdb"
    portable_db.touch()
    ProjectRepository().register(
        "the-repo", "The Repo", db_path=str(portable_db), project_path=str(repo_dir),
    )
    return home, repo_dir


class TestNoGuessing:
    def test_the_home_folder_is_an_error_not_the_project_under_it(self, home_with_repo):
        home, _ = home_with_repo
        with pytest.raises(ValueError, match="Pass project="):
            _resolve_session_project(None, str(home))

    def test_a_parent_of_the_repo_is_an_error(self, home_with_repo):
        home, repo_dir = home_with_repo
        with pytest.raises(ValueError, match="No memory project found"):
            _resolve_session_project(None, str(repo_dir.parent))

    def test_the_repo_folder_resolves(self, home_with_repo):
        _, repo_dir = home_with_repo
        assert _resolve_session_project(None, str(repo_dir)) == "the-repo"

    def test_a_folder_inside_the_repo_resolves(self, home_with_repo):
        _, repo_dir = home_with_repo
        nested = repo_dir / "src" / "pkg"
        nested.mkdir(parents=True)
        assert _resolve_session_project(None, str(nested)) == "the-repo"

    def test_a_portable_database_alone_resolves(self, tmp_path):
        repo_dir = tmp_path / "unbound"
        repo_dir.mkdir()
        portable_db = repo_dir / ".memory-mcp.duckdb"
        portable_db.touch()
        ProjectRepository().register("unbound-repo", "Unbound", db_path=str(portable_db))
        assert _resolve_session_project(None, str(repo_dir)) == "unbound-repo"

    def test_a_folder_named_like_a_slug_is_not_that_project(self, tmp_path):
        lookalike = tmp_path / "lookalike"
        lookalike.mkdir()
        ProjectRepository().register("lookalike", "Lookalike")
        with pytest.raises(ValueError, match="No memory project found"):
            _resolve_session_project(None, str(lookalike))


def test_loose_detection_is_strict_too(tmp_path):
    """Hooks and resolve_project use detect_project_from_cwd: a folder ABOVE a
    bound project is not that project, whatever its db_path contains."""
    from memory_mcp.context import detect_project_from_cwd, detect_project_strictly

    for folder in (tmp_path, tmp_path.parent, Path.home()):
        assert detect_project_from_cwd(str(folder)) == detect_project_strictly(str(folder))
