"""The git merge driver, driven end to end through real git.

Not `merge_snapshots()` called directly - two clones, real commits, a real
`git pull` that conflicts, and git invoking `memory-mcp merge-snapshot %O %A %B`
by way of the `.gitattributes` the export writes. A unit test of the SQL would
pass while the driver was never wired up at all, which is the failure mode that
matters: an unregistered driver looks exactly like a working one until the day
somebody loses a rule.

THE FOUR CASES, and why each is here:

- a rule added on each side -> BOTH survive. The default "take one side" is what
  this whole driver exists to prevent.
- a rule edited on both sides -> the newer `updated_at` wins, the same
  last-write-wins the rest of the sync already applies.
- a rule hard-deleted on one side, untouched on the other -> stays deleted. A
  tombstone is the only thing that deletes.
- a rule MISSING from one side with no tombstone -> survives. Absence is not
  deletion; it is usually a row that side has not pulled yet. Getting this
  backwards turns every pull into silent data loss.
"""

import os
import shutil
import subprocess
import sys

import pytest

from memory_mcp.constants import (
    GITATTRIBUTES_NAME, MANIFEST_NAME, MERGE_DRIVER_NAME, SNAPSHOT_DB_NAME,
    SNAPSHOT_DIRNAME,
)
from memory_mcp.db.snapshot import read_snapshot, write_snapshot

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)

PROJECT_UID = "uid-merge-test"

_GITATTRIBUTES = (
    f"{SNAPSHOT_DB_NAME} merge={MERGE_DRIVER_NAME}\n"
    f"{SNAPSHOT_DB_NAME} -diff\n"
    f"{SNAPSHOT_DB_NAME} -text\n"
)


def _git(repo, *args, check=True):
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {repo}:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def _configure(repo):
    """Identity, plus the merge driver - in the LOCAL config only.

    Never `--global`: a test must not write to the developer's ~/.gitconfig.
    `memory-mcp-setup` registers exactly these two keys globally on a real
    machine (setup.setup_merge_driver).
    """
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Merge Test")
    _git(repo, "config", "commit.gpgsign", "false")
    driver = f'"{sys.executable}" -m memory_mcp.cli merge-snapshot %O %A %B %P'
    _git(repo, "config", f"merge.{MERGE_DRIVER_NAME}.name", "memory-mcp snapshot")
    _git(repo, "config", f"merge.{MERGE_DRIVER_NAME}.driver", driver)
    _git(repo, "config", f"merge.{MERGE_DRIVER_NAME}.recursive", "binary")


def _memory(mid, title, content, updated, category="mandatory_rules"):
    return {
        "id": mid, "category": category, "title": title, "content": content,
        "summary": None, "tags": [], "metadata": None, "status": "active",
        "priority": 2, "source": "user", "related_ids": [], "entities": [],
        "expires_at": None, "created_at": "2026-01-01T00:00:00",
        "updated_at": updated, "created_by": None,
        "approval_status": "approved", "approved_by": None, "approved_at": None,
    }


def _snapshot_dir(repo):
    return repo / SNAPSHOT_DIRNAME


def _write(repo, memories, tombstones=None, provenance=None):
    """Write the snapshot the way `memory-mcp sync export` does."""
    snap = _snapshot_dir(repo)
    snap.mkdir(parents=True, exist_ok=True)
    categories = {}
    for mem in memories:
        categories.setdefault(mem["category"], []).append(mem)
    write_snapshot(
        snap / SNAPSHOT_DB_NAME, project_id=PROJECT_UID, slug="merge-test",
        categories=categories, tombstones=tombstones or [],
        provenance=provenance or [],
    )
    (snap / MANIFEST_NAME).write_text(
        '{"version": 2, "project_id": "%s", "slug": "merge-test"}' % PROJECT_UID
    )
    (snap / GITATTRIBUTES_NAME).write_text(_GITATTRIBUTES)


