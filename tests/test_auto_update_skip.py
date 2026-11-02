"""The Stop-hook update must not rebuild identical bytes.

The auto-update hook fires at every turn end where the built paths changed -
including turns whose only change was a .agent-memory snapshot export. A
rebuild that would produce the same bytes cost a launchd reload per turn;
harmless to MCP sessions since v0.18.0 (stateless transport), still a restart
for nothing. install_is_current() is the skip.
"""

import subprocess

import pytest

import memory_mcp.setup as setup_mod
from memory_mcp.db.registry import set_setting

HEAD = "b" * 40


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """REPO_DIR pointed at a temp repo; git calls run for real inside it."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    monkeypatch.setattr(setup_mod, "REPO_DIR", repo)
    for args in (
        ["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "commit", "--allow-empty", "-q", "-m", "init"],
    ):
        subprocess.run(args, cwd=repo, capture_output=True, check=True)
    return repo


def _head(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


class TestInstallIsCurrent:
    def test_current_when_commit_matches_and_built_paths_clean(self, git_repo):
        set_setting("install:commit", _head(git_repo))
        assert setup_mod.install_is_current() is True

    def test_stale_when_the_recorded_commit_differs(self, git_repo):
        set_setting("install:commit", "0" * 40)
        assert setup_mod.install_is_current() is False

    def test_dirty_built_path_means_rebuild(self, git_repo):
        set_setting("install:commit", _head(git_repo))
        (git_repo / "src" / "memory_mcp").mkdir(parents=True, exist_ok=True)
        (git_repo / "src" / "memory_mcp" / "new_module.py").write_text("x = 1\n")
        assert setup_mod.install_is_current() is False

    def test_a_snapshot_export_is_not_a_rebuild_trigger(self, git_repo):
        set_setting("install:commit", _head(git_repo))
        (git_repo / ".agent-memory").mkdir()
        (git_repo / ".agent-memory" / "memory.duckdb").write_bytes(b"x")
        # .agent-memory is outside _INSTALL_PATHS: the tree is 'dirty' to git,
        # the built paths are not.
        assert setup_mod.install_is_current() is True

    def test_no_recorded_commit_means_rebuild(self, git_repo, tmp_path, monkeypatch):
        import memory_mcp.db.registry as reg

        monkeypatch.setattr(reg, "get_setting", lambda key: None)
        assert setup_mod.install_is_current() is False
