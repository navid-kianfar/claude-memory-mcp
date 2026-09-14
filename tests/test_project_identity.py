"""A project is identified by the uid in its committed snapshot, not its path.

Before this, moving a folder (or renaming it) made the next Claude session
register a second project, because detection matched on the bound path and then
on the folder name. The uid lives in .claude-memory/manifest.json, which is
committed, so it survives a move, a rename, and a teammate's clone.

A LINKED WORKTREE is the exception that rule needed. It carries the same committed
uid as the checkout it came from, so "rebind to wherever the uid is seen" bound
projects to `.claude/worktrees/<task>` - observed in the live registry, not
theorised. Every absolute path in the real checkout then looked like it was outside
the project root, and the binding dangled the moment the worktree was removed.
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


# ---------- linked worktrees resolve, and bind nothing ----------


def _worktree(tmp_path, name, parent):
    """A folder shaped like `git worktree add` leaves one: `.git` is a FILE
    holding a gitdir pointer, not a directory."""
    folder = _folder(tmp_path, name)
    (folder / ".git").write_text(f"gitdir: {parent}/.git/worktrees/{name}\n")
    return folder


def test_a_worktree_of_a_known_project_resolves_without_rebinding(tmp_path, repo):
    main = _folder(tmp_path, "mainline")
    container.project_service.claim_folder(str(main), UID, "mainline")
    slug = repo.get_by_uid(UID).slug
    tree = _worktree(tmp_path, "wt-feature", main)

    result = container.project_service.claim_folder(str(tree), UID, "mainline")

    assert result == {"slug": slug, "action": "worktree"}
    assert repo.get(slug).project_path == str(main)


def test_a_claude_worktree_path_resolves_without_rebinding(tmp_path, repo):
    """The second witness: Claude Code's own agent worktrees, which may be real
    clones rather than git worktrees and so have a `.git` directory."""
    main = _folder(tmp_path, "mainline")
    container.project_service.claim_folder(str(main), UID, "mainline")
    slug = repo.get_by_uid(UID).slug
    tree = _folder(tmp_path, "mainline/.claude/worktrees/task-abc")
    (tree / ".git").mkdir()

    result = container.project_service.claim_folder(str(tree), UID, "mainline")

    assert result["action"] == "worktree"
    assert repo.get(slug).project_path == str(main)


def test_a_worktree_of_an_UNKNOWN_uid_registers_nothing(tmp_path, repo):
    """A fresh clone registers itself; a worktree must not. Registering one would
    create a project whose path vanishes when the worktree is removed."""
    main = _folder(tmp_path, "never-seen")
    before = len(repo.list_all())
    tree = _worktree(tmp_path, "wt-orphan", main)

    result = container.project_service.claim_folder(str(tree), UID, "never-seen")

    assert result["action"] == "unclaimed"
    assert len(repo.list_all()) == before
    assert repo.get_by_uid(UID) is None


def test_a_normal_checkout_with_a_git_directory_still_rebinds(tmp_path, repo):
    """The worktree rule must not cost a moved folder its rebind."""
    original = _folder(tmp_path, "ordinary")
    (original / ".git").mkdir()
    container.project_service.claim_folder(str(original), UID, "ordinary")
    moved = _folder(tmp_path, "ordinary-moved")
    (moved / ".git").mkdir()

    result = container.project_service.claim_folder(str(moved), UID, "ordinary")

    assert result["action"] == "rebound"
    assert repo.get(result["slug"]).project_path == str(moved)


def test_the_worktree_predicate_reads_both_witnesses(tmp_path):
    from memory_mcp.services.project_service import is_linked_worktree

    plain = _folder(tmp_path, "plain")
    (plain / ".git").mkdir()
    pointer = _worktree(tmp_path, "pointer", plain)
    claude = _folder(tmp_path, "x/.claude/worktrees/task-1")

    assert is_linked_worktree(plain) is False
    assert is_linked_worktree(pointer) is True
    assert is_linked_worktree(claude) is True
    # A folder with no .git at all is not a worktree either.
    assert is_linked_worktree(_folder(tmp_path, "bare")) is False
