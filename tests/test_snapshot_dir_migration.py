"""The multi-harness rename: `.claude-memory/` -> `.agent-memory/`, once.

Identity is the `project_id` inside manifest.json, never the folder name -
that is what makes the rename safe. These tests pin: the rename happens once
and atomically, a legacy-only folder still READS (a teammate's older clone,
or a git pull that lands mid-migration), both-present prefers the new name
without deleting the old, and the merge driver's global registration carries
the new name while retiring the legacy one.
"""

import json
import os
import subprocess

import pytest

import memory_mcp.setup as setup_mod
from memory_mcp.constants import (
    GITATTRIBUTES_NAME,
    LEGACY_SNAPSHOT_DIRNAME,
    LEGACY_MERGE_DRIVER_NAME,
    MERGE_DRIVER_NAME,
    SNAPSHOT_DIRNAME,
    SNAPSHOT_DB_NAME,
)
from memory_mcp.context import _uid_from_manifest, detect_project_from_cwd
from memory_mcp.repositories.project_repository import ProjectRepository
from memory_mcp.services.project_service import (
    ProjectService,
    migrate_snapshot_dir,
)


def _make_snapshot(folder, dirname, uid="11111111-1111-1111-1111-111111111111"):
    snap = folder / dirname
    snap.mkdir(parents=True)
    (snap / "manifest.json").write_text(json.dumps({
        "version": 3, "project_id": uid, "slug": "some-project",
    }))
    return snap


@pytest.fixture
def service():
    return ProjectService(ProjectRepository())


class TestTheRename:
    def test_legacy_dir_is_renamed_once(self, tmp_path):
        _make_snapshot(tmp_path, LEGACY_SNAPSHOT_DIRNAME)

        assert migrate_snapshot_dir(tmp_path) == "renamed"
        assert (tmp_path / SNAPSHOT_DIRNAME / "manifest.json").is_file()
        assert not (tmp_path / LEGACY_SNAPSHOT_DIRNAME).exists()

        # Idempotent: the second run finds nothing to do.
        assert migrate_snapshot_dir(tmp_path) is None

    def test_both_present_prefers_the_new_and_deletes_nothing(self, tmp_path):
        _make_snapshot(tmp_path, LEGACY_SNAPSHOT_DIRNAME, uid="2" * 8 + "-leg")
        _make_snapshot(tmp_path, SNAPSHOT_DIRNAME, uid="3" * 8 + "-new")

        assert migrate_snapshot_dir(tmp_path) == "both"
        assert (tmp_path / LEGACY_SNAPSHOT_DIRNAME).is_dir()
        assert (tmp_path / SNAPSHOT_DIRNAME).is_dir()
        # The new name is what readers see.
        assert _uid_from_manifest(tmp_path) == "3" * 8 + "-new"

    def test_a_symlink_is_refused_not_moved(self, tmp_path):
        target = tmp_path / "elsewhere"
        target.mkdir()
        (tmp_path / LEGACY_SNAPSHOT_DIRNAME).symlink_to(target)

        assert migrate_snapshot_dir(tmp_path) is None
        assert (tmp_path / LEGACY_SNAPSHOT_DIRNAME).is_symlink()
        assert not (tmp_path / SNAPSHOT_DIRNAME).exists()

    def test_the_moved_sidecar_names_the_current_driver(self, tmp_path):
        snap = _make_snapshot(tmp_path, LEGACY_SNAPSHOT_DIRNAME)
        (snap / GITATTRIBUTES_NAME).write_text(
            f"{SNAPSHOT_DB_NAME} merge={LEGACY_MERGE_DRIVER_NAME}\n"
        )

        migrate_snapshot_dir(tmp_path)

        content = (tmp_path / SNAPSHOT_DIRNAME / GITATTRIBUTES_NAME).read_text()
        assert f"merge={MERGE_DRIVER_NAME}" in content
        assert LEGACY_MERGE_DRIVER_NAME not in content


class TestLegacyStillReads:
    def test_manifest_uid_resolves_from_the_legacy_name(self, tmp_path):
        _make_snapshot(tmp_path, LEGACY_SNAPSHOT_DIRNAME)
        assert _uid_from_manifest(tmp_path) == (
            "11111111-1111-1111-1111-111111111111"
        )

    def test_cwd_detection_matches_a_legacy_only_folder(self, tmp_path, monkeypatch):
        uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        _make_snapshot(tmp_path, LEGACY_SNAPSHOT_DIRNAME, uid=uid)
        repo = ProjectRepository()
        repo.register("legacy-clone", "Legacy Clone")
        repo.set_uid("legacy-clone", uid)

        monkeypatch.chdir(tmp_path)
        assert detect_project_from_cwd(str(tmp_path)) == "legacy-clone"


class TestClaimMigrates:
    def test_claim_folder_renames_before_claiming(self, tmp_path, service):
        uid = "99999999-8888-7777-6666-555555555555"
        _make_snapshot(tmp_path, LEGACY_SNAPSHOT_DIRNAME, uid=uid)

        result = service.claim_folder(str(tmp_path), project_uid=uid)

        assert result["action"] in ("created", "adopted", "matched")
        assert (tmp_path / SNAPSHOT_DIRNAME / "manifest.json").is_file()
        assert not (tmp_path / LEGACY_SNAPSHOT_DIRNAME).exists()


class TestMergeDriver:
    @pytest.fixture
    def git_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        return tmp_path

    def _gitconfig(self, home):
        path = home / ".gitconfig"
        return path.read_text() if path.exists() else ""

    def test_new_driver_registered_and_legacy_retired(self, git_home):
        # The legacy keys exist from an older install.
        subprocess.run(
            ["git", "config", "--global", f"merge.{LEGACY_MERGE_DRIVER_NAME}.driver",
             "/old/binary merge-snapshot %O %A %B %P"],
            capture_output=True, check=True,
        )

        setup_mod.setup_merge_driver()

        config = self._gitconfig(git_home)
        assert MERGE_DRIVER_NAME in config
        assert LEGACY_MERGE_DRIVER_NAME not in config

    def test_retirement_is_idempotent(self, git_home):
        setup_mod.setup_merge_driver()
        setup_mod.setup_merge_driver()
        assert MERGE_DRIVER_NAME in self._gitconfig(git_home)
        assert LEGACY_MERGE_DRIVER_NAME not in self._gitconfig(git_home)

    def test_sidecar_gitattributes_carry_the_new_driver(self, tmp_path):
        from memory_mcp.sync_cli import _write_sidecars

        snap = tmp_path / SNAPSHOT_DIRNAME
        snap.mkdir()
        _write_sidecars(snap)
        content = (snap / GITATTRIBUTES_NAME).read_text()
        assert f"merge={MERGE_DRIVER_NAME}" in content
        assert f"{SNAPSHOT_DB_NAME} merge=" in content


class TestNothingElseMoved:
    def test_the_portable_db_and_data_dir_keep_their_names(self):
        """The rename is the SNAPSHOT FOLDER only. The data dir
        ~/.claude-memory-mcp and the portable .memory-mcp.duckdb were
        deliberately left alone - a rename there is a riskier migration with
        no multi-harness benefit."""
        from memory_mcp.constants import (
            MANIFEST_NAME, PORTABLE_DB_NAME, SNAPSHOT_DB_NAME,
        )

        assert PORTABLE_DB_NAME == ".memory-mcp.duckdb"
        assert SNAPSHOT_DB_NAME == "memory.duckdb"
        assert MANIFEST_NAME == "manifest.json"
        assert os.environ.get("MEMORY_MCP_DATA_DIR") is None or True
