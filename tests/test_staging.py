"""Staging memories: a home for working state, so sessions stop writing .md files.

User decisions (2026-11-20, task 5e632888), each pinned below:

1. When a task is done, its durable staging notes are folded into it as one
   comment, then the staging memories are removed.
2. Staging without a task is allowed and expires after 7 days.
3. A new progress/plan/notes .md file, or any CLAUDE.md edit, gets a
   context-only nudge from the PreToolUse gate - never a deny, never an ask.
4. Staging is left out of search, listing and recall by default.
"""

import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memory_mcp import server
from memory_mcp.constants import SYNC_CATEGORIES
from memory_mcp.container import container
from memory_mcp.db.connection import connect, get_connection
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.enforcement import (
    _STAGING_LINE, format_rules_block, markdown_nudge, markdown_nudge_reason,
)
from memory_mcp.exceptions import TaskNotFoundError
from memory_mcp.harnesses import ADAPTERS
from memory_mcp.harnesses import codex as codex_mod
from memory_mcp.harnesses.claude import HOOK_MATCHERS
from memory_mcp.harnesses.deepseek import DSH_HOOK_MATCHERS
from memory_mcp.models import (
    CreateTaskRequest, MemoryCategory, MemoryFilter, Pagination, SearchRequest,
    StoreMemoryRequest, TaskState, UpdateTaskRequest,
)
from memory_mcp.web import routes

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "require-task.sh"

# Imported from the gate tests so both suites drive the script the same way.
from tests.test_require_task_gate import _Req, _stub_daemon  # noqa: E402


@pytest.fixture
def project():
    slug = "t-staging"
    container.project_repo.register(slug, slug)
    get_connection(slug).close()
    return slug


def _task(project, title="Build the staging tool"):
    return container.task_service.create(
        CreateTaskRequest(project=project, title=title, description="d"),
    )


def _stage(project, title, content, **kw):
    return container.staging_service.stage(project, title, content, **kw)


def _comments(project, task_id):
    return container.task_service.detail(project, task_id).comments


def _staging_rows(project):
    with connect(project) as conn:
        return conn.execute(
            "SELECT id FROM memories WHERE category = 'staging'"
        ).fetchall()


# ---------- 1. create -> task done -> fold and cleanup ----------


class TestFoldOnDone:
    def test_done_folds_durable_notes_into_one_comment_and_removes_them(self, project):
        task = _task(project)
        _stage(project, "Plan", "1. repo 2. service 3. hook", task_id=task.id)
        _stage(project, "Finding", "DuckDB DELETE supports RETURNING", task_id=task.id)
        _stage(project, "Scratch", "running the tests now", task_id=task.id, durable=False)

        container.task_service.done(project, task.id, note="Shipped")

        comments = _comments(project, task.id)
        folded = [c for c in comments if "Working notes folded" in c.body]
        assert len(folded) == 1
        assert "1. repo 2. service 3. hook" in folded[0].body
        assert "DuckDB DELETE supports RETURNING" in folded[0].body
        assert "running the tests now" not in folded[0].body
        assert _staging_rows(project) == []
        assert container.task_service.get(project, task.id).state == TaskState.DONE

    def test_closing_through_update_folds_too(self, project):
        task = _task(project)
        _stage(project, "Progress", "half way", task_id=task.id)

        container.task_service.update(UpdateTaskRequest(
            project=project, task_id=task.id, state=TaskState.DONE,
        ))

        assert any("half way" in c.body for c in _comments(project, task.id))
        assert _staging_rows(project) == []

    def test_only_the_closing_tasks_notes_are_touched(self, project):
        closing, other = _task(project, "One"), _task(project, "Two")
        _stage(project, "Mine", "closing note", task_id=closing.id)
        kept = _stage(project, "Theirs", "other note", task_id=other.id)

        container.task_service.done(project, closing.id)

        assert [n.id for n in container.staging_service.notes(project, other.id)] == [kept.id]

    def test_a_task_with_no_notes_gets_no_extra_comment(self, project):
        task = _task(project)
        container.task_service.done(project, task.id)
        assert _comments(project, task.id) == []

    def test_only_scratch_notes_are_removed_without_a_comment(self, project):
        task = _task(project)
        _stage(project, "Scratch", "tmp", task_id=task.id, durable=False)

        container.task_service.done(project, task.id)

        assert _comments(project, task.id) == []
        assert _staging_rows(project) == []

    def test_a_failed_fold_leaves_the_task_open_and_the_notes_in_place(
        self, project, monkeypatch,
    ):
        task = _task(project)
        _stage(project, "Plan", "keep me", task_id=task.id)

        def _boom(*_a, **_k):
            raise RuntimeError("board down")

        monkeypatch.setattr(container.task_service, "comment", _boom)
        with pytest.raises(RuntimeError):
            container.task_service.done(project, task.id)

        assert container.task_service.get(project, task.id).state != TaskState.DONE
        assert len(_staging_rows(project)) == 1


