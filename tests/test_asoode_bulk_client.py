"""POST /tasks/bulk as the client and the asoode provider put it on the wire.

The contract is asoode's own source (tasks.controller.ts `bulk`, task.dto.ts
BulkTasksBody, tasks.service.ts `bulk`): a selector of explicit ids capped at
500, one action, an optional dryRun, and an answer of id LISTS - matched,
succeeded, failed - plus affectedCount and dryRun. These pin the request shape
and the reading of the answer against an HTTP transport; whether the live
server agrees is the live verification's job, not this file's.
"""

import json

import httpx
import pytest

from memory_mcp.asoode_client import (
    BULK_IDS_MAX, AsoodeClient, AsoodeError, AsoodeNotFoundError,
)
from memory_mcp.providers import ProviderNotFoundError
from memory_mcp.providers.asoode import AsoodeProvider


def _ok(data):
    return httpx.Response(201, json={"status": 2, "data": data})


@pytest.fixture
def wire(monkeypatch):
    """Every request the client sends, answered by `wire.answer(request)`."""

    class Wire:
        def __init__(self):
            self.requests: list[tuple[str, dict]] = []
            self.answer = lambda body: _ok({
                "matched": body["selector"]["ids"],
                "succeeded": body["selector"]["ids"],
                "failed": [], "affectedCount": len(body["selector"]["ids"]),
                "dryRun": bool(body.get("dryRun")),
            })

        def handle(self, request):
            body = json.loads(request.content or b"{}")
            self.requests.append((request.url.path, body))
            return self.answer(body)

    state = Wire()
    transport = httpx.MockTransport(state.handle)
    real_init = httpx.Client.__init__

    def patched(self, *a, **kw):
        kw["transport"] = transport
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.Client, "__init__", patched)
    return state


@pytest.fixture
def client():
    return AsoodeClient("https://api.test", "asoode_pat_test")


class TestTheRequest:
    def test_it_posts_selector_and_action_to_tasks_bulk(self, wire, client):
        client.bulk_tasks({"ids": ["a"]}, {"type": "archive"})

        path, body = wire.requests[0]
        assert path == "/tasks/bulk"
        assert body == {"selector": {"ids": ["a"]}, "action": {"type": "archive"}}

    def test_dry_run_is_sent_only_when_asked(self, wire, client):
        client.bulk_tasks({"ids": ["a"]}, {"type": "remove"}, dry_run=True)
        assert wire.requests[0][1]["dryRun"] is True

    def test_an_unknown_action_never_reaches_the_wire(self, wire, client):
        with pytest.raises(AsoodeError, match="unknown bulk action"):
            client.bulk_tasks({"ids": ["a"]}, {"type": "delete"})
        assert wire.requests == []

    def test_explicit_ids_are_chunked_at_the_server_cap(self, wire, client):
        ids = [f"id-{n}" for n in range(BULK_IDS_MAX + 1)]

        answer = client.bulk_tasks_by_ids(ids, {"type": "archive"})

        sizes = [len(body["selector"]["ids"]) for _, body in wire.requests]
        assert sizes == [BULK_IDS_MAX, 1]
        assert answer["succeeded"] == ids
        assert answer["affectedCount"] == BULK_IDS_MAX + 1

    def test_a_repeated_id_is_sent_once(self, wire, client):
        client.bulk_tasks_by_ids(["a", "a", "b"], {"type": "archive"})
        assert wire.requests[0][1]["selector"]["ids"] == ["a", "b"]


class TestTheAnswer:
    def test_partial_success_is_returned_not_raised(self, wire, client):
        wire.answer = lambda body: _ok({
            "matched": ["a", "b"], "succeeded": ["a", "a-child"], "failed": ["b"],
            "affectedCount": 2, "dryRun": False,
        })

        answer = client.bulk_tasks({"ids": ["a", "b"]}, {"type": "remove"})

        assert answer["failed"] == ["b"]
        assert answer["succeeded"] == ["a", "a-child"]

    def test_missing_lists_read_as_empty(self, wire, client):
        wire.answer = lambda body: _ok({"affectedCount": 0})
        answer = client.bulk_tasks({"ids": ["a"]}, {"type": "archive"})
        assert answer["matched"] == answer["succeeded"] == answer["failed"] == []

    def test_validation_failure_raises(self, wire, client):
        wire.answer = lambda body: httpx.Response(
            201, json={"status": 7, "message": "selector.ids must be UUIDs"},
        )
        with pytest.raises(AsoodeError, match="validation failed"):
            client.bulk_tasks({"ids": ["a"]}, {"type": "archive"})

    def test_not_found_is_the_provider_level_not_found(self, wire, client):
        """A filter selector over a board the token cannot read answers 3."""
        wire.answer = lambda body: httpx.Response(201, json={"status": 3})
        with pytest.raises(AsoodeNotFoundError) as caught:
            client.bulk_tasks({"packageId": "wp"}, {"type": "archive"})
        assert isinstance(caught.value, ProviderNotFoundError)
        assert caught.value.status == 3


class TestTheProviderSpeaksOurVocabulary:
    def test_set_state_sends_the_ordinal(self, wire, client):
        AsoodeProvider(client).bulk(["a"], "set_state", state="blocked")
        assert wire.requests[0][1]["action"] == {"type": "set-state", "state": 5}

    def test_an_unknown_state_is_refused_before_the_wire(self, wire, client):
        with pytest.raises(AsoodeError, match="unknown task state"):
            AsoodeProvider(client).bulk(["a"], "set_state", state="nope")
        assert wire.requests == []

    def test_delete_is_bulk_remove(self, wire, client):
        result = AsoodeProvider(client).delete(["a"])
        assert wire.requests[0][1]["action"] == {"type": "remove"}
        assert result.succeeded == ("a",)

    def test_archive_many_restores_with_unarchive(self, wire, client):
        AsoodeProvider(client).archive_many(["a"], archived=False)
        assert wire.requests[0][1]["action"] == {"type": "unarchive"}

    def test_move_names_board_and_list(self, wire, client):
        AsoodeProvider(client).move_to_board("a", "wp2", "list-9")
        assert wire.requests[0][1]["action"] == {
            "type": "move", "packageId": "wp2", "listId": "list-9",
        }

    def test_a_move_reported_failed_raises(self, wire, client):
        wire.answer = lambda body: _ok({
            "matched": ["a"], "succeeded": [], "failed": ["a"], "affectedCount": 0,
            "dryRun": False,
        })
        with pytest.raises(AsoodeError, match="did not move"):
            AsoodeProvider(client).move_to_board("a", "wp2", "list-9")
