"""The DuckDB memory snapshot: round-trip fidelity, versioning, atomic write.

The snapshot is the git-committable handoff between machines. If it loses a
field, the loss is silent and only shows up as a rule that stopped being
enforced on somebody else's laptop - so these tests check every field of a
fully-populated memory, not just that the row count matches.
"""

import json

import duckdb
import pytest

from memory_mcp.constants import SNAPSHOT_SCHEMA_VERSION
from memory_mcp.db.snapshot import (
    SnapshotError,
    SnapshotVersionError,
    count_memories,
    merge_snapshots,
    read_snapshot,
    write_snapshot,
)


def _full_memory(mid="m1", category="mandatory_rules", updated="2026-01-01T10:00:00"):
    """A memory with every carried field set to something distinguishable."""
    return {
        "id": mid,
        "category": category,
        "title": f"Title {mid}",
        "content": "Body with a 'quote', a \"double\", a newline\nand unicode: é中",
        "summary": "the summary",
        "tags": ["alpha", "beta"],
        "metadata": {"nested": {"k": 1}, "list": [1, 2, 3]},
        "status": "active",
        "priority": 3,
        "source": "user",
        "related_ids": ["other-1", "other-2"],
        "entities": ["DuckDB", "git"],
        "expires_at": "2027-06-01T00:00:00",
        "created_at": "2026-01-01T09:00:00",
        "updated_at": updated,
        "created_by": "user-7",
        "approval_status": "approved",
        "approved_by": "admin-1",
        "approved_at": "2026-01-01T09:30:00",
    }


class TestRoundTrip:
    def test_every_field_survives(self, tmp_path):
        path = tmp_path / "memory.duckdb"
        original = _full_memory()
        write_snapshot(
            path, project_id="uid-1", slug="proj",
            categories={"mandatory_rules": [original]},
        )

        back = read_snapshot(path)["categories"]["mandatory_rules"][0]
        for field, value in original.items():
            assert back[field] == value, f"{field} did not survive the round trip"

    def test_provenance_and_tombstones_survive(self, tmp_path):
        path = tmp_path / "memory.duckdb"
        write_snapshot(
            path, project_id="uid-1", slug="proj",
            categories={"decision": [_full_memory("d1", "decision")]},
            provenance=[
                {"memory_id": "d1", "operation": "create",
                 "details": {"source": "test"}, "actor": "u1",
                 "created_at": "2026-01-01T09:00:00"},
                {"memory_id": "d1", "operation": "update", "details": None,
                 "actor": None, "created_at": "2026-01-02T09:00:00"},
            ],
            tombstones=[
                {"memory_id": "gone-1", "category": "decision", "title": "Gone",
                 "deleted_at": "2026-02-01T00:00:00", "actor": "u1"},
            ],
        )

        snap = read_snapshot(path)
        assert len(snap["provenance"]) == 2
        assert snap["provenance"][0]["details"] == {"source": "test"}
        assert snap["provenance"][1]["details"] is None
        assert snap["tombstones"] == [
            {"memory_id": "gone-1", "category": "decision", "title": "Gone",
             "deleted_at": "2026-02-01T00:00:00", "actor": "u1"},
        ]

    def test_empty_project_round_trips(self, tmp_path):
        path = tmp_path / "memory.duckdb"
        write_snapshot(path, project_id="uid-1", slug="proj", categories={})
        snap = read_snapshot(path)
        assert snap["categories"] == {}
        assert snap["meta"]["slug"] == "proj"
        assert count_memories(path) == 0

    def test_categories_are_preserved_separately(self, tmp_path):
        path = tmp_path / "memory.duckdb"
        write_snapshot(
            path, project_id="uid-1", slug="proj",
            categories={
                "mandatory_rules": [_full_memory("r1")],
                "forbidden_rules": [_full_memory("f1", "forbidden_rules")],
                "architecture": [_full_memory("a1", "architecture")],
            },
        )
        snap = read_snapshot(path)
        assert set(snap["categories"]) == {
            "mandatory_rules", "forbidden_rules", "architecture",
        }