class TestStagingNeedsAnOpenTask:
    def test_an_unknown_task_is_refused(self, project):
        with pytest.raises(TaskNotFoundError):
            _stage(project, "Plan", "x", task_id="no-such-task")

    def test_a_done_task_is_refused(self, project):
        task = _task(project)
        container.task_service.done(project, task.id)
        with pytest.raises(ValueError, match="done"):
            _stage(project, "Plan", "x", task_id=task.id)

    def test_a_note_tied_to_a_task_does_not_expire(self, project):
        task = _task(project)
        note = _stage(project, "Plan", "x", task_id=task.id)
        assert note.expires_at is None
        assert note.category == MemoryCategory.STAGING
        assert note.metadata["task_id"] == task.id


# ---------- 2. TTL ----------


def _expire(project, memory_id):
    with connect(project) as conn:
        conn.execute(
            "UPDATE memories SET expires_at = ? WHERE id = ?",
            [datetime.now(timezone.utc) - timedelta(minutes=1), memory_id],
        )


class TestTaskless:
    def test_a_taskless_note_expires_after_seven_days(self, project):
        note = _stage(project, "Exploring", "what does the sweeper do")
        # DuckDB hands TIMESTAMP back naive, in the session's local time - the
        # same convention every other expiry in the store follows.
        now = datetime.now() if note.expires_at.tzinfo is None else datetime.now(timezone.utc)
        delta = note.expires_at - now
        assert timedelta(days=6, hours=23) < delta <= timedelta(days=7)

    def test_an_expired_note_is_gone_from_every_read_and_purged(self, project):
        old = _stage(project, "Old", "stale exploration")
        _expire(project, old.id)

        assert container.staging_service.notes(project) == ()
        memories, total = container.memory_repo.list(
            project, MemoryFilter(include_staging=True), Pagination(),
        )
        assert total == 0
        _stage(project, "New", "fresh")  # staging purges expired rows as it writes
        assert len(_staging_rows(project)) == 1

    def test_giving_a_taskless_note_a_task_removes_its_expiry(self, project):
        task = _task(project)
        note = _stage(project, "Exploring", "x")
        updated = server.memory_update(
            memory_id=note.id, project=project, metadata={"task_id": task.id},
        )
        assert updated["memory"]["expires_at"] is None


# ---------- 4. exclusion from recall ----------


class TestKeptOutOfRecall:
    @pytest.fixture
    def both(self, project):
        decision = container.memory_service.store(StoreMemoryRequest(
            project=project, category=MemoryCategory.DECISION,
            title="Outbox sweep interval", content="The outbox sweep runs every minute.",
        ))
        note = _stage(project, "Outbox sweep interval",
                      "The outbox sweep runs every minute, checking now.")
        return decision, note

    def _search(self, project, **kw):
        response = container.search_service.search(
            SearchRequest(project=project, query="outbox sweep", min_similarity=0.0, **kw),
        )
        return {hit.memory.id for hit in response.results}

    def test_search_leaves_staging_out_by_default(self, project, both):
        decision, note = both
        found = self._search(project)
        assert decision.id in found and note.id not in found

    def test_search_includes_staging_when_asked(self, project, both):
        _, note = both
        assert note.id in self._search(project, include_staging=True)
        assert note.id in self._search(project, category=MemoryCategory.STAGING)

    def test_listing_leaves_staging_out_unless_asked(self, project, both):
        decision, note = both
        listed = server.memory_list(project=project)
        assert [m["id"] for m in listed["memories"]] == [decision.id]
        listed = server.memory_list(project=project, include_staging=True)
        assert {m["id"] for m in listed["memories"]} == {decision.id, note.id}
        listed = server.memory_list(project=project, category="staging")
        assert [m["id"] for m in listed["memories"]] == [note.id]

    def test_recall_by_title_answers_with_the_real_memory(self, project, both):
        decision, note = both
        assert server.memory_recall(
            project=project, title="Outbox sweep interval",
        )["memory"]["id"] == decision.id
        assert server.memory_recall(project=project, memory_id=note.id)["memory"]["id"] == note.id

    def test_staging_never_reaches_the_snapshot(self):
        assert "staging" not in SYNC_CATEGORIES


