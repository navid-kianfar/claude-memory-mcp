"""POST /api/hook/attachments and the two scripts that call it.

The route is where an untrusted hook payload meets a daemon that will copy what
it reads towards a remote board, so most of this file is about what it refuses:
a subagent's turn, a directory that is not a project, and every `transcript_path`
that is not a regular file inside `~/.claude/projects/` - outside it, through a
symlink, through `..`, a directory, a FIFO, a hard link, a subagent transcript.
Every refusal is an empty body, and so is every exception: a hook never sees a 500.

The scripts are tested as scripts, against a stub daemon, because the contract a
bash hook keeps is what it sends and what it prints.
"""

import asyncio
import json
import os
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from memory_mcp.config import settings
from memory_mcp.container import container
from memory_mcp.db.connection import connect
from memory_mcp.models import CreateTaskRequest
from memory_mcp.services import attachment_inbox as inbox_mod
from memory_mcp.web import hooks
from tests.test_attachment_inbox import SID, png, paste, screenshot_result, write

HOOKS_DIR = Path(__file__).resolve().parents[1] / ".claude" / "hooks"
INJECT = HOOKS_DIR / "inject-rules.sh"
SESSION_END = HOOKS_DIR / "session-end.sh"


@pytest.fixture(autouse=True)
def unhurried(monkeypatch):
    monkeypatch.setattr(inbox_mod, "SCAN_BUDGET_SECONDS", 3600)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "-repo").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def transcript(home):
    return home / ".claude" / "projects" / "-repo" / f"{SID}.jsonl"


@pytest.fixture
def project(monkeypatch, tmp_path):
    slug = "hook-attach"
    container.project_service.init_project(slug, "Hook Attach")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "memory_mcp.context.detect_project_from_cwd",
        lambda cwd: slug if cwd and cwd.startswith(str(repo)) else None,
    )
    return slug, repo


class _Req:
    """The slice of a Starlette request the route reads: headers for the auth
    check, and the body as a stream."""

    def __init__(self, body):
        self._raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = {}
        self.cookies = {}
        self.query_params = {}
        self.scope = {"type": "http", "headers": []}

    async def stream(self):
        for i in range(0, len(self._raw), 4096):
            yield self._raw[i:i + 4096]
        yield b""


def call(body) -> tuple[int, str]:
    response = asyncio.run(hooks._hook_attachments(_Req(body)))
    return response.status_code, response.body.decode()


def payload(repo, transcript, event="UserPromptSubmit", **extra) -> dict:
    return {"session_id": SID, "cwd": str(repo), "transcript_path": str(transcript),
            "event": event, "agent_id": "", "agent_type": "", **extra}


def inbox_count(slug) -> int:
    with connect(slug) as conn:
        return conn.execute("SELECT count(*) FROM attachment_inbox").fetchone()[0]


def store_is_empty(slug) -> bool:
    root = Path(settings.data_dir) / "attachments" / slug
    return not root.exists() or not any(p.is_file() for p in root.rglob("*"))


# ---------- who is answered ----------


