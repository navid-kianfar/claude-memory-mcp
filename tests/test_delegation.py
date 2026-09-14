"""The lead is told, by name, when it implements work that belongs to an agent.

The user, 2026-09-13: "most of the time the main agent does all the things and
does not respect the rules we had like having agents for each work." Prose lost
to momentum, so two mechanisms now read what the session actually did:

- the ESCALATION: the per-turn line turns into "STOP ... brief `python`" once the
  lead has edited enough source files on a task whose owner it never dispatched,
  and turns back the turn after that owner is dispatched;
- the DISPATCH GATE: a generic `backend`/`frontend` dispatch where the repo's own
  specialist applies and has not gone first puts a permission prompt in front of
  the USER naming that specialist. The user chose ask over deny: it never
  refuses.

Every test drives the real ledgers (registry on tmp_path), a real project and
real tasks; the scripts are run as the CLI would run them.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from memory_mcp import enforcement
from memory_mcp.container import container
from memory_mcp.db.registry import dispatches_for, record_dispatch, record_edit
from memory_mcp.models import CreateTaskRequest, TaskState, UpdateTaskRequest
from memory_mcp.services import stack_detect as sd

DISPATCH_HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "record-dispatch.sh"

ROSTER = [
    "app", "backend", "designer", "devops", "docs", "dotnet", "frontend", "go",
    "kotlin", "nodejs", "python", "react", "reviewer", "rust", "test",
]
SID = "claude-session-1"
PYPROJECT = "[project]\nname = 'x'\n"


def _pkg(**deps):
    return json.dumps({"name": "x", "dependencies": deps})


VITE_REACT = {
    "frontend/package.json": _pkg(react="18"),
    "frontend/vite.config.ts": "",
}


@pytest.fixture(autouse=True)
def _clean_memo():
    sd.clear_cache()
    yield
    sd.clear_cache()


@pytest.fixture
def agents(tmp_path, monkeypatch):
    directory = tmp_path / "agents"

    def install(names=ROSTER):
        directory.mkdir(exist_ok=True)
        for path in directory.glob("*.md"):
            path.unlink()
        for name in names:
            (directory / f"{name}.md").write_text(
                f"---\nname: {name}\ndescription: The {name} agent.\n---\n"
            )
        monkeypatch.setattr(enforcement, "AGENT_TEAM_DIR", directory)

    install()
    return install


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _files(root, files):
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


@pytest.fixture
def project(repo, agents):
    slug = "t-delegation"
    container.project_service.init_project(slug, "Delegation", project_path=str(repo))
    _files(repo, {"pyproject.toml": PYPROJECT, **VITE_REACT})
    return slug


def _task(slug, title="Build the flusher", role="python", start=True, parent_id=None):
    task = container.task_service.create(
        CreateTaskRequest(project=slug, title=title, role=role, parent_id=parent_id)
    )
    if start:
        container.task_service.start(slug, task.id, session_id="mem-1")
    return task


def _in_progress_without_clock(slug, task):
    """Moving a task to in_progress starts its clock; stopping the clock leaves
    the state alone. That pair is the only way to an idle in-progress task."""
    container.task_service.update(
        UpdateTaskRequest(project=slug, task_id=task.id, state=TaskState.IN_PROGRESS)
    )
    container.task_service.stop(slug, task.id)
    assert task.id not in container.task_repo.running_task_ids(slug)


def _edit(repo, *rels, session=SID, by_agent=None):
    for rel in rels:
        path = rel if os.path.isabs(rel) else str(repo / rel)
        record_edit(session, slug="t-delegation", path=path, tool="Edit",
                    by_agent=by_agent)


def _line(repo, slug, session=SID):
    return enforcement.agent_team_line(cwd=str(repo), slug=slug, session_id=session)


def _escalation(slug, session=SID):
    return enforcement.delegation_escalation(slug, session)


# -------------------------------------------------------------------- escalation


class TestEscalation:
    def test_two_files_is_still_the_lead_s_to_make(self, project, repo):
        _task(project)
        _edit(repo, "src/a.py", "src/b.py")

        assert _escalation(project) is None
        assert not _line(repo, project).startswith("[Agent team] STOP")

    def test_three_source_files_on_a_roled_task_names_the_role_and_the_task(
        self, project, repo,
    ):
        task = _task(project, title="Build the flusher", role="python")
        _edit(repo, "src/a.py", "src/b.py", "src/c.py")

        escalation = _escalation(project)
        line = _line(repo, project)

        assert escalation is not None
        assert (escalation.role, escalation.task_id, escalation.edited) == (
            "python", task.id, 3,
        )
        assert line == (
            "[Agent team] STOP: you have edited 3 source files this session on task "
            "'Build the flusher' (role python) without dispatching `python`, who "
            "owns that work. Hand it over now: brief `python` with the files and "
            "what done looks like, then integrate its report. Name the agent type "
            "in the dispatch description."
        )
        assert "\n" not in line and len(line) < 400

    def test_it_clears_the_turn_after_the_owner_is_dispatched(self, project, repo):
        _task(project, role="python")
        _edit(repo, "src/a.py", "src/b.py", "src/c.py")
        assert _line(repo, project).startswith("[Agent team] STOP")

        record_dispatch(SID, slug=project, agent_type="python")

        assert _escalation(project) is None
        assert "This repo: .=python; frontend/=react" in _line(repo, project)

    def test_dispatching_a_different_agent_does_not_clear_it(self, project, repo):
        _task(project, role="python")
        _edit(repo, "src/a.py", "src/b.py", "src/c.py")
        record_dispatch(SID, slug=project, agent_type="backend")

        assert _escalation(project).role == "python"

    def test_edits_made_by_agents_are_not_the_lead_s(self, project, repo):
        _task(project)
        _edit(repo, "src/a.py", "src/b.py", "src/c.py", by_agent="python")

        assert _escalation(project) is None

    def test_notes_docs_and_the_memory_snapshot_are_not_source(self, project, repo):
        _task(project)
        _edit(repo, "README.md", "src/notes.MD", "docs/guide.txt",
              ".claude/hooks/x.sh", ".claude-memory/manifest.json")

        assert _escalation(project) is None

    def test_files_outside_the_project_root_do_not_count(self, project, repo, tmp_path):
        _task(project)
        _edit(repo, *(str(tmp_path / "scratch" / f"{n}.py") for n in "abc"))

        assert _escalation(project) is None

    def test_the_same_file_three_times_is_one_file(self, project, repo):
        _task(project)
        _edit(repo, "src/a.py", "src/a.py", "src/./a.py")

        assert _escalation(project) is None

    def test_no_session_id_never_escalates(self, project, repo):
        _task(project)
        _edit(repo, "src/a.py", "src/b.py", "src/c.py")

        assert enforcement.delegation_escalation(project, "") is None
        line = enforcement.agent_team_line(cwd=str(repo), slug=project)
        assert not line.startswith("[Agent team] STOP")

    def test_edits_from_another_session_do_not_count(self, project, repo):
        _task(project)
        _edit(repo, "src/a.py", "src/b.py", "src/c.py", session="someone-else")

        assert _escalation(project) is None

    def test_no_task_in_progress_is_nothing_to_hand_over(self, project, repo):
        _task(project, start=False)
        _edit(repo, "src/a.py", "src/b.py", "src/c.py")

        assert _escalation(project) is None

    def test_an_unroled_task_names_the_agent_detected_for_the_edited_paths(
        self, project, repo,
    ):
        _task(project, title="Fix the task dialog", role=None)
        _edit(repo, "frontend/src/a.tsx", "frontend/src/b.tsx", "frontend/src/c.tsx",
              "src/x.py")

        escalation = _escalation(project)
        line = _line(repo, project)

        assert escalation.role == "react"
        assert escalation.detected_prefix == "frontend/src/"
        assert "(role unset; detected react for frontend/src/)" in line
        assert "without dispatching `react`" in line

    def test_the_task_with_a_running_clock_wins(self, project, repo):
        clocking = _task(project, title="Clocking", role="python")
        idle = _task(project, title="Idle but newer", role="react", start=False)
        _in_progress_without_clock(project, idle)
        _edit(repo, "src/a.py", "src/b.py", "src/c.py")

        assert _escalation(project).task_id == clocking.id

    def test_a_candidate_whose_owner_cannot_be_named_is_skipped(self, project, repo):
        """pm is the session itself; an uninstalled role cannot be dispatched."""
        _task(project, title="Plan it", role="pm")
        _task(project, title="Port to elixir", role="elixir")
        named = _task(project, title="The real one", role="react")
        _edit(repo, "src/a.py", "src/b.py", "src/c.py")

        assert _escalation(project).task_id == named.id

    def test_a_sub_task_in_progress_counts(self, project, repo):
        parent = _task(project, title="Parent", role=None, start=False)
        child = _task(project, title="Child", role="python", parent_id=parent.id)
        _edit(repo, "src/a.py", "src/b.py", "src/c.py")

        assert _escalation(project).task_id == child.id

    def test_the_stack_root_stands_in_for_an_unbound_project(self, repo, agents):
        slug = "t-unbound"
        container.project_service.init_project(slug, "Unbound")
        _files(repo, {"pyproject.toml": PYPROJECT})
        _task(slug)
        _edit(repo, "src/a.py", "src/b.py", "src/c.py")

        profile = sd.profile_for(str(repo), slug)
        escalation = enforcement.delegation_escalation(slug, SID, profile=profile)

        assert escalation is not None and escalation.role == "python"

    def test_a_ledger_that_explodes_costs_the_escalation_not_the_turn(
        self, project, repo, monkeypatch,
    ):
        _task(project)
        _edit(repo, "src/a.py", "src/b.py", "src/c.py")

        def boom(*_a, **_k):
            raise RuntimeError("registry locked")

        monkeypatch.setattr(enforcement, "edits_for", boom)
        block = enforcement.rules_text_for_project(project, cwd=str(repo), session_id=SID)

        assert "This repo: .=python; frontend/=react" in block


# ------------------------------------------------------------------ dispatch gate


def _gate(subagent_type, repo, slug="t-delegation", session=SID):
    return enforcement.dispatch_gate(
        subagent_type, cwd=str(repo), slug=slug, session_id=session,
    )


class TestTheDispatchGate:
    def test_generic_backend_in_a_python_repo_asks_naming_python(self, project, repo):
        answer = _gate("backend", repo)

        assert answer["decision"] == "ask"
        assert answer["specialists"] == ["python"]
        assert answer["reason"] == (
            "[Memory MCP] Claude is dispatching the generic `backend` agent, but this "
            "repo is python at `.` (pyproject.toml) and `python` carries the stack's "
            "conventions `backend` does not. Yes runs `backend` anyway; No stops it, "
            "and Claude should dispatch `python` instead. MEMORY_MCP_NO_GATE=1 turns "
            "this prompt off."
        )
        assert answer["context"].startswith(
            "[Memory MCP] Dispatch `python`, not `backend`: this repo is python at `.`"
        )

    def test_generic_frontend_asks_for_the_frontend_s_own_specialist(self, project, repo):
        answer = _gate("frontend", repo)

        assert answer["specialists"] == ["react"]
        assert "react at `frontend/` (frontend/package.json, frontend/vite.config.ts)" in (
            answer["reason"]
        )

    def test_backend_is_allowed_after_the_specialist_has_gone_first(self, project, repo):
        record_dispatch(SID, slug=project, agent_type="python")

        assert _gate("backend", repo) == {}

    def test_a_specialist_for_other_work_going_first_does_not_count(self, project, repo):
        """python planning the API says nothing about who builds the screens."""
        record_dispatch(SID, slug=project, agent_type="python")

        assert _gate("frontend", repo)["specialists"] == ["react"]

    @pytest.mark.parametrize("agent", [
        "general-purpose", "Explore", "Plan", "test", "reviewer", "designer",
        "devops", "docs", "pm", "python", "react", "nodejs", "",
    ])
    def test_never_asks_about_anything_but_the_generic_roles(self, project, repo, agent):
        assert _gate(agent, repo) == {}

    def test_a_hit_the_generic_role_legitimately_owns_lets_it_through(self, agents, repo):
        slug = "t-mixed"
        container.project_service.init_project(slug, "Mixed", project_path=str(repo))
        _files(repo, {
            "services/api/pyproject.toml": PYPROJECT,
            "services/legacy/pom.xml": "<project/>",
            "services/legacy/src/main/java/App.java": "class App {}",
        })

        assert _gate("backend", repo, slug=slug) == {}

    def test_an_uninstalled_specialist_lets_the_generic_role_through(self, agents, repo):
        agents([n for n in ROSTER if n != "go"])
        slug = "t-go"
        container.project_service.init_project(slug, "Go", project_path=str(repo))
        _files(repo, {"go.mod": "module x\n"})

        assert _gate("backend", repo, slug=slug) == {}

    def test_a_warning_hit_lets_the_generic_role_through(self, agents, repo):
        slug = "t-electron"
        container.project_service.init_project(slug, "E", project_path=str(repo))
        _files(repo, {"package.json": _pkg(electron="30")})

        assert _gate("backend", repo, slug=slug) == {}

    def test_frontend_in_a_repo_with_no_frontend_is_not_asked_about(self, agents, repo):
        slug = "t-py"
        container.project_service.init_project(slug, "Py", project_path=str(repo))
        _files(repo, {"pyproject.toml": PYPROJECT})

        assert _gate("frontend", repo, slug=slug) == {}

    def test_a_nextjs_repo_is_nodejs_for_server_work_and_react_for_screens(
        self, agents, repo,
    ):
        slug = "t-next"
        container.project_service.init_project(slug, "Next", project_path=str(repo))
        _files(repo, {"package.json": _pkg(next="14", react="18")})

        assert _gate("backend", repo, slug=slug)["specialists"] == ["nodejs"]
        assert _gate("frontend", repo, slug=slug)["specialists"] == ["react"]
        record_dispatch(SID, slug=slug, agent_type="nodejs")
        assert _gate("frontend", repo, slug=slug) == {}

    def test_nothing_to_go_on_means_nothing_asked(self, project, repo, tmp_path,
                                                  monkeypatch):
        empty = tmp_path / "empty"
        (empty / ".git").mkdir(parents=True)

        assert _gate("backend", repo, session="") == {}
        assert _gate("backend", repo, slug=None) == {}
        assert _gate("backend", empty, slug="nowhere") == {}
        monkeypatch.setattr(enforcement, "AGENT_TEAM_DIR", tmp_path / "no-agents")
        assert _gate("backend", repo) == {}


# ------------------------------------------------------------------------- route


class _Body:
    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.cookies = {}
        self.scope = {"type": "http", "headers": []}

    async def json(self):
        return self._body


def _route(body):
    from memory_mcp.web import hooks

    return json.loads(asyncio.run(hooks._hook_dispatch(_Body(body))).body)


class TestTheDispatchRoute:
    @pytest.fixture(autouse=True)
    def _project_of(self, project, monkeypatch):
        monkeypatch.setattr("memory_mcp.context.detect_project_from_cwd",
                            lambda cwd: project)

    def test_it_asks_and_still_records(self, repo):
        answer = _route({"session_id": SID, "cwd": str(repo),
                         "subagent_type": "backend", "description": "x (backend)"})

        assert answer["decision"] == "ask"
        assert [r["agent_type"] for r in dispatches_for(SID)] == ["backend"]

    def test_the_answer_reads_the_ledger_as_it_was_before_this_dispatch(self, repo):
        assert _route({"session_id": SID, "cwd": str(repo),
                       "subagent_type": "python"}) == {}
        assert _route({"session_id": SID, "cwd": str(repo),
                       "subagent_type": "backend"}) == {}
        assert [r["agent_type"] for r in dispatches_for(SID)] == ["python", "backend"]

    def test_a_gate_that_explodes_lets_the_dispatch_run_and_records_it(
        self, repo, monkeypatch,
    ):
        def boom(*_a, **_k):
            raise RuntimeError("detector gone")

        monkeypatch.setattr(enforcement, "dispatch_gate", boom)

        assert _route({"session_id": SID, "cwd": str(repo),
                       "subagent_type": "backend"}) == {}
        assert len(dispatches_for(SID)) == 1

    def test_a_non_string_subagent_type_is_the_default_agent(self, repo):
        assert _route({"session_id": SID, "cwd": str(repo), "subagent_type": 7}) == {}
        assert dispatches_for(SID)[0]["agent_type"] == "general-purpose"


# ------------------------------------------------------------------------ script


@contextmanager
def _daemon(answer: str):
    seen: list[dict] = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen.append(json.loads(self.rfile.read(length) or b"{}"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(answer.encode())

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run(payload, port, **env):
    full = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port), **env}
    full.pop("MEMORY_MCP_URL", None)
    if "MEMORY_MCP_NO_GATE" not in env:
        full.pop("MEMORY_MCP_NO_GATE", None)
    return subprocess.run(
        ["bash", str(DISPATCH_HOOK)], input=json.dumps(payload), text=True,
        capture_output=True, timeout=30, env=full,
    )


PAYLOAD = {"session_id": SID, "cwd": "/repo", "tool_name": "Agent",
           "tool_input": {"subagent_type": "backend", "description": "x (backend)"}}


@pytest.mark.skipif(not DISPATCH_HOOK.exists(), reason="hook script not present")
class TestTheDispatchScript:
    def test_the_daemon_s_real_answer_becomes_the_documented_ask(self, project, repo):
        """End to end on the contract: what `dispatch_gate` answers is what the
        script turns into Claude Code's PreToolUse `permissionDecision: "ask"`."""
        answer = _gate("backend", repo)
        with _daemon(json.dumps(answer)) as (port, seen):
            result = _run(PAYLOAD, port)

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert json.loads(result.stdout) == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": answer["reason"],
                "additionalContext": answer["context"],
            }
        }
        assert seen[0]["subagent_type"] == "backend"

    def test_an_ask_with_no_text_still_asks(self):
        with _daemon('{"decision": "ask"}') as (port, _seen):
            result = _run(PAYLOAD, port)

        out = json.loads(result.stdout)["hookSpecificOutput"]
        assert result.returncode == 0
        assert out["permissionDecision"] == "ask"
        assert out["permissionDecisionReason"]
        assert "additionalContext" not in out

    def test_the_off_switch_silences_the_prompt_not_the_ledger(self):
        with _daemon('{"decision": "ask", "reason": "r"}') as (port, seen):
            result = _run(PAYLOAD, port, MEMORY_MCP_NO_GATE="1")

        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert len(seen) == 1

    @pytest.mark.parametrize("answer", [
        '{"decision": "block", "reason": "no"}', "{}", "", "<html>", "[1]",
    ])
    def test_nothing_but_ask_or_deny_is_acted_on(self, answer):
        with _daemon(answer) as (port, _seen):
            result = _run(PAYLOAD, port)

        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")

    def test_no_daemon_allows_quickly_and_silently(self):
        started = time.monotonic()
        result = _run({**PAYLOAD, "cwd": str(DISPATCH_HOOK.parent)}, 9)

        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert time.monotonic() - started < 1.5
