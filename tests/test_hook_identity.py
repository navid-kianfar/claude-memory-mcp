"""What a hook payload says about who is calling, and what we refuse to guess.

Every hook script used to extract `cwd` and throw the rest away. These tests are
about the part of that fix which is easy to get wrong: `agent_id`/`agent_type` are
DOCUMENTED hook fields that were never verified on the installed CLI, so "absent"
must mean "unknown", not "this is the lead". Reading an absent field as the lead
would silently file a subagent's edits under the lead on exactly the builds where
we cannot tell them apart - and that is the input to a message aimed at the lead.

The probe that would settle which fields the CLI really sends has not run. Until
it does, the `agent_id` paths here are exercised by these tests and by nothing on
a real machine.
"""

import asyncio
import json

import pytest

from memory_mcp.container import container
from memory_mcp.db.connection import get_connection
from memory_mcp.db.registry import (
    AGENT_ID_SUPPORTED_KEY, agent_id_supported, client_session, dispatches_for,
    get_setting, set_setting,
)
from memory_mcp.web.hooks import (
    HookIdentity, identity_from_body, identity_from_query,
)

FULL_PAYLOAD = {
    "cwd": "/repo",
    "session_id": "sess-1",
    "agent_id": "agent-9",
    "agent_type": "python",
    "transcript_path": "/t/sess-1.jsonl",
    "tool_name": "Write",
    "tool_input": {"file_path": "/repo/src/x.py", "subagent_type": "python"},
}


class _Query:
    """The slice of a Starlette request the identity parser reads."""

    def __init__(self, params):
        self.query_params = params


def _flat(payload: dict) -> dict:
    """The payload as a hook script forwards it: flat, strings only."""
    flat = {k: v for k, v in payload.items() if k != "tool_input"}
    flat.update(payload.get("tool_input", {}))
    return flat


class TestParsing:
    def test_a_full_payload_keeps_every_field(self):
        identity = identity_from_query(_Query(_flat(FULL_PAYLOAD)))

        assert identity.cwd == "/repo"
        assert identity.session_id == "sess-1"
        assert identity.agent_id == "agent-9"
        assert identity.agent_type == "python"
        assert identity.transcript_path == "/t/sess-1.jsonl"
        assert identity.tool == "Write"
        assert identity.file_path == "/repo/src/x.py"

    def test_an_empty_payload_parses_to_all_empty_strings(self):
        """Not None, and not an exception: a hook with no payload at all is the
        normal case for several events, and every caller does `if identity.x`."""
        identity = identity_from_query(_Query({}))

        assert identity == HookIdentity()
        assert identity.cwd == ""
        assert identity.session_id == ""

    def test_a_body_with_nulls_parses_to_empty_strings(self):
        identity = identity_from_body(
            {"cwd": "/repo", "session_id": None, "agent_id": None}
        )

        assert identity.cwd == "/repo"
        assert identity.session_id == ""
        assert identity.agent_id == ""

    def test_non_string_values_are_dropped_not_coerced(self):
        """The payload is untrusted input from a CLI whose shape varies. A number
        where a path belongs is a field we do not have, not `str(42)`."""
        identity = identity_from_body({"cwd": 42, "session_id": ["s"], "tool_name": {}})

        assert identity == HookIdentity()

    def test_whitespace_is_stripped(self):
        assert identity_from_query(_Query({"session_id": "  s1  "})).session_id == "s1"

    def test_a_missing_body_is_an_identity_not_a_crash(self):
        assert identity_from_body(None) == HookIdentity()


class TestIsSubagent:
    """True / False / None, and None is a real answer."""

    def test_an_agent_id_means_subagent(self):
        assert HookIdentity(agent_id="a1").is_subagent is True

    def test_a_worktree_cwd_means_subagent_whatever_the_build_sends(self):
        """The second witness, and the one that does not depend on the CLI:
        Claude Code puts an isolated agent's checkout under .claude/worktrees/."""
        identity = HookIdentity(cwd="/repo/.claude/worktrees/task-abc")

        assert identity.is_subagent is True

    def test_no_agent_id_on_a_build_that_sends_them_means_the_lead(self):
        set_setting(AGENT_ID_SUPPORTED_KEY, "1")

        assert HookIdentity(cwd="/repo").is_subagent is False

    def test_no_agent_id_on_an_unproven_build_is_UNKNOWN(self):
        """The case the whole design turns on. Before any payload has proved this
        CLI sends agent_id, an absent one tells us nothing - and `None` is what
        stops a caller reading it as 'the lead'."""
        assert get_setting(AGENT_ID_SUPPORTED_KEY) is None
        assert HookIdentity(cwd="/repo").is_subagent is None


class TestByAgent:
    """What lands in the edit ledger's `by_agent` column."""

    def test_the_lead_is_null(self):
        assert HookIdentity(cwd="/repo").by_agent is None

    def test_an_agent_is_its_type(self):
        assert HookIdentity(agent_id="a1", agent_type="python").by_agent == "python"

    def test_an_agent_with_no_type_is_recorded_as_unknown_not_as_the_lead(self):
        """A build that sends agent_id but not agent_type still must not look like
        the lead: the column is read as 'was this the lead?', not as a name."""
        assert HookIdentity(agent_id="a1").by_agent == "unknown"


