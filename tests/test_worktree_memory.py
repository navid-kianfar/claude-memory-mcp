"""Worktree-safe memory (task 463f7785): the user's symptoms, reproduced.

Agents in separate git worktrees all write into ONE central store. The user saw
tasks reported as not done when they were done, and as done when they were
only done on an unmerged worktree; merges of the snapshot were a blind union.

These drive the real code paths: real git repositories and worktrees, the real
sync CLI with only its HTTP transport routed into a Container, the real merge
driver, and the project DuckDB underneath.
"""

import json
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from memory_mcp import merge_cli, merge_review, server, sync_cli
from memory_mcp.constants import MANIFEST_NAME, SNAPSHOT_DB_NAME, SNAPSHOT_DIRNAME
from memory_mcp.container import container as shared_container
from memory_mcp.db.connection import get_connection
from memory_mcp.db.schema import (
    CURRENT_SCHEMA_VERSION, create_schema, get_schema_version, run_migrations,
)
from memory_mcp.db.snapshot import read_snapshot, write_snapshot
from memory_mcp.models import CreateTaskRequest, MemoryCategory, StoreMemoryRequest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

MAIN = "main"
FEATURE = "feat-x"


# ---------- fixtures ----------


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch, tmp_path):
    """No user or system git config: the machine's merge driver must not run."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in (
        ("GIT_AUTHOR_NAME", "Test"), ("GIT_AUTHOR_EMAIL", "test@example.invalid"),
        ("GIT_COMMITTER_NAME", "Test"), ("GIT_COMMITTER_EMAIL", "test@example.invalid"),
    ):
        monkeypatch.setenv(key, value)


@pytest.fixture
def container():
    # The server's tools use the process-wide container; using the same one
    # lets a test call a tool and the service layer interchangeably.
    return shared_container


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path.resolve() / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", MAIN)
    (path / "README.md").write_text("hello\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def worktree(repo) -> Path:
    path = repo / ".claude" / "worktrees" / "w1"
    _git(repo, "worktree", "add", "-q", "-b", FEATURE, str(path))
    return Path(_git(path, "rev-parse", "--show-toplevel"))


@pytest.fixture
def project(container, repo):
    slug = f"wt-{repo.parent.name[-12:]}".lower().replace("_", "-")
    container.project_repo.register(
        slug, slug, project_path=str(repo), project_uid=f"uid-{slug}",
    )
    get_connection(slug).close()
    return slug


@pytest.fixture
def daemon(container, monkeypatch):
    """sync_cli's HTTP calls routed into the Container, the routes' logic real.

    Every call is recorded, so a test can assert what was NOT sent.
    """
    calls: list[tuple[str, str]] = []

    def _slug_for(cwd: str) -> str | None:
        for proj in container.project_repo.list_all():
            if proj.project_path and cwd.startswith(proj.project_path):
                return proj.slug
        return None

    def _daemon(path, method="GET", payload=None):
        calls.append((method, path))
        if path == "/api/hook/claim":
            return {"action": "existing", "slug": _slug_for(str(payload.get("cwd")))}
        slug = path.split("/")[3]
        if path.endswith("/sync-export"):
            return container.sync_service.build_full_snapshot(slug)
        if path.endswith("/sync-import"):
            return {"status": "ok", **container.sync_service.apply_snapshot(
                slug, payload["categories"], payload["reconcile"],
                provenance=payload.get("provenance"),
                tombstones=payload.get("tombstones"),
            )}
        if path.endswith("/asoode/link-proposals"):
            return {"proposals": []}
        if path.endswith("/worktrees"):
            return {"status": "ok", **container.sync_service.register_worktree(
                slug, payload["path"], payload["branch"],
            )}
        if path.endswith("/branch-marks/merged"):
            return {"status": "ok", **container.sync_service.clear_merged_branches(
                slug, payload["branches"],
            )}
        if path.endswith("/branch-marks"):
            return container.sync_service.branch_marks(slug)
        raise AssertionError(f"unexpected daemon call: {method} {path}")

    monkeypatch.setattr(sync_cli, "_daemon", _daemon)
    return calls


def _store(container, slug, title, content, category="decision"):
    return container.memory_service.store(StoreMemoryRequest(
        project=slug, category=MemoryCategory(category), title=title, content=content,
    ))


def _row(mid, title, content, updated_at, category="decision"):
    return {
        "id": mid, "category": category, "title": title, "content": content,
        "status": "active", "priority": 0, "approval_status": "approved",
        "created_at": "2026-01-01T00:00:00", "updated_at": updated_at,
    }


def _session(container, slug, sid, cwd):
    container.session_repo.insert(slug, sid, metadata={"agent": "python", "cwd": str(cwd)})
    return sid


def _task(container, slug, title):
    return container.task_service.create(CreateTaskRequest(project=slug, title=title))


def _write_repo_snapshot(folder: Path, slug: str, categories, tombstones=None):
    snap = folder / SNAPSHOT_DIRNAME
    snap.mkdir(parents=True, exist_ok=True)
    write_snapshot(
        snap / SNAPSHOT_DB_NAME, project_id=f"uid-{slug}", slug=slug,
        categories=categories, tombstones=tombstones or [],
    )
    (snap / MANIFEST_NAME).write_text(json.dumps({
        "version": 3, "project_id": f"uid-{slug}", "slug": slug,
    }))
    return snap


# ---------- the symptoms: stale worktree imports ----------


class TestAStaleWorktreeSnapshotNeverReachesTheStore:
    def test_a_task_done_on_main_is_still_done_after_a_worktree_import(
        self, container, project, repo, worktree, daemon,
    ):
        task = _task(container, project, "Ship the login page")
        main_session = _session(container, project, "s-main", repo)
        container.task_service.done(project, task.id, None, main_session)
        container.sync_service.stamp_task_branch(project, task.id, main_session)
        decided = _store(container, project, "Auth", "we use sessions")

        # The worktree's committed snapshot is as old as its branch - and here
        # it even claims a NEWER edit, the shape the old import would apply.
        future = (datetime.now() + timedelta(days=1)).isoformat()
        _write_repo_snapshot(worktree, project, {"decision": [
            _row(decided.id, "Auth", "we use JWT", future),
            _row("ghost", "Ghost", "never in the store", future),
        ]})

        sync_cli._import(str(worktree))

        after = container.task_repo.get(project, task.id)
        assert after.state.value == "done"
        assert after.branch is None, "done on main is plainly done"
        assert container.memory_repo.get_by_id(project, decided.id).content == "we use sessions"
        assert container.memory_repo.get_by_id(project, "ghost") is None
        assert not any(path.endswith("/sync-import") for _, path in daemon)

    def test_a_worktree_import_from_a_subfolder_registers_its_branch(
        self, container, project, worktree, daemon,
    ):
        # A subfolder has no `.git` file of its own; git still knows.
        (worktree / "src").mkdir()

        sync_cli._import(str(worktree / "src"))

        assert container.task_repo.worktree_branch(project, str(worktree)) == FEATURE

    def test_a_worktree_export_writes_no_snapshot_anywhere(
        self, container, project, repo, worktree, daemon,
    ):
        _store(container, project, "D1", "content")

        sync_cli._export(str(worktree))

        assert not (repo / SNAPSHOT_DIRNAME).exists(), "main's working tree untouched"
        assert not (worktree / SNAPSHOT_DIRNAME).exists()
        assert container.task_repo.worktree_branch(project, str(worktree)) == FEATURE

    def test_a_stale_main_snapshot_does_not_resurrect_a_hard_delete(
        self, container, project,
    ):
        memory = _store(container, project, "Old rule", "remove me")
        stale = container.sync_service.build_snapshot(project)
        container.memory_service.delete(project, memory.id, hard=True)

        result = container.sync_service.apply_snapshot(project, stale, list(stale))

        assert container.memory_repo.get_by_id(project, memory.id) is None
        assert result["kept_deleted"] == 1
        assert result["added"] == 0

    def test_a_re_add_newer_than_the_delete_still_wins(self, container, project):
        memory = _store(container, project, "Rule", "first")
        container.memory_service.delete(project, memory.id, hard=True)
        newer = (datetime.now() + timedelta(minutes=5)).isoformat()

        container.sync_service.apply_snapshot(
            project, {"decision": [_row(memory.id, "Rule", "re-added", newer)]},
            ["decision"],
        )

        assert container.memory_repo.get_by_id(project, memory.id).content == "re-added"


# ---------- the symptom: done only on an unmerged branch ----------


class TestDoneOnlyOnAnUnmergedBranch:
    def test_shows_branch_marked_not_plainly_done(
        self, container, project, worktree, daemon,
    ):
        sync_cli._import(str(worktree))
        task = _task(container, project, "Refactor the parser")
        session = _session(container, project, "s-wt", worktree)

        answer = server.memory_task_done(task.id, project=project, session_id=session)

        assert answer["task"]["state"] == "done"
        assert answer["task"]["branch"] == FEATURE
        assert container.task_repo.get(project, task.id).branch == FEATURE

    def test_session_start_answer_names_the_marked_tasks(
        self, container, project, worktree, daemon,
    ):
        sync_cli._import(str(worktree))
        task = _task(container, project, "Refactor the parser")
        server.memory_task_done(
            task.id, project=project, session_id=_session(container, project, "s-wt", worktree),
        )

        answer = server.memory_session_start(project=project, agent="python")

        (group,) = answer["unmerged_branch_tasks"]["branches"]
        assert group["branch"] == FEATURE
        assert group["tasks"] == [{"id": task.id, "title": "Refactor the parser", "state": "done"}]

    def test_session_start_records_the_folder(self, container, project, worktree):
        ctx = container.session_service.start(project, agent="python", cwd=str(worktree))

        assert container.session_repo.metadata(project, ctx.session_id)["cwd"] == str(worktree)

    def test_main_session_start_names_it_while_unmerged(
        self, container, project, repo, worktree, daemon, capsys,
    ):
        sync_cli._import(str(worktree))
        task = _task(container, project, "Refactor the parser")
        session = _session(container, project, "s-wt", worktree)
        server.memory_task_done(task.id, project=project, session_id=session)
        capsys.readouterr()

        sync_cli._import(str(repo))

        out = capsys.readouterr().out
        assert FEATURE in out and "Refactor the parser" in out and "not done" in out
        assert container.task_repo.get(project, task.id).branch == FEATURE

    def test_the_mark_clears_once_the_branch_is_merged(
        self, container, project, repo, worktree, daemon, capsys,
    ):
        sync_cli._import(str(worktree))
        task = _task(container, project, "Refactor the parser")
        session = _session(container, project, "s-wt", worktree)
        server.memory_task_done(task.id, project=project, session_id=session)
        (worktree / "parser.py").write_text("pass\n")
        _git(worktree, "add", "parser.py")
        _git(worktree, "commit", "-q", "-m", "parser")
        _git(repo, "merge", "-q", "--no-edit", FEATURE)
        capsys.readouterr()

        sync_cli._import(str(repo))

        assert container.task_repo.get(project, task.id).branch is None
        assert FEATURE not in capsys.readouterr().out

    def test_a_state_set_on_main_clears_the_mark(
        self, container, project, repo, worktree, daemon,
    ):
        sync_cli._import(str(worktree))
        task = _task(container, project, "Refactor the parser")
        server.memory_task_done(
            task.id, project=project, session_id=_session(container, project, "s-wt", worktree),
        )

        server.memory_task_start(
            task.id, project=project, session_id=_session(container, project, "s-main", repo),
        )

        reopened = container.task_repo.get(project, task.id)
        assert reopened.state.value == "in_progress" and reopened.branch is None

    def test_a_session_with_no_folder_leaves_the_mark_alone(self, container, project):
        task = _task(container, project, "Marked")
        container.task_repo.set_branch(project, task.id, FEATURE)
        container.session_repo.insert(project, "s-ui")

        assert container.sync_service.stamp_task_branch(project, task.id, "s-ui") == FEATURE

    def test_a_deleted_unmerged_branch_keeps_its_mark_and_says_so(
        self, container, project, repo, worktree, daemon, capsys,
    ):
        sync_cli._import(str(worktree))
        task = _task(container, project, "Abandoned work")
        server.memory_task_done(
            task.id, project=project, session_id=_session(container, project, "s-wt", worktree),
        )
        _git(repo, "worktree", "remove", "--force", str(worktree))
        _git(repo, "branch", "-D", FEATURE)
        capsys.readouterr()

        sync_cli._import(str(repo))

        assert container.task_repo.get(project, task.id).branch == FEATURE
        assert "no longer exists" in capsys.readouterr().out

    def test_hostile_input_is_refused_at_the_edge(self, container, project):
        with pytest.raises(ValueError):
            container.sync_service.register_worktree(project, "/x", "feat\nx")
        with pytest.raises(ValueError):
            container.sync_service.clear_merged_branches(project, "feat-x")


# ---------- concurrency ----------


class TestConcurrentWriters:
    def test_two_worktrees_importing_at_once_never_collide(self, container, project):
        now = datetime.now().isoformat()
        shared = [_row(f"m{i}", f"T{i}", f"c{i}", now) for i in range(8)]
        errors: list[BaseException] = []
        results: list[dict] = []

        def _import():
            try:
                results.append(container.sync_service.apply_snapshot(
                    project, {"decision": list(shared)}, ["decision"],
                ))
            except BaseException as e:  # noqa: BLE001 - collected and asserted
                errors.append(e)

        threads = [threading.Thread(target=_import) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert sum(r["added"] for r in results) == len(shared), "each id inserted once"
        stored = container.memory_repo.all_for_categories(project, ["decision"])
        assert sorted(m.id for m in stored) == sorted(r["id"] for r in shared)

    def test_concurrent_snapshot_writes_leave_a_readable_file(self, tmp_path):
        target = tmp_path / "snap" / SNAPSHOT_DB_NAME
        errors: list[BaseException] = []

        def _write(n):
            try:
                write_snapshot(
                    target, project_id="uid", slug="s",
                    categories={"decision": [_row(f"m{n}", "T", "c", "2026-01-01T00:00:00")]},
                )
            except BaseException as e:  # noqa: BLE001 - collected and asserted
                errors.append(e)

        threads = [threading.Thread(target=_write, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert len(read_snapshot(target)["categories"]["decision"]) == 1
        assert not list(target.parent.glob(".*.tmp")), "no half-built file left behind"


# ---------- the merge review path ----------


def _merge(tmp_path, slug, ours_rows, theirs_rows, theirs_tombstones=None):
    """Run the git merge driver on two snapshot files, as git would."""
    base = tmp_path / "base"
    base.write_bytes(b"")
    ours = tmp_path / "ours.duckdb"
    theirs = tmp_path / "theirs.duckdb"
    write_snapshot(ours, project_id=f"uid-{slug}", slug=slug,
                   categories={"decision": ours_rows})
    write_snapshot(theirs, project_id=f"uid-{slug}", slug=slug,
                   categories={"decision": theirs_rows},
                   tombstones=theirs_tombstones or [])
    code = merge_cli.main([str(base), str(ours), str(theirs), "memory.duckdb"])
    return code, ours


class TestMergesAreReviewed:
    def _setup_merge(self, container, project, repo, tmp_path, capsys):
        """Ours: what main already has. Theirs: an addition, an edit, a delete."""
        kept = _store(container, project, "Kept", "ours")
        doomed = _store(container, project, "Doomed", "will be deleted by theirs")
        ours_rows = container.sync_service.build_snapshot(project)["decision"]
        later = (datetime.now() + timedelta(minutes=5)).isoformat()
        theirs_rows = [
            _row(kept.id, "Kept", "theirs edited it", later),
            _row("new-one", "From theirs", "added on the branch", later),
        ]
        tomb = [{"memory_id": doomed.id, "deleted_at": later}]
        code, merged = _merge(tmp_path, project, ours_rows, theirs_rows, tomb)
        snap = repo / SNAPSHOT_DIRNAME
        snap.mkdir(exist_ok=True)
        shutil.copy(merged, snap / SNAPSHOT_DB_NAME)
        (snap / MANIFEST_NAME).write_text(json.dumps({
            "version": 3, "project_id": f"uid-{project}", "slug": project,
        }))
        out = capsys.readouterr().out
        return code, out, kept, doomed

    def test_the_driver_saves_a_review_and_says_how_to_settle_it(
        self, container, project, repo, tmp_path, capsys,
    ):
        code, out, kept, doomed = self._setup_merge(container, project, repo, tmp_path, capsys)

        assert code == 0
        (record,) = merge_review.pending(f"uid-{project}", project)
        assert merge_review.counts(record) == {
            "added": 1, "changed": 1, "removed": 1, "kept_ours": 0,
        }
        assert "waiting for your review" in out and record["id"] in out
        assert "sync review apply" in out

    def test_nothing_reaches_the_store_until_reviewed(
        self, container, project, repo, tmp_path, daemon, capsys,
    ):
        _, _, kept, doomed = self._setup_merge(container, project, repo, tmp_path, capsys)

        sync_cli._import(str(repo))
        sync_cli._import(str(repo))

        out = capsys.readouterr().out
        assert container.memory_repo.get_by_id(project, "new-one") is None
        assert container.memory_repo.get_by_id(project, kept.id).content == "ours"
        assert container.memory_repo.get_by_id(project, doomed.id) is not None
        assert "held back" in out and "waiting for your review" in out

    def test_apply_brings_it_in_minus_the_skipped_rows(
        self, container, project, repo, tmp_path, daemon, capsys,
    ):
        _, _, kept, doomed = self._setup_merge(container, project, repo, tmp_path, capsys)
        (record,) = merge_review.pending(f"uid-{project}", project)

        sync_cli.main(["review", "apply", record["id"], "--skip", doomed.id,
                       "--cwd", str(repo)])
        sync_cli._import(str(repo))

        assert container.memory_repo.get_by_id(project, "new-one").content == "added on the branch"
        assert container.memory_repo.get_by_id(project, kept.id).content == "theirs edited it"
        assert container.memory_repo.get_by_id(project, doomed.id) is not None, "skipped"
        assert merge_review.load(record["id"])["status"] == merge_review.STATUS_APPLIED
        assert merge_review.pending(f"uid-{project}", project) == ()

    def test_reject_keeps_main_as_it_was_across_imports(
        self, container, project, repo, tmp_path, daemon, capsys,
    ):
        _, _, kept, doomed = self._setup_merge(container, project, repo, tmp_path, capsys)
        (record,) = merge_review.pending(f"uid-{project}", project)

        sync_cli.main(["review", "reject", record["id"], "--cwd", str(repo)])
        sync_cli._import(str(repo))

        assert container.memory_repo.get_by_id(project, "new-one") is None
        assert container.memory_repo.get_by_id(project, kept.id).content == "ours"
        assert container.memory_repo.get_by_id(project, doomed.id) is not None

    def test_show_renders_the_diff(self, container, project, repo, tmp_path, daemon, capsys):
        self._setup_merge(container, project, repo, tmp_path, capsys)
        (record,) = merge_review.pending(f"uid-{project}", project)

        sync_cli.main(["review", "show", record["id"], "--cwd", str(repo)])

        out = capsys.readouterr().out
        assert "From theirs" in out and "theirs edited it" in out and "hard delete" in out

    def test_a_review_of_another_project_is_refused(
        self, container, project, repo, tmp_path, daemon, capsys,
    ):
        _, _, _, _ = self._setup_merge(container, project, repo, tmp_path, capsys)
        (record,) = merge_review.pending(f"uid-{project}", project)
        record["project_id"] = "uid-someone-else"
        merge_review.save(record)

        with pytest.raises(SystemExit):
            sync_cli.main(["review", "apply", record["id"], "--cwd", str(repo)])
        assert container.memory_repo.get_by_id(project, "new-one") is None

    def test_a_merge_that_changes_nothing_here_saves_no_review(self, project, tmp_path):
        row = _row("same", "Same", "same", "2026-01-01T00:00:00")
        code, _ = _merge(tmp_path, project, [row], [row])

        assert code == 0
        assert merge_review.pending(f"uid-{project}", project) == ()

    def test_a_review_that_cannot_be_saved_fails_the_merge(
        self, project, tmp_path, monkeypatch,
    ):
        def _broken(record):
            raise OSError("disk full")

        monkeypatch.setattr(merge_review, "save", _broken)
        ours_rows = [_row("a", "A", "ours", "2026-01-01T00:00:00")]
        theirs_rows = [_row("b", "B", "theirs", "2026-01-02T00:00:00")]

        code, ours = _merge(tmp_path, project, ours_rows, theirs_rows)

        assert code == 1, "git then records a conflict for a person"
        left = read_snapshot(ours)["categories"]["decision"]
        assert [m["id"] for m in left] == ["a"], "%A is left exactly as it was"


# ---------- schema v21 ----------


def _make_v20_db(path):
    conn = duckdb.connect(str(path))
    create_schema(conn)
    conn.execute("ALTER TABLE tasks DROP COLUMN branch")
    conn.execute("DROP TABLE worktrees")
    conn.execute("DELETE FROM schema_version WHERE version >= 21")
    conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (20)")
    conn.execute(
        "INSERT INTO tasks (id, title, state, source) VALUES ('t1', 'existing', 'done', 'user')"
    )
    conn.close()


def test_v20_db_gains_the_branch_column_and_the_registry(tmp_path):
    db = tmp_path / "v20.duckdb"
    _make_v20_db(db)
    conn = duckdb.connect(str(db))
    try:
        assert get_schema_version(conn) == 20
        assert run_migrations(conn) == CURRENT_SCHEMA_VERSION == 21
        assert conn.execute("SELECT id, state, branch FROM tasks").fetchall() == [
            ("t1", "done", None),
        ], "the existing task keeps its state and is unmarked"
        conn.execute("INSERT INTO worktrees (path, branch) VALUES ('/w', 'b')")
        assert run_migrations(conn) == 21, "idempotent"
    finally:
        conn.close()


def test_v21_shape_matches_between_fresh_and_migrated(tmp_path):
    fresh = duckdb.connect(str(tmp_path / "fresh.duckdb"))
    migrated_path = tmp_path / "migrated.duckdb"
    _make_v20_db(migrated_path)
    migrated = duckdb.connect(str(migrated_path))
    try:
        create_schema(fresh)
        run_migrations(migrated)
        for table in ("tasks", "worktrees"):
            a = {(r[1], r[2]) for r in fresh.execute(f"PRAGMA table_info('{table}')").fetchall()}
            b = {(r[1], r[2]) for r in migrated.execute(f"PRAGMA table_info('{table}')").fetchall()}
            assert a == b, table
    finally:
        fresh.close()
        migrated.close()


def test_an_old_database_file_upgrades_on_open(container):
    """The upgrade path through the real connection layer, not only the DDL."""
    from memory_mcp.db import connection as conn_mod

    slug = "v20-on-disk"
    container.project_repo.register(slug, slug)
    path = conn_mod.project_database_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    _make_v20_db(path)

    task = container.task_repo.get(slug, "t1")

    assert task is not None and task.state.value == "done" and task.branch is None


# ---------- the session-start hook carries the notes to the model ----------


SESSION_START = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "session-start.sh"


@pytest.mark.skipif(not SESSION_START.exists(), reason="hook script not present")
def test_session_start_puts_the_sync_notes_in_the_context(tmp_path):
    """A pending review or a branch mark printed by `sync import` reaches the
    model, inside the one JSON envelope every harness accepts."""
    import os

    from tests.test_hook_attachments import _daemon as stub_daemon

    # Not tmp_path / "memory-mcp": that is the test's data dir (conftest).
    (tmp_path / "stub-bin").mkdir()
    binary = tmp_path / "stub-bin" / "memory-mcp"
    binary.write_text(
        "#!/bin/bash\n"
        "echo '[Memory MCP] A memory merge is waiting for your review, id X.'\n"
    )
    binary.chmod(0o755)
    hook_input = {"session_id": "s1", "cwd": str(tmp_path), "hook_event_name": "SessionStart"}
    with stub_daemon({"/api/hook/rules": "RULES BLOCK"}) as (port, _seen):
        env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port),
               "MEMORY_MCP_BIN": str(binary)}
        for key in ("MEMORY_MCP_URL", "MEMORY_MCP_TOKEN", "MEMORY_MCP_HARNESS"):
            env.pop(key, None)
        result = subprocess.run(
            ["bash", str(SESSION_START)], input=json.dumps(hook_input), text=True,
            capture_output=True, timeout=30, env=env,
        )

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert set(out) == {"hookSpecificOutput"}
    assert out["hookSpecificOutput"]["additionalContext"] == (
        "[Memory MCP] A memory merge is waiting for your review, id X.\n\nRULES BLOCK"
    )