def _read(repo):
    """{id: memory} out of the repo's snapshot, across every category."""
    snapshot = read_snapshot(_snapshot_dir(repo) / SNAPSHOT_DB_NAME)
    return (
        {m["id"]: m for items in snapshot["categories"].values() for m in items},
        {t["memory_id"]: t for t in snapshot["tombstones"]},
        snapshot["provenance"],
    )


@pytest.fixture
def clones(tmp_path):
    """A bare origin and two clones that have both seen one shared commit.

    That shared commit is what gives git a merge BASE, which is the realistic
    shape: two people who pulled yesterday and both worked today.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))

    alice = tmp_path / "alice"
    _git(tmp_path, "clone", str(origin), str(alice))
    _configure(alice)

    _write(alice, [_memory("r0", "Shared rule", "the base", "2026-01-01T00:00:00")])
    _git(alice, "add", "-A")
    _git(alice, "commit", "-m", "base snapshot")
    _git(alice, "push", "-u", "origin", "main")

    bob = tmp_path / "bob"
    _git(tmp_path, "clone", str(origin), str(bob))
    _configure(bob)

    return alice, bob


def _diverge_and_merge(alice, bob, alice_state, bob_state):
    """Alice pushes her state, Bob commits his, Bob pulls. Returns the pull."""
    _write(alice, **alice_state)
    _git(alice, "add", "-A")
    _git(alice, "commit", "-m", "alice")
    _git(alice, "push", "origin", "main")

    _write(bob, **bob_state)
    _git(bob, "add", "-A")
    _git(bob, "commit", "-m", "bob")
    return _git(bob, "pull", "--no-rebase", "origin", "main", check=False)


class TestMergeThroughGit:
    def test_a_rule_added_on_each_side_leaves_both(self, clones):
        alice, bob = clones
        base = _memory("r0", "Shared rule", "the base", "2026-01-01T00:00:00")

        pull = _diverge_and_merge(
            alice, bob,
            {"memories": [base, _memory("ra", "Alice's rule", "from alice",
                                        "2026-02-01T00:00:00")]},
            {"memories": [base, _memory("rb", "Bob's rule", "from bob",
                                        "2026-02-02T00:00:00")]},
        )

        assert pull.returncode == 0, f"pull did not merge:\n{pull.stderr}"
        memories, _, _ = _read(bob)
        assert set(memories) == {"r0", "ra", "rb"}
        assert memories["ra"]["content"] == "from alice"
        assert memories["rb"]["content"] == "from bob"

    def test_a_rule_edited_on_both_sides_resolves_to_the_newer(self, clones):
        alice, bob = clones

        pull = _diverge_and_merge(
            alice, bob,
            {"memories": [_memory("r0", "Shared rule", "ALICE edited later",
                                  "2026-03-05T00:00:00")]},
            {"memories": [_memory("r0", "Shared rule", "bob edited earlier",
                                  "2026-03-01T00:00:00")]},
        )

        assert pull.returncode == 0, f"pull did not merge:\n{pull.stderr}"
        memories, _, _ = _read(bob)
        assert memories["r0"]["content"] == "ALICE edited later"
        assert memories["r0"]["updated_at"] == "2026-03-05T00:00:00"

    def test_the_newer_side_wins_regardless_of_which_clone_it_is(self, clones):
        """Same case, other way round - so the test cannot pass by preferring
        'theirs' or 'ours' by accident."""
        alice, bob = clones

        pull = _diverge_and_merge(
            alice, bob,
            {"memories": [_memory("r0", "Shared rule", "alice edited earlier",
                                  "2026-03-01T00:00:00")]},
            {"memories": [_memory("r0", "Shared rule", "BOB edited later",
                                  "2026-03-05T00:00:00")]},
        )

        assert pull.returncode == 0, f"pull did not merge:\n{pull.stderr}"
        memories, _, _ = _read(bob)
        assert memories["r0"]["content"] == "BOB edited later"

    def test_a_rule_deleted_on_one_side_stays_deleted(self, clones):
        """Alice hard-deletes r0: her snapshot has no row and one tombstone."""
        alice, bob = clones
        base = _memory("r0", "Shared rule", "the base", "2026-01-01T00:00:00")

        pull = _diverge_and_merge(
            alice, bob,
            {
                "memories": [_memory("ra", "Alice's rule", "from alice",
                                     "2026-02-01T00:00:00")],
                "tombstones": [{"memory_id": "r0", "category": "mandatory_rules",
                                "title": "Shared rule",
                                "deleted_at": "2026-02-01T00:00:00",
                                "actor": "alice"}],
            },
            {"memories": [base, _memory("rb", "Bob's rule", "from bob",
                                        "2026-02-02T00:00:00")]},
        )

        assert pull.returncode == 0, f"pull did not merge:\n{pull.stderr}"
        memories, tombstones, _ = _read(bob)
        assert "r0" not in memories, "a tombstoned rule came back"
        # ...and both sides' additions still survived the same merge.
        assert set(memories) == {"ra", "rb"}
        # The tombstone is carried forward, or the next machine to export would
        # re-add the rule from its own copy.
        assert "r0" in tombstones

    def test_absence_without_a_tombstone_is_not_a_deletion(self, clones):
        """THE TRAP. Bob's side simply has not got r0 - no tombstone anywhere.

        A three-way merge that read "in the base, gone from one side" as a
        delete would drop it. It must survive: that shape is a clone that has
        not pulled yet, and treating it as a delete makes every pull lossy.
        """
        alice, bob = clones
        base = _memory("r0", "Shared rule", "the base", "2026-01-01T00:00:00")

        pull = _diverge_and_merge(
            alice, bob,
            {"memories": [base, _memory("ra", "Alice's rule", "from alice",
                                        "2026-02-01T00:00:00")]},
            # Bob's export dropped r0 with no tombstone - a stale or partial
            # snapshot, not a deletion.
            {"memories": [_memory("rb", "Bob's rule", "from bob",
                                  "2026-02-02T00:00:00")]},
        )

        assert pull.returncode == 0, f"pull did not merge:\n{pull.stderr}"
        memories, _, _ = _read(bob)
        assert "r0" in memories, "absence was treated as deletion - data loss"
        assert set(memories) == {"r0", "ra", "rb"}

    def test_an_edit_newer_than_the_tombstone_resurrects_the_rule(self, clones):
        """Somebody re-added the rule after it was deleted. The edit wins, and
        the tombstone is dropped so the next merge does not re-kill it."""
        alice, bob = clones

        pull = _diverge_and_merge(
            alice, bob,
            {
                "memories": [],
                "tombstones": [{"memory_id": "r0", "category": "mandatory_rules",
                                "title": "Shared rule",
                                "deleted_at": "2026-02-01T00:00:00",
                                "actor": "alice"}],
            },
            {"memories": [_memory("r0", "Shared rule", "re-added on purpose",
                                  "2026-04-01T00:00:00")]},
        )

        assert pull.returncode == 0, f"pull did not merge:\n{pull.stderr}"
        memories, tombstones, _ = _read(bob)
        assert memories["r0"]["content"] == "re-added on purpose"
        assert "r0" not in tombstones

    def test_provenance_is_unioned_not_resolved(self, clones):
        """An audit trail loses entries under last-write-wins, so it is unioned.

        The overlapping entry is deduped on its natural key - the id column is a
        local auto-increment integer and means nothing across clones.
        """
        alice, bob = clones
        base = _memory("r0", "Shared rule", "the base", "2026-01-01T00:00:00")
        shared = {"memory_id": "r0", "operation": "create", "details": None,
                  "actor": None, "created_at": "2026-01-01T00:00:00"}

        pull = _diverge_and_merge(
            alice, bob,
            {"memories": [base], "provenance": [
                shared,
                {"memory_id": "r0", "operation": "update", "details": None,
                 "actor": "alice", "created_at": "2026-02-01T00:00:00"},
            ]},
            {"memories": [base], "provenance": [
                shared,
                {"memory_id": "r0", "operation": "update", "details": None,
                 "actor": "bob", "created_at": "2026-02-02T00:00:00"},
            ]},
        )

        assert pull.returncode == 0, f"pull did not merge:\n{pull.stderr}"
        _, _, provenance = _read(bob)
        actors = sorted(str(p["actor"]) for p in provenance)
        assert actors == ["None", "alice", "bob"], actors


class TestMergeRefusals:
    def test_a_corrupt_side_is_left_as_a_conflict_not_guessed(self, clones):
        """Exit non-zero, and git records a conflict for a human.

        The alternative - picking whichever side is readable - is the silent
        loss this driver exists to stop, dressed up as a successful merge.
        """
        alice, bob = clones
        _write(alice, [_memory("ra", "Alice's rule", "from alice",
                               "2026-02-01T00:00:00")])
        (_snapshot_dir(alice) / SNAPSHOT_DB_NAME).write_text("not a database")
        _git(alice, "add", "-A")
        _git(alice, "commit", "-m", "corrupt")
        _git(alice, "push", "origin", "main")

        _write(bob, [_memory("rb", "Bob's rule", "from bob", "2026-02-02T00:00:00")])
        _git(bob, "add", "-A")
        _git(bob, "commit", "-m", "bob")
        pull = _git(bob, "pull", "--no-rebase", "origin", "main", check=False)

        assert pull.returncode != 0
        assert "conflict" in (pull.stdout + pull.stderr).lower()
        # Bob's own memories are untouched: a refused merge changes nothing.
        memories, _, _ = _read(bob)
        assert set(memories) == {"rb"}

    def test_the_driver_reports_a_conflict_when_it_is_not_registered(self, tmp_path):
        """A clone WITHOUT memory-mcp installed must fail visibly, not quietly.

        This is why the snapshot is marked `merge=<driver>` rather than
        `merge=ours` or `binary`: with no driver by that name git falls back to
        an ordinary binary conflict, which someone has to look at.
        """
        origin = tmp_path / "origin.git"
        _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
        alice = tmp_path / "a"
        _git(tmp_path, "clone", str(origin), str(alice))
        _git(alice, "config", "user.email", "t@example.invalid")
        _git(alice, "config", "user.name", "T")
        _write(alice, [_memory("r0", "Shared", "base", "2026-01-01T00:00:00")])
        _git(alice, "add", "-A")
        _git(alice, "commit", "-m", "base")
        _git(alice, "push", "-u", "origin", "main")

        bob = tmp_path / "b"
        _git(tmp_path, "clone", str(origin), str(bob))
        _git(bob, "config", "user.email", "t@example.invalid")
        _git(bob, "config", "user.name", "T")
        # deliberately NO merge driver configured here

        _write(alice, [_memory("ra", "Alice", "a", "2026-02-01T00:00:00")])
        _git(alice, "add", "-A")
        _git(alice, "commit", "-m", "alice")
        _git(alice, "push", "origin", "main")
        _write(bob, [_memory("rb", "Bob", "b", "2026-02-02T00:00:00")])
        _git(bob, "add", "-A")
        _git(bob, "commit", "-m", "bob")

        pull = _git(bob, "pull", "--no-rebase", "origin", "main", check=False)
        assert pull.returncode != 0
        assert "conflict" in (pull.stdout + pull.stderr).lower()


def test_setup_registers_the_driver_with_the_runtime_binary():
    """The command git runs must be the runtime venv's binary, not bare PATH.

    git runs the driver from inside a merge with whatever environment started it
    - a GUI client, an IDE, a hook - where PATH is not the shell's.
    """
    from memory_mcp.setup import merge_driver_command, runtime_dir

    command = merge_driver_command()
    assert str(runtime_dir() / "bin" / "memory-mcp") in command
    assert command.endswith("merge-snapshot %O %A %B %P")