# ---------- the MCP surface ----------


class TestTools:
    def test_memory_stage_and_task_get_show_the_notes(self, project):
        task = _task(project)
        staged = server.memory_stage(
            title="Plan", content="steps", project=project, task_id=task.id,
        )
        assert staged["status"] == "ok"

        detail = server.memory_task_get(task_id=task.id, project=project)
        assert [n["title"] for n in detail["staging"]] == ["Plan"]
        listed = server.memory_staging_list(project=project, task_id=task.id)
        assert listed["total"] == 1

    def test_memory_store_with_the_staging_category_is_staging(self, project):
        task = _task(project)
        answer = server.memory_store(
            category="staging", title="Plan", content="steps", project=project,
            metadata={"task_id": task.id},
        )
        assert answer["memory"]["expires_at"] is None
        assert [n.id for n in container.staging_service.notes(project, task.id)] == [
            answer["memory"]["id"]
        ]

    def test_an_unknown_task_is_a_clear_error(self, project):
        answer = server.memory_stage(
            title="Plan", content="x", project=project, task_id="missing",
        )
        assert answer["type"] == "TaskNotFoundError"

    def test_guidance_reaches_the_lead_and_every_turn(self):
        assert "memory_stage" in server.SERVER_INSTRUCTIONS
        assert _STAGING_LINE in format_rules_block("p-no-rules", [], [])

    def test_guidance_reaches_every_agent(self):
        base = Path(__file__).resolve().parents[1] / "agents" / "_base.md"
        assert "memory_stage" in base.read_text()


# ---------- 3. the nudge: decision ----------


class TestNudgeDecision:
    @pytest.mark.parametrize("path,is_new", [
        ("/repo/CLAUDE.md", False),
        ("/repo/sub/CLAUDE.md", True),
        ("/repo/CLAUDE.local.md", False),
        ("/repo/PROGRESS.md", True),
        ("/repo/implementation-plan.md", True),
        ("/repo/notes/sessionNotes.md", True),
        ("/repo/TODO.md", True),
    ])
    def test_these_are_nudged(self, path, is_new):
        assert markdown_nudge_reason(path, is_new)

    @pytest.mark.parametrize("path,is_new", [
        ("/repo/PROGRESS.md", False),          # an existing file is not new
        ("/repo/docs/plan.md", True),           # docs/ is documentation
        ("/repo/README.md", True),
        ("/repo/CHANGELOG.md", True),
        ("/repo/agents/planner.md", True),      # a word, not a substring
        ("/repo/src/progress.py", True),        # not markdown
        ("/repo/architecture.md", True),        # not working state by name
        ("", True),
    ])
    def test_these_are_left_alone(self, path, is_new):
        assert markdown_nudge_reason(path, is_new) is None

    def test_the_nudge_names_the_tool_and_the_file(self):
        text = markdown_nudge(("/repo/PLAN.md",), {"/repo/PLAN.md"})
        assert "memory_stage" in text and "PLAN.md" in text


# ---------- 3. the nudge: the gate route ----------


def _gate(cwd, **extra):
    response = asyncio.run(routes._hook_gate(_Req(cwd, **extra)))
    return json.loads(response.body)


