"""Trello held to the same contract as asoode.

This is the test the whole abstraction was for. Trello differs from asoode in
both ways the capability flags exist to describe - the list IS the state, and
there is no idempotency key - so if the conformance suite passes here without
special cases, the interface is carrying its weight.

The fake is an httpx MockTransport speaking Trello's actual REST shapes, not a
stub of the provider's own methods: URL construction, query auth and param names
are all under test, which is where a real integration usually breaks.
"""

import json

import httpx
import pytest

from memory_mcp.providers import ProviderAuthError, ProviderError
from memory_mcp.providers.trello import TrelloProvider
from tests.providers.conformance import ProviderConformance


class FakeTrello:
    """Trello's REST API, in dictionaries."""

    def __init__(self, require_auth=True):
        self.require_auth = require_auth
        self.orgs, self.boards, self.lists, self.cards = {}, {}, {}, {}
        self.comments: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str]] = []
        self.attachments: list[tuple[str, str]] = []
        self._n = 0

    def _next(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if self.require_auth and not (params.get("key") and params.get("token")):
            return httpx.Response(401, text="unauthorized")
        path = request.url.path.replace("/1", "", 1)
        method = request.method
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {}
        return self._route(method, path, params, body)

    def _route(self, method, path, params, body):
        parts = [p for p in path.split("/") if p]

        if method == "GET" and path == "/members/me/organizations":
            return httpx.Response(200, json=[
                {"id": i, "displayName": t} for i, t in self.orgs.items()])
        if method == "POST" and path == "/organizations":
            oid = self._next("org")
            self.orgs[oid] = params.get("displayName") or ""
            return httpx.Response(200, json={"id": oid, "displayName": self.orgs[oid]})

        if method == "GET" and path == "/members/me/boards":
            return httpx.Response(200, json=list(self.boards.values()))
        if method == "GET" and len(parts) == 3 and parts[0] == "organizations" and parts[2] == "boards":
            return httpx.Response(200, json=[
                b for b in self.boards.values() if b["idOrganization"] == parts[1]])

        if method == "POST" and path == "/boards":
            bid = self._next("board")
            self.boards[bid] = {
                "id": bid, "name": params.get("name") or "",
                "idOrganization": params.get("idOrganization"),
                "url": f"https://trello.com/b/{bid}",
            }
            if params.get("defaultLists") == "true":
                for title in ("To Do", "Doing", "Done"):
                    lid = self._next("list")
                    self.lists[lid] = {"id": lid, "name": title, "idBoard": bid}
            return httpx.Response(200, json=self.boards[bid])

        if method == "GET" and len(parts) == 2 and parts[0] == "boards":
            board = self.boards.get(parts[1])
            return httpx.Response(200, json=board) if board else httpx.Response(404)
        if method == "GET" and len(parts) == 3 and parts[0] == "boards" and parts[2] == "lists":
            return httpx.Response(200, json=[
                l for l in self.lists.values() if l["idBoard"] == parts[1]])
        if method == "GET" and len(parts) == 3 and parts[0] == "boards" and parts[2] == "cards":
            return httpx.Response(200, json=[
                c for c in self.cards.values()
                if self.lists.get(c["idList"], {}).get("idBoard") == parts[1]])

        if method == "POST" and path == "/cards":
            list_id = params.get("idList")
            if list_id not in self.lists:
                return httpx.Response(404)
            cid = self._next("card")
            self.cards[cid] = {
                "id": cid, "name": params.get("name") or "",
                "desc": params.get("desc") or "", "idList": list_id,
                "idBoard": self.lists[list_id]["idBoard"],
            }
            return httpx.Response(200, json=self.cards[cid])

        if len(parts) == 2 and parts[0] == "cards":
            card = self.cards.get(parts[1])
            if card is None:
                return httpx.Response(404)
            if method == "GET":
                return httpx.Response(200, json=card)
            if method == "PUT":
                if params.get("idList"):
                    card["idList"] = params["idList"]
                    self.moves.append((card["id"], params["idList"]))
                return httpx.Response(200, json=card)
        if method == "POST" and len(parts) == 3 and parts[0] == "cards" and parts[2] == "attachments":
            if parts[1] not in self.cards:
                return httpx.Response(404)
            self.attachments.append((parts[1], params.get("name") or ""))
            return httpx.Response(200, json={"id": self._next("att")})
        if method == "POST" and len(parts) == 4 and parts[0] == "cards" and parts[3] == "comments":
            if parts[1] not in self.cards:
                return httpx.Response(404)
            self.comments.append((parts[1], params.get("text") or ""))
            return httpx.Response(200, json={"id": self._next("comment")})

        return httpx.Response(404, text=f"unrouted {method} {path}")


@pytest.fixture
def fake():
    return FakeTrello()


def _wired(fake, monkeypatch) -> TrelloProvider:
    """A provider whose httpx calls land in the fake Trello."""
    transport = fake.transport()
    real_init = httpx.Client.__init__

    def patched(self, *a, **kw):
        kw["transport"] = transport
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.Client, "__init__", patched)
    return TrelloProvider(key="k", token="t")


@pytest.fixture
def provider(fake, monkeypatch):
    return _wired(fake, monkeypatch)


class TestTrelloConformance(ProviderConformance):
    """The same contract asoode passes, on a platform shaped differently."""

    # Declared on the class because ProviderConformance defines a `provider`
    # fixture of its own, and a class-level one shadows the module's.
    @pytest.fixture
    def provider(self, fake, monkeypatch):
        return _wired(fake, monkeypatch)

    @pytest.fixture
    def container(self, provider):
        space = provider.create_space("Conformance Space")
        return provider.create_container("Conformance", space_id=space.id)