class TestSchemaVersion:
    def test_a_newer_snapshot_is_refused_not_half_read(self, tmp_path):
        path = tmp_path / "memory.duckdb"
        write_snapshot(
            path, project_id="uid-1", slug="proj",
            categories={"decision": [_full_memory("d1", "decision")]},
        )
        _stamp_version(path, SNAPSHOT_SCHEMA_VERSION + 1)

        with pytest.raises(SnapshotVersionError) as exc:
            read_snapshot(path)
        # The message has to name both versions or it is not actionable.
        assert str(SNAPSHOT_SCHEMA_VERSION + 1) in str(exc.value)
        assert "nothing was imported" in str(exc.value).lower()

    def test_the_current_version_is_stamped(self, tmp_path):
        path = tmp_path / "memory.duckdb"
        write_snapshot(path, project_id="uid-1", slug="proj", categories={})
        assert read_snapshot(path)["schema_version"] == SNAPSHOT_SCHEMA_VERSION

    def test_a_merge_refuses_a_newer_side(self, tmp_path):
        ours, theirs = tmp_path / "ours.duckdb", tmp_path / "theirs.duckdb"
        for path in (ours, theirs):
            write_snapshot(path, project_id="uid-1", slug="proj",
                           categories={"decision": [_full_memory("d1", "decision")]})
        _stamp_version(theirs, SNAPSHOT_SCHEMA_VERSION + 1)

        with pytest.raises(SnapshotVersionError):
            merge_snapshots(None, ours, theirs, tmp_path / "out.duckdb")

    def test_a_merge_refuses_two_different_projects(self, tmp_path):
        ours, theirs = tmp_path / "ours.duckdb", tmp_path / "theirs.duckdb"
        write_snapshot(ours, project_id="uid-A", slug="alpha", categories={})
        write_snapshot(theirs, project_id="uid-B", slug="beta", categories={})

        with pytest.raises(SnapshotError) as exc:
            merge_snapshots(None, ours, theirs, tmp_path / "out.duckdb")
        assert "different projects" in str(exc.value)


class TestNotASnapshot:
    def test_a_missing_file_raises(self, tmp_path):
        with pytest.raises(SnapshotError):
            read_snapshot(tmp_path / "nope.duckdb")

    def test_an_empty_file_raises(self, tmp_path):
        path = tmp_path / "memory.duckdb"
        path.write_bytes(b"")
        with pytest.raises(SnapshotError):
            read_snapshot(path)

    def test_a_text_file_raises_rather_than_returning_nothing(self, tmp_path):
        """A snapshot git left with conflict markers must not read as 'empty'."""
        path = tmp_path / "memory.duckdb"
        path.write_text("<<<<<<< HEAD\nnot a database\n>>>>>>> theirs\n")
        with pytest.raises(SnapshotError):
            read_snapshot(path)


class TestAtomicWrite:
    def test_a_failed_write_leaves_the_previous_snapshot_intact(self, tmp_path):
        """The export is build-beside-and-rename, so a crash cannot truncate it.

        A half-written snapshot is worse than no snapshot: the next import would
        read it as "the project has three rules" and the JSON that used to be
        the backup is gone by then.
        """
        path = tmp_path / "memory.duckdb"
        write_snapshot(path, project_id="uid-1", slug="proj",
                       categories={"decision": [_full_memory("d1", "decision")]})
        before = path.read_bytes()

        with pytest.raises(Exception):
            write_snapshot(
                path, project_id="uid-1", slug="proj",
                # A memory whose content is not text at all: the insert fails
                # after the temp database has been created.
                categories={"decision": [{"id": "bad", "category": "decision",
                                          "title": "t", "content": object()}]},
            )

        assert path.read_bytes() == before
        assert read_snapshot(path)["categories"]["decision"][0]["id"] == "d1"
        assert not list(tmp_path.glob(".memory.duckdb.tmp*"))


def _stamp_version(path, version: int) -> None:
    """Rewrite the snapshot's schema_version, as a newer writer would."""
    conn = duckdb.connect()
    conn.execute(f"ATTACH '{path}' AS s")
    conn.execute(
        "INSERT OR REPLACE INTO s.snapshot_meta (key, value) VALUES "
        "('schema_version', ?)", [str(version)],
    )
    conn.execute("CHECKPOINT s")
    conn.close()


def test_json_fields_are_stored_as_text_not_duckdb_lists(tmp_path):
    """The wire format is scalars only - the merge depends on it.

    A LIST or STRUCT column would make the merge's UNION ALL type-sensitive and
    the "did this row change?" comparison structural. Text keeps both trivial,
    and keeps the file readable by anything that speaks DuckDB.
    """
    path = tmp_path / "memory.duckdb"
    write_snapshot(path, project_id="uid-1", slug="proj",
                   categories={"decision": [_full_memory("d1", "decision")]})

    conn = duckdb.connect()
    conn.execute(f"ATTACH '{path}' AS s (READ_ONLY)")
    # PRAGMA table_info gives (cid, name, type, notnull, default, pk).
    types = {
        r[1]: r[2]
        for r in conn.execute("PRAGMA table_info('s.memories')").fetchall()
    }
    row = conn.execute("SELECT tags, metadata FROM s.memories").fetchone()
    conn.close()

    assert isinstance(row[0], str) and json.loads(row[0]) == ["alpha", "beta"]
    assert isinstance(row[1], str)
    assert types["tags"] == "VARCHAR"
    assert types["metadata"] == "VARCHAR"