class TestNudgeInTheGate:
    @pytest.fixture
    def rooted(self, project, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        container.project_service.link_folder(project, str(root))
        monkeypatch.setattr("memory_mcp.context.detect_project_from_cwd", lambda cwd: project)
        return project, root

    def test_an_allowed_write_carries_the_nudge_and_stays_allowed(self, rooted):
        _, root = rooted
        path = str(root / "PROGRESS.md")
        answer = _gate(str(root), tool_name="Write", file_path=path, new_file=path)
        assert answer["allow"] is True
        assert "memory_stage" in answer["nudge"]

    def test_a_deny_keeps_its_verdict_and_carries_the_nudge_in_its_reason(self, rooted):
        project, root = rooted
        upsert_project_link(project, base_url="https://api.asoode.com",
                            remote_project_id="p1", remote_work_package_id="wp1")
        path = str(root / "CLAUDE.md")
        answer = _gate(str(root), tool_name="Edit", file_path=path)
        assert answer["allow"] is False
        assert "No task is in progress" in answer["reason"]
        assert "memory_stage" in answer["reason"]

    def test_an_ordinary_edit_has_no_nudge(self, rooted):
        _, root = rooted
        answer = _gate(str(root), tool_name="Edit", file_path=str(root / "app.py"))
        assert "nudge" not in answer

    def test_a_write_outside_the_project_root_has_no_nudge(self, rooted, tmp_path):
        path = str(tmp_path / "scratch" / "PLAN.md")
        answer = _gate(str(rooted[1]), tool_name="Write", file_path=path, new_file=path)
        assert "nudge" not in answer


# ---------- 3. the nudge, per harness ----------

#: Each harness's write tool, as its matcher names it.
_GATED = {
    "claude": ("Write", HOOK_MATCHERS["require-task.sh"]),
    "zcode": ("Write", HOOK_MATCHERS["require-task.sh"]),  # ZCode reuses Claude's
    "deepseek": ("write", DSH_HOOK_MATCHERS["require-task.sh"]),
}


def _run_hook(payload, port, harness):
    env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port), "MEMORY_MCP_HARNESS": harness}
    env.pop("MEMORY_MCP_URL", None)
    env.pop("MEMORY_MCP_NO_GATE", None)
    return subprocess.run(["bash", str(HOOK)], input=json.dumps(payload), text=True,
                          capture_output=True, timeout=30, env=env)


@pytest.mark.skipif(not HOOK.exists(), reason="hook script not present")
class TestNudgePerHarness:
    @pytest.mark.parametrize("harness", sorted(_GATED))
    def test_the_gate_hook_runs_on_this_harness_write_tool(self, harness):
        tool, matcher = _GATED[harness]
        assert tool in matcher.split("|")
        adapter = next(a for a in ADAPTERS if a.name == harness)
        assert adapter.capabilities().markdown_nudge is True

    @pytest.mark.parametrize("harness", sorted(_GATED))
    def test_the_nudge_is_printed_as_context_and_never_blocks(self, harness, tmp_path):
        tool, _ = _GATED[harness]
        target = tmp_path / "PLAN.md"
        answer = json.dumps({"allow": True, "reason": "ok", "nudge": "Use memory_stage."})
        with _stub_daemon(answer) as (port, seen):
            result = _run_hook(
                {"cwd": str(tmp_path), "tool_name": tool,
                 "tool_input": {"file_path": str(target)}},
                port, harness,
            )

        assert result.returncode == 0, result.stderr
        # Exactly the envelope ZCode's strict schema accepts, nothing beside it.
        assert json.loads(result.stdout) == {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "additionalContext": "Use memory_stage.",
        }}
        assert seen[0]["query"]["new_file"] == [str(target)]

    def test_an_existing_file_is_not_reported_as_new(self, tmp_path):
        target = tmp_path / "PLAN.md"
        target.write_text("x")
        with _stub_daemon('{"allow": true, "reason": "ok"}') as (port, seen):
            result = _run_hook(
                {"cwd": str(tmp_path), "tool_name": "Edit",
                 "tool_input": {"file_path": str(target)}},
                port, "claude",
            )
        assert result.returncode == 0
        assert result.stdout == ""
        assert "new_file" not in seen[0]["query"]

    def test_a_deny_still_blocks_with_the_nudge_in_its_reason(self, tmp_path):
        answer = json.dumps({"allow": False, "reason": "No task.\n\nUse memory_stage.",
                             "nudge": "Use memory_stage."})
        with _stub_daemon(answer) as (port, _):
            result = _run_hook(
                {"cwd": str(tmp_path), "tool_name": "Write",
                 "tool_input": {"file_path": str(tmp_path / "CLAUDE.md")}},
                port, "claude",
            )
        assert result.returncode == 2
        assert "Use memory_stage." in result.stderr
        assert result.stdout == ""

    def test_codex_carries_the_guidance_in_its_agents_pointer(self):
        adapter = next(a for a in ADAPTERS if a.name == "codex")
        assert adapter.capabilities().markdown_nudge is False
        assert "memory_stage" in codex_mod.AGENTS_POINTER

    def test_opencode_declares_the_gap(self):
        adapter = next(a for a in ADAPTERS if a.name == "opencode")
        assert adapter.capabilities().markdown_nudge is False