class TestTheDifferencesTheFlagsDescribe:
    def test_the_list_is_the_state(self, provider, fake):
        """No status field: 'done' means the card sits in the Done list."""
        space = provider.create_space("S")
        board = provider.create_container("B", space_id=space.id)
        task = provider.create_task(board.id, board.groups[0].id, "Move me")

        provider.set_state(task.id, "done")

        done_list = next(g for g in board.groups if g.title == "Done")
        assert fake.cards[task.id]["idList"] == done_list.id
        assert fake.moves, "set_state must MOVE the card"

    def test_state_is_read_back_from_the_list_name(self, provider):
        space = provider.create_space("S")
        board = provider.create_container("B", space_id=space.id)
        task = provider.create_task(board.id, board.groups[0].id, "Read me")
        provider.set_state(task.id, "in_progress")

        fetched = provider.fetch_container(board.id, with_tasks=True)
        assert next(t for t in fetched.tasks if t.id == task.id).state == "in_progress"

    def test_capabilities_say_the_state_is_not_independent(self, provider):
        assert provider.capabilities.supports_independent_state is False

    def test_a_state_with_no_list_still_lands_somewhere_visible(self, provider, fake):
        """A blocked task belongs on the board, not rejected - Trello simply has
        nowhere specific to put it, and the flags say so."""
        space = provider.create_space("S")
        board = provider.create_container("B", space_id=space.id)
        task = provider.create_task(board.id, board.groups[0].id, "Blocked work")

        provider.set_state(task.id, "blocked")          # no Blocked list exists
        assert fake.cards[task.id]["idList"] in {g.id for g in board.groups}

    def test_only_round_tripping_states_are_declared(self, provider):
        """Declaring nine would be a lie: a board's states are its lists."""
        assert set(provider.capabilities.states) == {"todo", "in_progress", "done"}

    def test_garbage_states_are_still_rejected(self, provider):
        space = provider.create_space("S")
        board = provider.create_container("B", space_id=space.id)
        task = provider.create_task(board.id, board.groups[0].id, "X")
        with pytest.raises(ProviderError, match="unknown task state"):
            provider.set_state(task.id, "nearly-done")

    def test_there_is_no_idempotency_key(self, provider):
        """So the flusher must rely on task_sync's stored remote id - which is
        exactly why that mapping is stored rather than derived."""
        assert provider.capabilities.supports_external_ref is False
        assert provider.find_container("anything") is None

    def test_a_repeated_create_really_would_duplicate(self, provider):
        """Stated as a test so nobody 'optimises' the stored-id lookup away."""
        space = provider.create_space("S")
        board = provider.create_container("B", space_id=space.id)
        first = provider.create_task(board.id, board.groups[0].id, "Same", external_ref="r1")
        second = provider.create_task(board.id, board.groups[0].id, "Same", external_ref="r1")
        assert first.id != second.id

    def test_time_tracking_is_declared_absent_and_refuses(self, provider):
        from datetime import datetime, timezone

        assert provider.capabilities.supports_time_tracking is False
        with pytest.raises(ProviderError, match="no native time tracking"):
            provider.log_time("card", datetime.now(timezone.utc))


class TestTransport:
    def test_auth_rides_the_query_string(self, fake, monkeypatch):
        """Key AND token, which is why credentials key on (provider, account)
        rather than on a server URL."""
        seen = {}
        real_init = httpx.Client.__init__

        def handler(request):
            seen.update(dict(request.url.params))
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(httpx.Client, "__init__",
                            lambda self, *a, **kw: real_init(self, *a, **{**kw, "transport": transport}))
        TrelloProvider(key="my-key", token="my-token").list_spaces()
        assert seen["key"] == "my-key" and seen["token"] == "my-token"

    def test_a_missing_credential_says_where_to_get_one(self, monkeypatch):
        monkeypatch.setattr(
            "memory_mcp.providers.credentials.get_credential", lambda *a, **k: None)
        with pytest.raises(ProviderAuthError, match="trello.com/app-key"):
            TrelloProvider().list_spaces()

    def test_a_half_configured_credential_is_refused(self, monkeypatch):
        """key with no token would look configured and fail at the first call."""
        monkeypatch.setattr(
            "memory_mcp.providers.credentials.get_credential", lambda *a, **k: "just-a-key")
        with pytest.raises(ProviderAuthError):
            TrelloProvider().list_spaces()

    def test_401_is_an_auth_error_not_a_generic_one(self, monkeypatch):
        fake = FakeTrello(require_auth=True)
        transport = fake.transport()
        real_init = httpx.Client.__init__
        monkeypatch.setattr(httpx.Client, "__init__",
                            lambda self, *a, **kw: real_init(self, *a, **{**kw, "transport": transport}))
        with pytest.raises(ProviderAuthError):
            TrelloProvider(key="", token="").list_spaces()

    def test_unreachable_names_the_platform(self, monkeypatch):
        def boom(request):
            raise httpx.ConnectError("refused")

        transport = httpx.MockTransport(boom)
        real_init = httpx.Client.__init__
        monkeypatch.setattr(httpx.Client, "__init__",
                            lambda self, *a, **kw: real_init(self, *a, **{**kw, "transport": transport}))
        with pytest.raises(ProviderError, match="Trello unreachable"):
            TrelloProvider(key="k", token="t").list_spaces()
