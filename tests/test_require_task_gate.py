"""The PreToolUse gate: no work on a bound project without a task in progress.

This is the ONLY hook in the project that can refuse anything. The other three
print into the model's context and hope - and a rule saying "put the work on
the board first" was followed roughly 70% of the time, which is what this
exists to fix. Text can remind; only an exit status can require.

Every test here is really about the same property: it blocks the one case it
must, and OPENS on everything else. A gate that stops a person editing a file
because a board is unreachable would be a far worse bug than the one it fixes.
"""

import asyncio
import json
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from memory_mcp.container import container
from memory_mcp.db.connection import get_connection
from memory_mcp.db.registry import (
    client_session, edits_for, upsert_project_link,
)
from memory_mcp.models import CreateTaskRequest, TaskState, UpdateTaskRequest
from memory_mcp.web import routes

HOOKS_DIR = Path(__file__).resolve().parents[1] / ".claude" / "hooks"
HOOK = HOOKS_DIR / "require-task.sh"
DISPATCH_HOOK = HOOKS_DIR / "record-dispatch.sh"


@pytest.fixture
def project(tmp_path):
    slug = "t-gate"
    container.project_repo.register(slug, slug)
    get_connection(slug).close()
    return slug


class _Req:
    """The slice of a Starlette request the gate actually reads."""

    def __init__(self, cwd, **extra):
        self.query_params = {"cwd": cwd, **extra}
        self.headers = {}
        self.cookies = {}
        self.scope = {"type": "http", "headers": []}


def _gate(cwd, **extra):
    """`extra` is what the hook script now forwards beside cwd: session_id,
    tool_name, file_path, and the agent fields when the CLI sends them."""
    response = asyncio.run(routes._hook_gate(_Req(cwd, **extra)))
    return json.loads(response.body)


def _bind(slug):
    upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id="wp1",
    )


class TestTheGateDecision:
    def test_a_directory_that_is_not_a_project_is_allowed(self):
        assert _gate("/tmp/definitely-not-a-memory-project")["allow"] is True

    def test_an_unbound_project_is_allowed(self, project, monkeypatch):
        """No board means no queue to be out of step with."""
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        answer = _gate("/anywhere")
        assert answer["allow"] is True
        assert "not bound" in answer["reason"]

    def test_a_bound_project_with_no_task_in_progress_is_BLOCKED(
        self, project, monkeypatch,
    ):
        """The one case the gate exists for."""
        _bind(project)
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        container.task_service.create(
            CreateTaskRequest(project=project, title="Queued, not started")
        )
        answer = _gate("/anywhere")
        assert answer["allow"] is False
        # The reason has to name the way out, or the model cannot act on it.
        assert "memory_task_start" in answer["reason"]
        assert "memory_task_plan" in answer["reason"]

    def test_starting_a_task_opens_the_gate(self, project, monkeypatch):
        _bind(project)
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        task = container.task_service.create(
            CreateTaskRequest(project=project, title="Real work")
        )
        assert _gate("/anywhere")["allow"] is False
        container.task_service.start(project, task.id)
        answer = _gate("/anywhere")
        assert answer["allow"] is True
        assert "Real work" in answer["reason"]

    def test_a_started_sub_task_opens_the_gate(self, project, monkeypatch):
        """Starting the sub-task being worked must be enough. When it was not,
        the way through was to start the parent, whose clock then held the
        sub-task's work."""
        _bind(project)
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        parent = container.task_service.create(
            CreateTaskRequest(project=project, title="The feature")
        )
        child = container.task_service.create(
            CreateTaskRequest(project=project, title="Its first part", parent_id=parent.id)
        )
        container.task_service.start(project, child.id)
        answer = _gate("/anywhere")
        assert answer["allow"] is True
        assert answer["task_id"] == child.id

    def test_finishing_the_task_closes_it_again(self, project, monkeypatch):
        _bind(project)
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        task = container.task_service.create(
            CreateTaskRequest(project=project, title="Done soon")
        )
        container.task_service.start(project, task.id)
        container.task_service.done(project, task.id)
        assert _gate("/anywhere")["allow"] is False

    def test_a_paused_task_does_not_hold_the_gate_open(self, project, monkeypatch):
        """Pausing is stopping work; the gate must reflect that."""
        _bind(project)
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        task = container.task_service.create(
            CreateTaskRequest(project=project, title="Paused")
        )
        container.task_service.start(project, task.id)
        container.task_service.update(
            UpdateTaskRequest(
                project=project, task_id=task.id, state=TaskState.PAUSED,
            )
        )
        assert _gate("/anywhere")["allow"] is False

    def test_an_exception_inside_the_gate_fails_OPEN(self, project, monkeypatch):
        """The property that makes this safe to install by default."""
        def _boom(cwd):
            raise RuntimeError("registry is on fire")

        monkeypatch.setattr("memory_mcp.context.detect_project_from_cwd", _boom)
        answer = _gate("/anywhere")
        assert answer["allow"] is True
        assert "failing open" in answer["reason"]