class TestAgentIdSupported:
    def test_it_starts_unknown(self):
        assert agent_id_supported() is False
        assert get_setting(AGENT_ID_SUPPORTED_KEY) is None

    def test_a_payload_carrying_an_agent_id_flips_it_once_and_it_stays(self):
        identity_from_query(_Query({"cwd": "/repo", "agent_id": "a1"}))
        assert agent_id_supported() is True

        # A later payload without one must not un-learn it: most hook events do
        # not run inside a subagent at all.
        identity_from_query(_Query({"cwd": "/repo"}))
        assert agent_id_supported() is True

    def test_payloads_without_an_agent_id_never_set_it(self):
        identity_from_query(_Query({"cwd": "/repo", "session_id": "s1"}))
        identity_from_body({"cwd": "/repo", "agent_id": ""})

        assert agent_id_supported() is False


@pytest.mark.parametrize(
    "payload",
    [FULL_PAYLOAD, {}, {"cwd": "/repo"}, {"session_id": "s"}],
)
def test_parsing_is_total(payload):
    """No payload shape raises. A hook that 500s is a hook that blocked an edit."""
    flat = _flat(payload)
    assert identity_from_query(_Query(flat)) == identity_from_body(flat)
    # And the round trip through a real JSON body behaves the same.
    assert identity_from_body(json.loads(json.dumps(flat))).cwd == flat.get("cwd", "")


# ---------- the routes that fill the ledgers ----------


class _Body:
    """The slice of a Starlette request a POST hook handler reads."""

    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.cookies = {}
        self.scope = {"type": "http", "headers": []}

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _dispatch(body):
    from memory_mcp.web import hooks

    return json.loads(asyncio.run(hooks._hook_dispatch(_Body(body))).body)


class TestTheDispatchRoute:
    """It records, and it answers `{}` - this release builds the ledger only. The
    deny shape is already honoured by the script, so switching it on later is a
    daemon-side change."""

    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        slug = "t-dispatch"
        container.project_repo.register(slug, slug)
        get_connection(slug).close()
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: slug,
        )
        return slug

    def test_a_dispatch_is_recorded_against_the_project(self, project):
        answer = _dispatch({
            "session_id": "s1", "cwd": "/repo", "subagent_type": "python",
            "description": "Do the thing (python)", "tool_use_id": "t1",
        })

        assert answer == {}
        rows = dispatches_for("s1")
        assert len(rows) == 1
        assert rows[0]["agent_type"] == "python"
        assert rows[0]["slug"] == project
        assert rows[0]["description"] == "Do the thing (python)"
        assert rows[0]["tool_use_id"] == "t1"

    def test_no_subagent_type_records_the_default_agent(self, project):
        _dispatch({"session_id": "s1", "cwd": "/repo"})

        assert dispatches_for("s1")[0]["agent_type"] == "general-purpose"

    def test_the_session_is_remembered_with_its_transcript(self, project):
        _dispatch({
            "session_id": "s1", "cwd": "/repo", "subagent_type": "python",
            "transcript_path": "/t/s1.jsonl",
        })

        assert client_session("s1")["transcript_path"] == "/t/s1.jsonl"

    def test_a_dispatch_outside_any_project_is_still_recorded(self, monkeypatch):
        """The ledger is keyed on the session, not the project: a slug it cannot
        resolve is a NULL column, not a dropped row."""
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: None,
        )
        _dispatch({"session_id": "s1", "cwd": "/elsewhere", "subagent_type": "python"})

        assert dispatches_for("s1")[0]["slug"] is None

    def test_no_session_id_records_nothing_and_still_answers(self, project):
        assert _dispatch({"cwd": "/repo", "subagent_type": "python"}) == {}
        assert dispatches_for("") == []

    def test_an_unreadable_body_answers_empty(self, project):
        from memory_mcp.web import hooks

        request = _Body(ValueError("not json"))
        assert json.loads(asyncio.run(hooks._hook_dispatch(request)).body) == {}

    def test_a_ledger_that_explodes_never_denies_a_dispatch(self, project,
                                                            monkeypatch):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("registry is on fire")

        monkeypatch.setattr("memory_mcp.web.hooks.record_dispatch", _boom)

        assert _dispatch({"session_id": "s1", "cwd": "/repo"}) == {}

    def test_an_agent_id_in_a_dispatch_payload_teaches_the_build(self, project):
        _dispatch({"session_id": "s1", "cwd": "/repo", "agent_id": "a1",
                   "agent_type": "pm", "subagent_type": "python"})

        assert agent_id_supported() is True


class TestAutoRegisterSkipsWorktrees:
    """A worktree carries the same committed uid, so registering it would make a
    duplicate project whose path disappears with the worktree."""

    def _auto_register(self, cwd):
        from memory_mcp.web import hooks

        response = asyncio.run(hooks._hook_auto_register(_Query({"cwd": cwd})))
        return response.body.decode()

    def test_a_git_worktree_is_not_registered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: None,
        )
        tree = tmp_path / "wt-feature"
        tree.mkdir()
        (tree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt-feature\n")

        assert self._auto_register(str(tree)) == ""
        assert container.project_repo.get("wt-feature") is None

    def test_a_claude_worktree_is_not_registered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: None,
        )
        tree = tmp_path / "repo" / ".claude" / "worktrees" / "task-abc"
        tree.mkdir(parents=True)
        (tree / ".git").mkdir()

        assert self._auto_register(str(tree)) == ""
        assert container.project_repo.get("task-abc") is None

    def test_an_ordinary_repo_is_still_registered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "memory_mcp.context.detect_project_from_cwd", lambda cwd: None,
        )
        folder = tmp_path / "ordinary-repo"
        folder.mkdir()
        (folder / ".git").mkdir()

        note = self._auto_register(str(folder))

        assert "ordinary-repo" in note
        assert container.project_repo.get("ordinary-repo") is not None