class TestWhoIsAnswered:
    def test_a_paste_in_a_project_is_parked_and_announced(self, project, transcript):
        slug, repo = project
        write(transcript, paste(png(1)))

        status, body = call(payload(repo, transcript))

        assert status == 200
        assert body.startswith("[Memory MCP] The user attached attachment-")
        assert inbox_count(slug) == 1

    def test_a_directory_that_is_not_a_project_is_silent(self, project, transcript, tmp_path):
        slug, _ = project
        write(transcript, paste(png(1)))

        assert call(payload(tmp_path / "elsewhere", transcript)) == (200, "")
        assert inbox_count(slug) == 0

    def test_a_subagents_turn_is_silent_and_not_scanned(self, project, transcript,
                                                       monkeypatch):
        _, repo = project
        write(transcript, paste(png(1)))
        scans = []
        monkeypatch.setattr(container.attachment_inbox_service, "handle_hook",
                            lambda *a, **k: scans.append(a) or "boom")

        assert call(payload(repo, transcript, agent_id="a-1", agent_type="python")) == (200, "")
        assert scans == []

    def test_a_turn_inside_an_agent_worktree_is_silent(self, project, transcript, monkeypatch):
        _, repo = project
        scans = []
        monkeypatch.setattr(container.attachment_inbox_service, "handle_hook",
                            lambda *a, **k: scans.append(a) or "boom")
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: "hook-attach",
        )

        cwd = repo / ".claude" / "worktrees" / "agent-1"
        assert call(payload(cwd, transcript)) == (200, "")
        assert scans == []

    @pytest.mark.parametrize("event", ["", "PreToolUse", "SessionStart", None, ["Stop"]])
    def test_an_event_it_does_not_scan_on_is_silent(self, project, transcript, event):
        slug, repo = project
        write(transcript, paste(png(1)))

        assert call(payload(repo, transcript, event=event)) == (200, "")
        assert inbox_count(slug) == 0

    def test_no_session_id_is_silent(self, project, transcript):
        slug, repo = project
        write(transcript, paste(png(1)))

        assert call(payload(repo, transcript, session_id="")) == (200, "")
        assert inbox_count(slug) == 0

    def test_a_session_id_that_is_not_an_id_is_silent(self, project, transcript):
        slug, repo = project
        write(transcript, paste(png(1)))

        assert call(payload(repo, transcript, session_id="../../etc")) == (200, "")
        assert inbox_count(slug) == 0

    def test_server_mode_never_reads_a_transcript(self, project, transcript, monkeypatch):
        slug, repo = project
        write(transcript, paste(png(1)))
        monkeypatch.setattr(settings, "mode", "server")
        # Authorised, so the refusal under test is the transcript one and not
        # the missing bearer token.
        monkeypatch.setattr(hooks, "_hook_authorized", lambda request: True)

        assert call(payload(repo, transcript)) == (200, "")
        assert inbox_count(slug) == 0

    def test_an_exception_inside_is_an_empty_200(self, project, transcript, monkeypatch):
        _, repo = project

        def explode(*_a, **_k):
            raise RuntimeError("/Users/secret/path in a stack trace")

        monkeypatch.setattr(container.attachment_inbox_service, "handle_hook", explode)

        assert call(payload(repo, transcript)) == (200, "")

    @pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b"", b"{" + b" " * 70_000 + b"}"])
    def test_a_body_that_is_not_a_small_json_object_is_silent(self, project, raw):
        assert call(raw) == (200, "")


# ---------- which transcripts may be opened ----------


