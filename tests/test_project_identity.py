"""A project is identified by the uid in its committed snapshot, not its path.

Before this, moving a folder (or renaming it) made the next Claude session
register a second project, because detection matched on the bound path and then
on the folder name. The uid lives in .claude-memory/manifest.json, which is
committed, so it survives a move, a rename, and a teammate's clone.
"""

import json

import pytest

from memory_mcp.constants import MANIFEST_NAME, SNAPSHOT_DIRNAME
from memory_mcp.container import container
from memory_mcp.context import detect_project_from_cwd
from memory_mcp.repositories import ProjectRepository

UID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def repo():
    return ProjectRepository()


def _folder(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _write_manifest(folder, project_id, slug):
    snap = folder / SNAPSHOT_DIRNAME
    snap.mkdir(parents=True, exist_ok=True)
    (snap / MANIFEST_NAME).write_text(
        json.dumps({"version": 1, "project_id": project_id, "slug": slug,
                    "categories": []})
    )


def test_new_uid_registers_the_project(tmp_path, repo):
    folder = _folder(tmp_path, "fresh-clone")
    result = container.project_service.claim_folder(str(folder), UID, "fresh-clone")

    assert result["action"] == "created"
    assert repo.get(result["slug"]).project_uid == UID


def test_same_folder_twice_is_a_no_op(tmp_path):
    folder = _folder(tmp_path, "stable")
    container.project_service.claim_folder(str(folder), UID, "stable")

    assert container.project_service.claim_folder(
        str(folder), UID, "stable"
    )["action"] == "matched"


def test_moved_folder_rebinds_instead_of_duplicating(tmp_path, repo):
    original = _folder(tmp_path, "myproject")
    container.project_service.claim_folder(str(original), UID, "myproject")
    before = len(repo.list_all())

    moved = _folder(tmp_path / "elsewhere", "myproject")
    result = container.project_service.claim_folder(str(moved), UID, "myproject")

    assert result["action"] == "rebound"
    assert result["slug"] == "myproject"
    assert repo.get("myproject").project_path == str(moved)
    assert len(repo.list_all()) == before


def test_renamed_folder_rebinds_too(tmp_path, repo):
    original = _folder(tmp_path, "oldname")
    container.project_service.claim_folder(str(original), UID, "oldname")
    before = len(repo.list_all())

    renamed = _folder(tmp_path, "totally-different-name")
    result = container.project_service.claim_folder(str(renamed), UID, "oldname")

    assert result["action"] == "rebound"
    assert result["slug"] == "oldname"  # the slug is stable; only the path moved
    assert len(repo.list_all()) == before


def test_locally_known_project_adopts_the_committed_uid(tmp_path, repo):
    """A teammate registered this repo locally, then pulled it. Same folder, so
    it is the same project: the committed uid wins, no duplicate appears."""
    folder = _folder(tmp_path, "shared-repo")
    container.project_service.init_project(
        "shared-repo", "shared-repo", project_path=str(folder)
    )
    local_uid = repo.get("shared-repo").project_uid
    before = len(repo.list_all())

    result = container.project_service.claim_folder(str(folder), UID, "shared-repo")

    assert result["action"] == "adopted"
    assert result["slug"] == "shared-repo"
    assert repo.get("shared-repo").project_uid == UID != local_uid
    assert len(repo.list_all()) == before


def test_unbound_project_adopts_the_committed_uid(tmp_path, repo):
    """A project with no folder bound yet takes both the uid and the folder."""
    folder = _folder(tmp_path, "unbound")
    container.project_service.init_project("unbound", "unbound")
    before = len(repo.list_all())

    result = container.project_service.claim_folder(str(folder), UID, "unbound")

    assert result["action"] == "adopted"
    assert repo.get("unbound").project_uid == UID
    assert repo.get("unbound").project_path == str(folder)
    assert len(repo.list_all()) == before


def test_same_folder_name_different_project_gets_its_own_slug(tmp_path, repo):
    """Two unrelated repos both called `api` must not collide."""
    first = _folder(tmp_path / "org-a", "api")
    container.project_service.claim_folder(str(first), UID, "api")

    second = _folder(tmp_path / "org-b", "api")
    result = container.project_service.claim_folder(str(second), "99999999-0000-0000-0000-000000000000", "api")

    assert result["action"] == "created"
    assert result["slug"] == "api-2"
    assert repo.get("api").project_path == str(first)


def test_detection_follows_the_manifest_uid(tmp_path, repo):
    """Detection must find the project even when path and name both say otherwise."""
    registered = _folder(tmp_path, "somewhere")
    container.project_service.claim_folder(str(registered), UID, "somewhere")

    moved = _folder(tmp_path, "unrelated-folder-name")
    _write_manifest(moved, UID, "somewhere")

    assert detect_project_from_cwd(str(moved)) == "somewhere"


def test_detection_from_a_subdirectory(tmp_path):
    folder = _folder(tmp_path, "withsubdirs")
    container.project_service.claim_folder(str(folder), UID, "withsubdirs")
    _write_manifest(folder, UID, "withsubdirs")
    nested = folder / "src" / "deep"
    nested.mkdir(parents=True)

    assert detect_project_from_cwd(str(nested)) == "withsubdirs"


def test_unreadable_manifest_falls_back_to_path_detection(tmp_path):
    """A conflicted or half-written manifest must never break detection."""
    folder = _folder(tmp_path, "conflicted")
    container.project_service.init_project(
        "conflicted", "conflicted", project_path=str(folder)
    )
    snap = folder / SNAPSHOT_DIRNAME
    snap.mkdir()
    (snap / MANIFEST_NAME).write_text("<<<<<<< HEAD\nnot json at all\n")

    assert detect_project_from_cwd(str(folder)) == "conflicted"


def test_claim_without_a_uid_falls_back_to_detection(tmp_path):
    folder = _folder(tmp_path, "no-manifest-yet")
    container.project_service.init_project(
        "no-manifest-yet", "x", project_path=str(folder)
    )

    result = container.project_service.claim_folder(str(folder), None, None)

    assert result == {"slug": "no-manifest-yet", "action": "unclaimed"}