class TestScopeIsTheProjectRoot:
    """An edit outside the project's own folder is not work this board tracks.

    This blocked three agents writing to a scratchpad, and would block every
    future one. The gate only learned it was possible when `tool_input.file_path`
    started being forwarded.
    """

    @pytest.fixture
    def bound(self, project, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        container.project_service.link_folder(project, str(root))
        _bind(project)
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        return project, root

    def test_a_path_outside_the_project_root_is_allowed(self, bound):
        _, root = bound
        answer = _gate(str(root), session_id="s1", tool_name="Write",
                       file_path="/private/tmp/scratch/x.py")

        assert answer["allow"] is True
        assert answer["reason"] == "outside the project root"

    def test_a_path_inside_the_root_with_no_task_still_BLOCKS(self, bound):
        """The scope fix must not become a way around the gate."""
        _, root = bound
        answer = _gate(str(root), session_id="s1", tool_name="Write",
                       file_path=str(root / "src" / "x.py"))

        assert answer["allow"] is False
        assert "memory_task_start" in answer["reason"]

    def test_the_root_itself_is_inside_the_root(self, bound):
        _, root = bound
        answer = _gate(str(root), session_id="s1", tool_name="Write",
                       file_path=str(root))

        assert answer["allow"] is False

    def test_a_sibling_folder_sharing_the_roots_name_prefix_is_outside(self, bound):
        """`/x/root-backup` is not under `/x/root`, however much it looks it."""
        _, root = bound
        answer = _gate(str(root), session_id="s1", tool_name="Write",
                       file_path=str(root.parent / "root-backup" / "x.py"))

        assert answer["reason"] == "outside the project root"

    def test_with_no_file_path_the_gate_behaves_exactly_as_before(self, bound):
        """An installed hook script older than this release forwards no file_path.
        It must keep getting the old answer, not an accidental allow."""
        _, root = bound

        assert _gate(str(root), session_id="s1")["allow"] is False

    def test_a_project_with_no_bound_folder_cannot_judge_scope(self, project,
                                                               monkeypatch):
        _bind(project)
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        answer = _gate("/anywhere", session_id="s1", file_path="/private/tmp/x.py")

        assert answer["allow"] is False


class TestTheGateIsAlsoTheEditLedger:
    """Every Edit/Write already comes through here, so recording the path costs
    nothing. What must not happen is a ledger failure reaching the decision."""

    @pytest.fixture
    def bound(self, project, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        container.project_service.link_folder(project, str(root))
        _bind(project)
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: project,
        )
        return project, root

    def test_an_edit_inside_the_root_leaves_one_row(self, bound):
        _, root = bound
        target = str(root / "src" / "x.py")

        _gate(str(root), session_id="s1", tool_name="Write", file_path=target)
        rows = edits_for("s1")

        assert len(rows) == 1
        assert rows[0]["path"] == target
        assert rows[0]["tool"] == "Write"
        assert rows[0]["slug"] == "t-gate"
        # No agent_id in the payload, so this reads as the lead's edit.
        assert rows[0]["by_agent"] is None

    def test_an_edit_OUTSIDE_the_root_is_recorded_too(self, bound):
        """The delegation count wants files the session touched, and a scratchpad
        write is still something the session did. The allow and the row are
        separate decisions."""
        _, root = bound

        _gate(str(root), session_id="s1", tool_name="Write",
              file_path="/private/tmp/x.py")

        assert len(edits_for("s1")) == 1

    def test_an_agents_edit_names_the_agent(self, bound):
        _, root = bound

        _gate(str(root), session_id="s1", tool_name="Edit", agent_id="a1",
              agent_type="python", file_path=str(root / "a.py"))

        assert edits_for("s1")[0]["by_agent"] == "python"

    def test_a_call_with_no_session_id_records_nothing(self, bound):
        _, root = bound

        _gate(str(root), tool_name="Write", file_path=str(root / "a.py"))

        assert edits_for("") == []

    def test_a_call_with_no_file_path_records_nothing(self, bound):
        _, root = bound

        _gate(str(root), session_id="s1", tool_name="Read")

        assert edits_for("s1") == []

    def test_a_directory_that_is_not_a_project_records_nothing(self):
        _gate("/tmp/definitely-not-a-memory-project", session_id="s1",
              tool_name="Write", file_path="/tmp/x.py")

        assert edits_for("s1") == []

    def test_the_session_is_remembered_with_its_transcript(self, bound):
        _, root = bound

        _gate(str(root), session_id="s1", transcript_path="/t/s1.jsonl",
              tool_name="Write", file_path=str(root / "a.py"))

        row = client_session("s1")
        assert row["transcript_path"] == "/t/s1.jsonl"
        assert row["slug"] == "t-gate"

    def test_a_ledger_that_explodes_does_not_change_the_decision(self, bound,
                                                                 monkeypatch):
        _, root = bound

        def _boom(*_args, **_kwargs):
            raise RuntimeError("registry is on fire")

        monkeypatch.setattr("memory_mcp.web.hooks.record_edit", _boom)
        monkeypatch.setattr("memory_mcp.web.hooks.touch_client_session", _boom)
        answer = _gate(str(root), session_id="s1", tool_name="Write",
                       file_path=str(root / "a.py"))

        # Still the honest answer for a bound project with no task. Failing open
        # here would turn a bookkeeping bug into a way around the one thing this
        # hook exists to enforce, which is why the ledger calls are wrapped at
        # their call sites and not only inside the accessors.
        assert answer["allow"] is False
        assert "memory_task_start" in answer["reason"]


@pytest.mark.skipif(not HOOK.exists(), reason="hook script not present")
class TestTheHookScript:
    """The script's own contract: exit 2 blocks, exit 0 allows."""

    def _run(self, payload, env=None):
        return subprocess.run(
            ["bash", str(HOOK)], input=json.dumps(payload), text=True,
            capture_output=True, timeout=30, env=env,
        )

    def test_an_unreachable_daemon_allows(self):
        """A dead daemon must never stop someone editing a file."""
        import os

        env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": "9"}  # nothing listens on 9
        result = self._run({"cwd": str(HOOK.parent)}, env=env)
        assert result.returncode == 0, result.stderr

    def test_the_off_switch_allows(self):
        import os

        env = {**os.environ, "MEMORY_MCP_NO_GATE": "1"}
        result = self._run({"cwd": str(HOOK.parent)}, env=env)
        assert result.returncode == 0

    def test_no_cwd_allows(self):
        assert self._run({}).returncode == 0

    def test_the_payload_is_data_not_shell(self):
        """The script evals shell-quoted assignments, so a payload that looks like
        shell must stay a string. A hook parsing an untrusted payload is the one
        place in this repo where an injection would run as the user."""
        import os

        canary = Path(os.environ.get("TMPDIR", "/tmp")) / "memory-mcp-gate-canary"
        canary.unlink(missing_ok=True)
        env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": "9"}
        result = self._run(
            {"cwd": str(HOOK.parent),
             "session_id": f"$(touch {canary})",
             "tool_input": {"file_path": f"x`touch {canary}`"}},
            env=env,
        )

        assert result.returncode == 0, result.stderr
        assert not canary.exists(), "the payload was executed as shell"

    def test_it_forwards_the_session_identity(self):
        """What the daemon now receives. Checked against a stub rather than the
        real daemon so the assertion is about the SCRIPT, not the route."""
        import os

        with _stub_daemon('{"allow": true, "reason": "ok"}') as (port, seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            result = self._run(
                {"cwd": str(HOOK.parent), "session_id": "s-fwd",
                 "agent_id": "a1", "agent_type": "python",
                 "transcript_path": "/t/s-fwd.jsonl", "tool_name": "Write",
                 "tool_input": {"file_path": "/repo/x.py"}},
                env=env,
            )

        assert result.returncode == 0, result.stderr
        query = seen[0]["query"]
        assert query["cwd"] == [str(HOOK.parent)]
        assert query["session_id"] == ["s-fwd"]
        assert query["agent_id"] == ["a1"]
        assert query["agent_type"] == ["python"]
        assert query["transcript_path"] == ["/t/s-fwd.jsonl"]
        assert query["tool_name"] == ["Write"]
        assert query["file_path"] == ["/repo/x.py"]


@contextmanager
def _stub_daemon(answer: str, status: int = 200):
    """A throwaway HTTP server standing in for the daemon, recording what the
    hook script actually sent. The scripts are the contract here, and the only
    honest way to test a bash script's request is to receive it."""
    seen: list[dict] = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _record(self, body=b""):
            parsed = urlparse(self.path)
            seen.append({
                "path": parsed.path,
                "query": parse_qs(parsed.query, keep_blank_values=True),
                "body": body.decode() or None,
            })
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(answer.encode())

        def do_GET(self):
            self._record()

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self._record(self.rfile.read(length))

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(not DISPATCH_HOOK.exists(), reason="hook script not present")
class TestTheDispatchScript:
    """record-dispatch.sh: it records, it can ASK, and it refuses one thing.

    The user chose ask over deny for delegation (2026-09-13). The one refusal is a
    SUBAGENT dispatching (2026-09-14): only the lead starts agents. Either way the
    script exits 0 with JSON - never 2. The full ask contract, end to end against
    the daemon's real answer, is in `tests/test_delegation.py::TestTheDispatchScript`.
    """

    def _run(self, payload, env=None):
        return subprocess.run(
            ["bash", str(DISPATCH_HOOK)], input=json.dumps(payload), text=True,
            capture_output=True, timeout=30, env=env,
        )

    def test_an_unreachable_daemon_allows_silently(self):
        import os

        env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": "9"}
        result = self._run(
            {"cwd": str(DISPATCH_HOOK.parent), "session_id": "s1",
             "tool_input": {"subagent_type": "python"}},
            env=env,
        )

        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""

    def test_no_cwd_allows(self):
        assert self._run({}).returncode == 0

    def test_it_posts_the_dispatch_as_json(self):
        import os

        with _stub_daemon("{}") as (port, seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            result = self._run(
                {"cwd": "/repo", "session_id": "s1", "tool_use_id": "t1",
                 "agent_id": "a1", "agent_type": "python",
                 "tool_input": {"subagent_type": "react",
                                "description": "Build the screen (react)"}},
                env=env,
            )

        assert result.returncode == 0, result.stderr
        assert seen[0]["path"] == "/api/hook/dispatch"
        assert json.loads(seen[0]["body"]) == {
            "session_id": "s1",
            "cwd": "/repo",
            "subagent_type": "react",
            "description": "Build the screen (react)",
            "tool_use_id": "t1",
            "agent_id": "a1",
            "agent_type": "python",
            "event": "PreToolUse",
        }

    def test_a_dispatch_with_no_subagent_type_is_the_default_agent(self):
        import os

        with _stub_daemon("{}") as (port, seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            self._run({"cwd": "/repo", "session_id": "s1"}, env=env)

        assert json.loads(seen[0]["body"])["subagent_type"] == "general-purpose"

    def test_a_description_with_quotes_and_newlines_survives(self):
        """It is free text a person wrote, which is why it goes in a JSON body
        built by Python rather than a URL built by bash."""
        import os

        nasty = 'He said "go"\n\tand $(echo no) `echo no` \'quoted\''
        with _stub_daemon("{}") as (port, seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            result = self._run(
                {"cwd": "/repo", "session_id": "s1",
                 "tool_input": {"subagent_type": "python", "description": nasty}},
                env=env,
            )

        assert result.returncode == 0, result.stderr
        assert json.loads(seen[0]["body"])["description"] == nasty

    def test_an_empty_answer_allows(self):
        import os

        with _stub_daemon("") as (port, _seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            result = self._run({"cwd": "/repo", "session_id": "s1"}, env=env)

        assert result.returncode == 0

    def test_an_unreadable_answer_allows(self):
        import os

        with _stub_daemon("<html>not json</html>") as (port, _seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            result = self._run({"cwd": "/repo", "session_id": "s1"}, env=env)

        assert result.returncode == 0

    def test_an_empty_object_allows(self):
        """The answer this task's daemon actually gives."""
        import os

        with _stub_daemon("{}") as (port, _seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            result = self._run({"cwd": "/repo", "session_id": "s1"}, env=env)

        assert result.returncode == 0
        assert result.stderr == ""

    def test_an_ask_prompts_the_user_with_the_reason(self):
        import os

        answer = json.dumps({"decision": "ask", "reason": "dispatch python"})
        with _stub_daemon(answer) as (port, _seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            env.pop("MEMORY_MCP_NO_GATE", None)
            result = self._run({"cwd": "/repo", "session_id": "s1"}, env=env)

        assert result.returncode == 0
        assert result.stderr == ""
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "ask"
        assert decision["permissionDecisionReason"] == "dispatch python"

    def test_a_deny_is_a_json_refusal_never_an_exit_2(self):
        """The daemon denies only a dispatch made from inside a subagent. It goes
        out as Claude Code's `permissionDecision: "deny"` on exit 0."""
        import os

        with _stub_daemon('{"decision": "deny", "reason": "only the lead"}') as (port, _seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            env.pop("MEMORY_MCP_NO_GATE", None)
            result = self._run({"cwd": "/repo", "session_id": "s1"}, env=env)

        assert (result.returncode, result.stderr) == (0, "")
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"] == "only the lead"

    def test_the_off_switch_does_not_reopen_nested_dispatch(self):
        """MEMORY_MCP_NO_GATE silences prompts - a delegation preference. The deny
        is the swarm guard, and switching prompts off must not bring swarms back."""
        import os

        with _stub_daemon('{"decision": "deny", "reason": "only the lead"}') as (port, _seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port),
                   "MEMORY_MCP_NO_GATE": "1"}
            result = self._run({"cwd": "/repo", "session_id": "s1"}, env=env)

        assert result.returncode == 0
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_the_off_switch_silences_an_ask(self):
        import os

        answer = json.dumps({"decision": "ask", "reason": "dispatch python"})
        with _stub_daemon(answer) as (port, seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port),
                   "MEMORY_MCP_NO_GATE": "1"}
            result = self._run({"cwd": "/repo", "session_id": "s1"}, env=env)

        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""
        # And the ledger still filled: turning the gate off silences the prompt,
        # not the bookkeeping the advice is built from.
        assert len(seen) == 1

    def test_the_payload_is_data_not_shell(self):
        import os

        canary = Path(os.environ.get("TMPDIR", "/tmp")) / "memory-mcp-dispatch-canary"
        canary.unlink(missing_ok=True)
        with _stub_daemon("{}") as (port, _seen):
            env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port)}
            result = self._run(
                {"cwd": f"/repo$(touch {canary})",
                 "session_id": f"`touch {canary}`",
                 "tool_input": {"description": f"; touch {canary}"}},
                env=env,
            )

        assert result.returncode == 0, result.stderr
        assert not canary.exists(), "the payload was executed as shell"