class TestTheTranscriptIsConfined:
    """A paste written somewhere the daemon must never read, in every case: if
    anything were opened, the paste would be parked and the store non-empty."""

    def test_a_path_outside_claude_projects_is_refused(self, project, home, tmp_path):
        slug, repo = project
        outside = tmp_path / "outside" / f"{SID}.jsonl"
        outside.parent.mkdir()
        write(outside, paste(png(1)))

        assert call(payload(repo, outside)) == (200, "")
        assert inbox_count(slug) == 0 and store_is_empty(slug)

    def test_a_symlink_out_of_the_tree_is_refused(self, project, home, tmp_path):
        slug, repo = project
        outside = tmp_path / "outside.jsonl"
        write(outside, paste(png(1)))
        link = home / ".claude" / "projects" / "-repo" / f"{SID}.jsonl"
        link.symlink_to(outside)

        assert call(payload(repo, link)) == (200, "")
        assert inbox_count(slug) == 0 and store_is_empty(slug)

    def test_a_symlinked_transcript_is_refused_even_inside_the_tree(self, project, home):
        slug, repo = project
        folder = home / ".claude" / "projects" / "-repo"
        write(folder / "real.jsonl", paste(png(1)))
        (folder / f"{SID}.jsonl").symlink_to(folder / "real.jsonl")

        assert call(payload(repo, folder / f"{SID}.jsonl")) == (200, "")
        assert inbox_count(slug) == 0

    def test_a_symlinked_folder_out_of_the_tree_is_refused(self, project, home, tmp_path):
        slug, repo = project
        (tmp_path / "evil").mkdir()
        write(tmp_path / "evil" / f"{SID}.jsonl", paste(png(1)))
        (home / ".claude" / "projects" / "-evil").symlink_to(tmp_path / "evil")

        path = home / ".claude" / "projects" / "-evil" / f"{SID}.jsonl"
        assert call(payload(repo, path)) == (200, "")
        assert inbox_count(slug) == 0 and store_is_empty(slug)

    def test_dot_dot_is_refused_even_when_it_lands_inside(self, project, home, transcript):
        slug, repo = project
        write(transcript, paste(png(1)))
        sneaky = f"{home}/.claude/projects/-repo/../-repo/{SID}.jsonl"

        assert call(payload(repo, sneaky)) == (200, "")
        assert inbox_count(slug) == 0

    def test_a_relative_path_is_refused(self, project, transcript):
        slug, repo = project
        write(transcript, paste(png(1)))

        assert call(payload(repo, f".claude/projects/-repo/{SID}.jsonl")) == (200, "")
        assert inbox_count(slug) == 0

    def test_a_directory_is_refused(self, project, home):
        slug, repo = project
        folder = home / ".claude" / "projects" / "-repo" / "dir.jsonl"
        folder.mkdir()

        assert call(payload(repo, folder)) == (200, "")

    def test_a_fifo_is_refused_without_blocking(self, project, home):
        slug, repo = project
        fifo = home / ".claude" / "projects" / "-repo" / "pipe.jsonl"
        os.mkfifo(fifo)
        result = {}
        worker = threading.Thread(target=lambda: result.update(r=call(payload(repo, fifo))))
        worker.start()
        worker.join(timeout=20)

        assert not worker.is_alive(), "opening a FIFO would block the hook"
        assert result["r"] == (200, "")

    def test_a_hard_link_is_refused(self, project, home, tmp_path):
        slug, repo = project
        outside = tmp_path / "outside.jsonl"
        write(outside, paste(png(1)))
        link = home / ".claude" / "projects" / "-repo" / f"{SID}.jsonl"
        os.link(outside, link)

        assert call(payload(repo, link)) == (200, "")
        assert inbox_count(slug) == 0

    def test_a_file_that_is_not_a_transcript_is_refused(self, project, home):
        slug, repo = project
        other = home / ".claude" / "projects" / "-repo" / "notes.txt"
        write(other, paste(png(1)))

        assert call(payload(repo, other)) == (200, "")
        assert inbox_count(slug) == 0

    def test_a_subagents_own_transcript_is_refused(self, project, home):
        slug, repo = project
        sub = home / ".claude" / "projects" / "-repo" / SID / "subagents" / "agent-1.jsonl"
        sub.parent.mkdir(parents=True)
        write(sub, paste(png(1)))

        assert call(payload(repo, sub)) == (200, "")
        assert inbox_count(slug) == 0

    def test_a_model_screenshot_in_a_real_transcript_is_never_parked(self, project, transcript):
        slug, repo = project
        write(transcript, screenshot_result(png(1)))

        assert call(payload(repo, transcript)) == (200, "")
        assert inbox_count(slug) == 0 and store_is_empty(slug)


# ---------- what the session is told, and when ----------


class TestDelivery:
    def test_one_task_in_progress_is_named_and_the_session_told_to_ask(
        self, project, transcript,
    ):
        slug, repo = project
        session = container.session_service.start(slug)
        task = container.task_service.create(CreateTaskRequest(project=slug, title="Fix it"))
        container.task_service.start(slug, task.id, session_id=session.session_id)
        write(transcript, paste(png(1)))

        _, body = call(payload(repo, transcript))

        assert "Ask the user whether it belongs on" in body and task.id in body
        assert container.task_service.attachments(slug, task.id) == []

    def test_a_notice_is_delivered_once_and_never_by_stop(self, project, transcript):
        _, repo = project
        write(transcript, paste(png(1)))

        _, at_stop = call(payload(repo, transcript, event="Stop"))
        _, again_at_stop = call(payload(repo, transcript, event="Stop"))
        _, prompt = call(payload(repo, transcript))
        _, next_prompt = call(payload(repo, transcript))

        assert at_stop and again_at_stop == at_stop
        assert prompt == at_stop
        assert next_prompt == ""

    def test_the_paste_found_only_at_stop_is_told_at_the_next_prompt(
        self, project, transcript,
    ):
        """P4: at UserPromptSubmit the transcript may not hold the prompt yet."""
        slug, repo = project
        write(transcript, paste(png(0), text="an earlier turn"))
        call(payload(repo, transcript))                     # the paste is not written yet
        call(payload(repo, transcript))                     # (its notice is delivered)

        write(transcript, paste(png(1)), mode="ab")         # the turn completes
        call(payload(repo, transcript, event="Stop"))
        _, next_prompt = call(payload(repo, transcript))

        assert next_prompt.count("The user attached") == 1
        assert inbox_count(slug) == 2


# ---------- the scripts ----------


