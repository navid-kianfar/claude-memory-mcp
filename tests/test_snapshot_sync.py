"""`memory-mcp sync export|import` against the DuckDB snapshot.

These drive the real CLI functions with the daemon's HTTP layer replaced by a
direct call into a real Container - so the whole path runs: folder I/O, the
snapshot file format, SyncService, and the project DuckDB underneath.

The migration tests are the important ones. On a machine that has just cloned a
repo and not yet imported, `.claude-memory/*.json` is the ONLY copy of that
project's rules and decisions. A migration that deletes its source after a
truncated write is unrecoverable, so "it refuses to delete" is tested as
carefully as "it deletes".
"""

import json

import pytest

from memory_mcp import sync_cli
from memory_mcp.constants import (
    GITATTRIBUTES_NAME, MANIFEST_NAME, SNAPSHOT_DB_NAME, SNAPSHOT_DIRNAME,
)
from memory_mcp.container import Container
from memory_mcp.db.connection import get_connection
from memory_mcp.db.snapshot import read_snapshot, write_snapshot
from memory_mcp.models import MemoryCategory, StoreMemoryRequest


@pytest.fixture
def container():
    return Container()


@pytest.fixture
def fake_daemon(container, monkeypatch):
    """Route sync_cli's HTTP calls straight into a Container.

    The CLI and the daemon are two processes in real life precisely because the
    daemon owns the project DuckDB's write lock. In a test there is one process,
    so the transport is the only thing worth faking - the routes' logic is the
    thing under test and runs for real.
    """
    def _claimed_slug(payload):
        """What /api/hook/claim does: match the folder to a registered project.

        By committed uid first, then by bound path - the same order the real
        route uses, and the reason a moved folder rebinds instead of registering
        a duplicate.
        """
        uid = payload.get("project_id")
        if uid:
            project = container.project_repo.get_by_uid(uid)
            if project:
                return project.slug
        cwd = str(payload.get("cwd") or "")
        for project in container.project_repo.list_all():
            if project.project_path and cwd.startswith(project.project_path):
                return project.slug
        return payload.get("slug")

    def _daemon(path, method="GET", payload=None):
        if path == "/api/hook/claim":
            return {"action": "existing", "slug": _claimed_slug(payload)}
        if path.endswith("/sync-export"):
            slug = path.split("/")[3]
            return container.sync_service.build_full_snapshot(slug)
        if path.endswith("/sync-import"):
            slug = path.split("/")[3]
            result = container.sync_service.apply_snapshot(
                slug, payload["categories"], payload["reconcile"],
                provenance=payload.get("provenance"),
                tombstones=payload.get("tombstones"),
            )
            return {"status": "ok", **result}
        raise AssertionError(f"unexpected daemon call: {method} {path}")

    monkeypatch.setattr(sync_cli, "_daemon", _daemon)
    return _daemon


def _project(container, slug, path):
    container.project_repo.register(
        slug, slug, project_path=str(path), project_uid=f"uid-{slug}",
    )
    get_connection(slug).close()
    return slug


def _store(container, project, category, title, content):
    return container.memory_service.store(
        StoreMemoryRequest(
            project=project, category=MemoryCategory(category),
            title=title, content=content,
        )
    )


def _snap(repo):
    return repo / SNAPSHOT_DIRNAME


def _write_legacy_json(repo, categories):
    """Lay down a pre-DuckDB snapshot, the way every project on disk has one."""
    snap = _snap(repo)
    snap.mkdir(parents=True, exist_ok=True)
    for category, items in categories.items():
        (snap / f"{category}.json").write_text(json.dumps(items, indent=2))
    (snap / MANIFEST_NAME).write_text(json.dumps({
        "version": 1, "project_id": "uid-legacy", "slug": "legacy",
        "categories": sorted(categories),
    }))
    return snap


def _clone_snapshot(src_repo, dst_repo):
    """Copy a committed snapshot into another checkout, as `git clone` would.

    The manifest's `project_id` is dropped on the way: in real life the two
    checkouts are on different machines with separate registries, but here one
    registry holds both, and leaving the uid in would make the claim resolve the
    clone back to the SOURCE project - which is correct behaviour (identity
    travels with the repo) and would test nothing.
    """
    dst = dst_repo / SNAPSHOT_DIRNAME
    dst.mkdir(parents=True, exist_ok=True)
    (dst / SNAPSHOT_DB_NAME).write_bytes(
        (src_repo / SNAPSHOT_DIRNAME / SNAPSHOT_DB_NAME).read_bytes()
    )
    manifest = json.loads(
        (src_repo / SNAPSHOT_DIRNAME / MANIFEST_NAME).read_text()
    )
    manifest.pop("project_id", None)
    (dst / MANIFEST_NAME).write_text(json.dumps(manifest))
    return dst


