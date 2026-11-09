"""The Stop-hook update must not rebuild identical bytes.

The auto-update hook fires at every turn end where the built paths changed -
including turns whose only change was a .agent-memory snapshot export. A
rebuild that would produce the same bytes cost a launchd reload per turn;
harmless to MCP sessions since v0.18.0 (stateless transport), still a restart
for nothing. install_is_current() is the skip.
"""

import subprocess
from pathlib import Path

import pytest

import memory_mcp.setup as setup_mod
from memory_mcp.db.registry import set_setting

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


def _record_current(repo):
    """What setup_runtime does after a successful build."""
    setup_mod._record_install_tree(setup_mod._install_tree_digest())


class TestInstallIsCurrent:
    def test_current_when_the_built_paths_are_unchanged(self, git_repo):
        _record_current(git_repo)
        assert setup_mod.install_is_current() is True

    def test_stale_when_the_recorded_tree_differs(self, git_repo):
        set_setting(setup_mod.INSTALL_TREE_KEY, "0" * 64)
        assert setup_mod.install_is_current() is False

    def test_new_file_in_a_built_path_means_rebuild(self, git_repo):
        _record_current(git_repo)
        (git_repo / "src" / "memory_mcp").mkdir(parents=True, exist_ok=True)
        (git_repo / "src" / "memory_mcp" / "new_module.py").write_text("x = 1\n")
        assert setup_mod.install_is_current() is False

    def test_a_hook_script_edit_means_rebuild(self, git_repo):
        hooks = git_repo / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "session-end.sh").write_text("#!/bin/bash\nexit 0\n")
        _record_current(git_repo)
        (hooks / "session-end.sh").write_text("#!/bin/bash\necho changed\nexit 0\n")
        assert setup_mod.install_is_current() is False

    def test_a_dirty_install_later_reverted_is_rebuilt(self, git_repo):
        module = git_repo / "src" / "mod.py"
        module.write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "mod"], cwd=git_repo, check=True)
        module.write_text("x = 2  # work in progress\n")
        _record_current(git_repo)  # installed from the dirty tree
        subprocess.run(["git", "checkout", "--", "src/mod.py"], cwd=git_repo, check=True)
        # Same HEAD, clean tree - but not the bytes the runtime was built from.
        assert setup_mod.install_is_current() is False

    def test_a_snapshot_export_is_not_a_rebuild_trigger(self, git_repo):
        _record_current(git_repo)
        (git_repo / ".agent-memory").mkdir()
        (git_repo / ".agent-memory" / "memory.duckdb").write_bytes(b"x")
        assert setup_mod.install_is_current() is True

    def test_ignored_build_output_is_not_a_rebuild_trigger(self, git_repo):
        (git_repo / ".gitignore").write_text("__pycache__/\n")
        _record_current(git_repo)
        (git_repo / "src" / "__pycache__").mkdir()
        (git_repo / "src" / "__pycache__" / "x.pyc").write_bytes(b"\0")
        assert setup_mod.install_is_current() is True

    def test_no_recorded_tree_means_rebuild(self, git_repo, monkeypatch):
        import memory_mcp.db.registry as reg

        monkeypatch.setattr(reg, "get_setting", lambda key: None)
        assert setup_mod.install_is_current() is False


class TestRuntimeInstallUsesTheLock:
    def test_install_is_constrained_by_the_exported_lock(self, git_repo, tmp_path, monkeypatch):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            if args[:2] == ["uv", "export"]:
                out = args[args.index("--output-file") + 1]
                Path(out).write_text("duckdb==1.5.6\n")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(setup_mod, "_safe_install_tree_digest", lambda: None)
        setup_mod.setup_runtime()
        install = next(c for c in calls if c[:3] == ["uv", "pip", "install"])
        constraint = Path(install[install.index("--constraint") + 1])
        assert constraint.read_text() == "duckdb==1.5.6\n"
        assert install[-1] == str(git_repo)

    def test_no_lock_still_installs_from_pyproject(self, git_repo, monkeypatch):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            code = 2 if args[:2] == ["uv", "export"] else 0
            return subprocess.CompletedProcess(args, code, "", "no lock")

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(setup_mod, "_safe_install_tree_digest", lambda: None)
        setup_mod.setup_runtime()
        install = next(c for c in calls if c[:3] == ["uv", "pip", "install"])
        assert "--constraint" not in install


def test_pyproject_requires_the_duckdb_with_the_crash_fixes():
    import tomllib

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    assert "duckdb>=1.5.6" in deps