@contextmanager
def _daemon(answers: dict[str, str]):
    """A stub daemon answering per path and recording every request."""
    seen: list[dict] = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _answer(self, body=b""):
            path = self.path.split("?", 1)[0]
            seen.append({"method": self.command, "path": path, "body": body.decode()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(answers.get(path, "").encode())

        def do_GET(self):
            self._answer()

        def do_POST(self):
            self._answer(self.rfile.read(int(self.headers.get("Content-Length") or 0)))

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run(script: Path, hook_input: dict, port: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": str(port),
           "MEMORY_MCP_BIN": "/nonexistent/memory-mcp"}
    env.pop("MEMORY_MCP_URL", None)
    env.pop("MEMORY_MCP_TOKEN", None)
    return subprocess.run(["bash", str(script)], input=json.dumps(hook_input), text=True,
                          capture_output=True, timeout=30, env=env)


HOOK_INPUT = {
    "session_id": SID, "cwd": "/repo", "transcript_path": f"/t/{SID}.jsonl",
    "hook_event_name": "UserPromptSubmit", "prompt": "hello",
}


@pytest.mark.skipif(not INJECT.exists(), reason="hook script not present")
class TestInjectRulesScript:
    def test_it_prints_the_notices_after_the_rules(self):
        answers = {"/api/hook/rules": "RULES BLOCK",
                   "/api/hook/attachments": "[Memory MCP] The user attached x.png"}
        with _daemon(answers) as (port, seen):
            result = _run(INJECT, HOOK_INPUT, port)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "RULES BLOCK\n[Memory MCP] The user attached x.png\n"
        assert [s["path"] for s in seen] == ["/api/hook/rules", "/api/hook/attachments"]
        assert seen[1]["method"] == "POST"
        assert json.loads(seen[1]["body"]) == {
            "session_id": SID, "cwd": "/repo", "transcript_path": f"/t/{SID}.jsonl",
            "agent_id": "", "agent_type": "", "event": "UserPromptSubmit",
        }

    def test_nothing_is_printed_when_there_is_nothing_to_tell(self):
        with _daemon({"/api/hook/rules": "RULES BLOCK"}) as (port, _):
            result = _run(INJECT, HOOK_INPUT, port)

        assert result.stdout == "RULES BLOCK"

    def test_without_a_transcript_it_makes_one_call(self):
        with _daemon({"/api/hook/rules": "RULES"}) as (port, seen):
            result = _run(INJECT, {**HOOK_INPUT, "transcript_path": ""}, port)

        assert result.returncode == 0
        assert [s["path"] for s in seen] == ["/api/hook/rules"]

    def test_an_unreachable_daemon_exits_0_silently(self):
        env = {**os.environ, "MEMORY_MCP_DAEMON_PORT": "9"}
        result = subprocess.run(["bash", str(INJECT)], input=json.dumps(HOOK_INPUT),
                                text=True, capture_output=True, timeout=30, env=env)

        assert result.returncode == 0
        assert result.stdout == ""

    def test_the_payload_is_data_not_shell(self, tmp_path):
        canary = tmp_path / "canary"
        hostile = {**HOOK_INPUT, "transcript_path": f"/t/$(touch {canary}).jsonl",
                   "session_id": f"`touch {canary}`"}
        with _daemon({}) as (port, seen):
            result = _run(INJECT, hostile, port)

        assert result.returncode == 0
        assert not canary.exists()
        assert json.loads(seen[-1]["body"])["transcript_path"] == hostile["transcript_path"]


@pytest.mark.skipif(not SESSION_END.exists(), reason="hook script not present")
class TestSessionEndScript:
    def test_it_posts_the_stop_event_and_prints_none_of_the_answer(self):
        answers = {"/api/hook/rules": "END NOTE",
                   "/api/hook/attachments": "[Memory MCP] The user attached x.png"}
        with _daemon(answers) as (port, seen):
            result = _run(SESSION_END, {**HOOK_INPUT, "hook_event_name": "Stop"}, port)

        assert result.returncode == 0, result.stderr
        posts = [s for s in seen if s["path"] == "/api/hook/attachments"]
        assert len(posts) == 1
        assert json.loads(posts[0]["body"])["event"] == "Stop"
        assert "The user attached" not in result.stdout

    def test_without_a_transcript_it_does_not_scan(self):
        with _daemon({}) as (port, seen):
            _run(SESSION_END, {**HOOK_INPUT, "transcript_path": ""}, port)

        assert "/api/hook/attachments" not in [s["path"] for s in seen]