def _legacy_memory(mid, category, title, content, updated="2026-01-01T10:00:00"):
    return {
        "id": mid, "category": category, "title": title, "content": content,
        "summary": None, "tags": ["t"], "metadata": None, "status": "active",
        "priority": 2, "source": "user", "related_ids": [], "entities": [],
        "expires_at": None, "created_at": "2026-01-01T09:00:00",
        "updated_at": updated, "created_by": None,
        "approval_status": "approved", "approved_by": None, "approved_at": None,
    }


class TestExport:
    def test_writes_the_database_manifest_and_gitattributes(
        self, container, fake_daemon, tmp_path,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        slug = _project(container, "expo", repo)
        _store(container, slug, "mandatory_rules", "R1", "always do this")

        sync_cli._export(str(repo))

        snap = _snap(repo)
        assert (snap / SNAPSHOT_DB_NAME).is_file()
        assert (snap / MANIFEST_NAME).is_file()
        # The .gitattributes is what points git at the merge driver. Without it
        # the driver is registered and never used, and a pull takes one side.
        text = (snap / GITATTRIBUTES_NAME).read_text()
        assert f"{SNAPSHOT_DB_NAME} merge=claude-memory-snapshot" in text

        manifest = json.loads((snap / MANIFEST_NAME).read_text())
        assert manifest["snapshot"] == SNAPSHOT_DB_NAME
        assert manifest["project_id"] == "uid-expo"

    def test_no_json_category_files_are_written(
        self, container, fake_daemon, tmp_path,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        slug = _project(container, "nojson", repo)
        _store(container, slug, "decision", "D1", "we chose duckdb")

        sync_cli._export(str(repo))

        assert not list(_snap(repo).glob("decision.json"))
        # The whole snapshot folder: the database, the identity manifest, and
        # the two sidecars that make git treat the database correctly.
        assert sorted(p.name for p in _snap(repo).iterdir()) == [
            GITATTRIBUTES_NAME, ".gitignore", MANIFEST_NAME, SNAPSHOT_DB_NAME,
        ]


class TestRoundTrip:
    def test_export_then_import_into_a_fresh_project_loses_nothing(
        self, container, fake_daemon, tmp_path,
    ):
        """The whole point of the snapshot: everything reaches the other machine."""
        repo = tmp_path / "repo"
        repo.mkdir()
        src = _project(container, "src", repo)
        rule = _store(container, src, "mandatory_rules", "R1", "always do this")
        forbidden = _store(container, src, "forbidden_rules", "F1", "never do that")
        decision = _store(container, src, "decision", "D1", "we chose duckdb")
        arch = _store(container, src, "architecture", "A1", "the daemon owns the lock")

        sync_cli._export(str(repo))

        # A second clone of the same repo folder, with an empty store.
        clone = tmp_path / "clone"
        clone.mkdir()
        _clone_snapshot(repo, clone)
        dst = _project(container, "dst", clone)

        sync_cli._import(str(clone))

        for original in (rule, forbidden, decision, arch):
            copied = container.memory_repo.get_by_id(dst, original.id)
            assert copied is not None, f"{original.title} did not survive"
            assert copied.title == original.title
            assert copied.content == original.content
            assert copied.category == original.category
            assert copied.priority == original.priority
        assert container.rules_service.get_rules(dst).total == 2

    def test_provenance_travels_with_the_memories(
        self, container, fake_daemon, tmp_path,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        src = _project(container, "psrc", repo)
        memory = _store(container, src, "decision", "D1", "we chose duckdb")
        sync_cli._export(str(repo))

        clone = tmp_path / "pclone"
        clone.mkdir()
        _clone_snapshot(repo, clone)
        dst = _project(container, "pdst", clone)

        sync_cli._import(str(clone))

        entries = container.provenance_repo.for_memory(dst, memory.id)
        assert [e.operation for e in entries].count("create") >= 1

    def test_a_second_import_adds_nothing(self, container, fake_daemon, tmp_path):
        """Idempotence: the SessionStart hook runs this on every session."""
        repo = tmp_path / "repo"
        repo.mkdir()
        src = _project(container, "idem", repo)
        _store(container, src, "mandatory_rules", "R1", "always")
        sync_cli._export(str(repo))

        before = len(container.provenance_repo.all_for_project(src))
        sync_cli._import(str(repo))
        sync_cli._import(str(repo))

        assert len(container.provenance_repo.all_for_project(src)) == before
        assert container.rules_service.get_rules(src).total == 1


class TestJsonMigration:
    def test_json_only_is_imported_written_and_then_deleted(
        self, container, fake_daemon, tmp_path,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        slug = _project(container, "legacy", repo)
        _write_legacy_json(repo, {
            "mandatory_rules": [
                _legacy_memory("r1", "mandatory_rules", "R1", "always do this"),
            ],
            "decision": [
                _legacy_memory("d1", "decision", "D1", "we chose duckdb"),
            ],
        })

        sync_cli._import(str(repo))

        # 1. the rows reached the store
        assert container.memory_repo.get_by_id(slug, "r1").title == "R1"
        assert container.memory_repo.get_by_id(slug, "d1").title == "D1"
        # 2. the database exists and holds them
        snapshot = read_snapshot(_snap(repo) / SNAPSHOT_DB_NAME)
        assert {m["id"] for items in snapshot["categories"].values() for m in items} == {
            "r1", "d1",
        }
        # 3. and only then were the category JSON files removed. manifest.json
        #    is identity, not data, and deliberately stays (see the test below).
        assert [p.name for p in _snap(repo).glob("*.json")] == [MANIFEST_NAME]

    def test_the_manifest_is_kept_and_rewritten(
        self, container, fake_daemon, tmp_path,
    ):
        """manifest.json is identity, not data - it survives the migration.

        `context._uid_from_manifest` reads it on every project detection in the
        daemon; making that path open DuckDB to answer "which project is this
        folder?" would be a regression, so the one small JSON file stays.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _project(container, "keepman", repo)
        _write_legacy_json(repo, {
            "decision": [_legacy_memory("d1", "decision", "D1", "x")],
        })

        sync_cli._import(str(repo))

        manifest = json.loads((_snap(repo) / MANIFEST_NAME).read_text())
        assert manifest["project_id"] == "uid-keepman"
        assert manifest["snapshot"] == SNAPSHOT_DB_NAME

    def test_json_is_kept_when_a_row_did_not_reach_the_database(
        self, container, fake_daemon, tmp_path, monkeypatch,
    ):
        """THE TEST THIS WHOLE TASK EXISTS FOR.

        A truncated write followed by an unlink destroys a project's history on
        a machine where the JSON was the only copy. The delete is gated on
        reading the rows back out and accounting for every id, so a snapshot
        that silently dropped one must leave the JSON exactly where it is.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _project(container, "truncated", repo)
        _write_legacy_json(repo, {
            "mandatory_rules": [
                _legacy_memory("r1", "mandatory_rules", "R1", "always do this"),
                _legacy_memory("r2", "mandatory_rules", "R2", "also always this"),
            ],
        })

        real_write = sync_cli.write_snapshot

        def truncating_write(path, **kwargs):
            """Write only the first memory - a migration that lost a row."""
            categories = {
                c: items[:1] for c, items in (kwargs.pop("categories") or {}).items()
            }
            return real_write(path, categories=categories, **kwargs)

        monkeypatch.setattr(sync_cli, "write_snapshot", truncating_write)

        sync_cli._import(str(repo))

        # The JSON is still there, untouched, with BOTH rules in it.
        surviving = json.loads((_snap(repo) / "mandatory_rules.json").read_text())
        assert {m["id"] for m in surviving} == {"r1", "r2"}
        # ...and the database really is the truncated one, so this is not a
        # test that passes because nothing went wrong.
        snapshot = read_snapshot(_snap(repo) / SNAPSHOT_DB_NAME)
        assert {m["id"] for items in snapshot["categories"].values() for m in items} == {
            "r1",
        }

    def test_unparseable_json_blocks_the_delete(
        self, container, fake_daemon, tmp_path,
    ):
        """A file left with git conflict markers is not a file we can account for."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _project(container, "conflicted", repo)
        snap = _write_legacy_json(repo, {
            "decision": [_legacy_memory("d1", "decision", "D1", "fine")],
        })
        (snap / "mandatory_rules.json").write_text(
            "<<<<<<< HEAD\n[]\n=======\n[]\n>>>>>>> theirs\n"
        )

        sync_cli._import(str(repo))

        assert (snap / "mandatory_rules.json").is_file()
        assert (snap / "decision.json").is_file()

    def test_both_present_prefers_the_database_and_folds_json_only_rows(
        self, container, fake_daemon, tmp_path,
    ):
        """Case 3: a DuckDB snapshot with stale JSON beside it.

        The database wins for anything both hold, but a memory that exists ONLY
        in the JSON is a memory this clone has never seen - dropping it to
        "prefer the database" is exactly the silent loss this work is about.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        slug = _project(container, "both", repo)
        snap = _snap(repo)
        snap.mkdir(parents=True, exist_ok=True)
        write_snapshot(
            snap / SNAPSHOT_DB_NAME, project_id="uid-both", slug=slug,
            categories={"decision": [
                _legacy_memory("d1", "decision", "From the database", "db copy"),
            ]},
        )
        (snap / MANIFEST_NAME).write_text(json.dumps({
            "version": 2, "project_id": "uid-both", "slug": slug,
        }))
        (snap / "decision.json").write_text(json.dumps([
            _legacy_memory("d1", "decision", "From the JSON", "json copy"),
            _legacy_memory("d2", "decision", "Only in the JSON", "json only"),
        ]))

        sync_cli._import(str(repo))

        assert container.memory_repo.get_by_id(slug, "d1").title == "From the database"
        assert container.memory_repo.get_by_id(slug, "d2").title == "Only in the JSON"
        assert not (snap / "decision.json").exists()

    def test_a_tombstoned_memory_is_not_resurrected_by_stale_json(
        self, container, fake_daemon, tmp_path,
    ):
        """The mirror image of the loss this task prevents.

        A memory somebody hard-deleted still sits in an old JSON file. Folding
        it back in would undo the delete, so a tombstoned id is accounted for
        without being re-added.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        slug = _project(container, "tomb", repo)
        snap = _snap(repo)
        snap.mkdir(parents=True, exist_ok=True)
        write_snapshot(
            snap / SNAPSHOT_DB_NAME, project_id="uid-tomb", slug=slug,
            categories={},
            tombstones=[{"memory_id": "d1", "category": "decision",
                         "title": "Deleted elsewhere",
                         "deleted_at": "2026-05-01T00:00:00", "actor": None}],
        )
        (snap / MANIFEST_NAME).write_text(json.dumps({
            "version": 2, "project_id": "uid-tomb", "slug": slug,
        }))
        (snap / "decision.json").write_text(json.dumps([
            _legacy_memory("d1", "decision", "Deleted elsewhere", "old copy"),
        ]))

        sync_cli._import(str(repo))

        assert container.memory_repo.get_by_id(slug, "d1") is None
        # It was accounted for by the tombstone, so the JSON could still go.
        assert not (snap / "decision.json").exists()


class TestSchemaVersionRefusal:
    def test_a_newer_snapshot_imports_nothing_at_all(
        self, container, fake_daemon, tmp_path, monkeypatch, capsys,
    ):
        """Refused whole, not applied in part. A half-imported rule set is worse
        than none: it looks like the project simply has fewer rules."""
        repo = tmp_path / "repo"
        repo.mkdir()
        slug = _project(container, "newer", repo)
        snap = _snap(repo)
        snap.mkdir(parents=True, exist_ok=True)
        write_snapshot(
            snap / SNAPSHOT_DB_NAME, project_id="uid-newer", slug=slug,
            categories={"mandatory_rules": [
                _legacy_memory("r1", "mandatory_rules", "R1", "always"),
            ]},
        )
        (snap / MANIFEST_NAME).write_text(json.dumps({
            "version": 2, "project_id": "uid-newer", "slug": slug,
        }))
        import duckdb

        conn = duckdb.connect()
        conn.execute(f"ATTACH '{snap / SNAPSHOT_DB_NAME}' AS s")
        conn.execute(
            "INSERT OR REPLACE INTO s.snapshot_meta VALUES ('schema_version', '99')"
        )
        conn.execute("CHECKPOINT s")
        conn.close()

        def _no_import(path, method="GET", payload=None):
            if path == "/api/hook/claim":
                return {"action": "existing", "slug": slug}
            raise AssertionError(f"nothing should have been imported: {path}")

        monkeypatch.setattr(sync_cli, "_daemon", _no_import)

        sync_cli._import(str(repo))

        assert container.memory_repo.get_by_id(slug, "r1") is None
        assert "newer memory-mcp" in capsys.readouterr().out
