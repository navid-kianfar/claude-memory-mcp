"""The session's project comes from the caller: cwd (manifest uid) or explicit slug.

The manifest in <cwd>/.agent-memory/ carries the project's stable identity -
verified live on tg-portal 2026-11-03 (slug + asoode link). An agent that only
knows where it is standing must never have to ask the user for the slug.
"""

import json

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
