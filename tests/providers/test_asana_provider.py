"""Asana held to the same contract.

The third provider, and the one that tests a different mismatch again: Asana's
state is a BOOLEAN plus a section, so neither "the status field" (asoode) nor
"the list is the state" (Trello) describes it. If the interface survives all
three without special cases in shared code, the shape is right.

The fake speaks Asana's REST shapes including its {"data": ...} envelope, so the
unwrapping is under test rather than assumed.
"""

import json

import httpx
import pytest

from memory_mcp.providers import ProviderAuthError, ProviderError
from memory_mcp.providers.asana import AsanaProvider
from tests.providers.conformance import ProviderConformance


class FakeAsana:
    def __init__(self):
        self.workspaces = {"w1": "Acme"}
        self.projects, self.sections, self.tasks = {}, {}, {}
        self.stories: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str]] = []
        self.attachments: list[tuple[str, str]] = []
        self._n = 0

    def _next(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"errors": [{"message": "no token"}]})
        path = request.url.path.replace("/api/1.0", "", 1)
        body = {}
        if request.content:
            try:
                body = json.loads(request.content).get("data", {})
            except ValueError:
                body = {}
        return self._route(request.method, path, dict(request.url.params), body)

    def _wrap(self, data, status=200):
        return httpx.Response(status, json={"data": data})

    def _route(self, method, path, params, body):
        parts = [p for p in path.split("/") if p]

        if method == "POST" and path == "/attachments":
            parent = params.get("parent")
            if parent not in self.tasks:
                return httpx.Response(404, json={"errors": [{"message": "no task"}]})
            self.attachments.append((parent, "upload"))
            return self._wrap({"gid": self._next("att")})

        if method == "GET" and path == "/workspaces":
            return self._wrap([{"gid": g, "name": n} for g, n in self.workspaces.items()])

        if method == "GET" and path == "/projects":
            rows = [p for p in self.projects.values()
                    if not params.get("workspace")
                    or p["workspace"]["gid"] == params["workspace"]]
            return self._wrap(rows)
        if method == "POST" and path == "/projects":
            gid = self._next("proj")
            self.projects[gid] = {
                "gid": gid, "name": body.get("name") or "",
                "notes": body.get("notes") or "",
                "workspace": {"gid": body.get("workspace")},
                "permalink_url": f"https://app.asana.com/0/{gid}",
            }
            for title in ("To Do", "In Progress", "Done"):
                sid = self._next("sect")
                self.sections[sid] = {"gid": sid, "name": title, "project": gid}
            return self._wrap(self.projects[gid])

        if method == "GET" and len(parts) == 2 and parts[0] == "projects":
            project = self.projects.get(parts[1])
            return self._wrap(project) if project else httpx.Response(404)
        if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "sections":
            return self._wrap([s for s in self.sections.values() if s["project"] == parts[1]])
        if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "tasks":
            return self._wrap([
                self._task_view(t) for t in self.tasks.values()
                if parts[1] in t["projects"]
            ])

        if method == "POST" and path == "/tasks":
            wanted = list(body.get("projects") or [])
            if any(g not in self.projects for g in wanted):
                return httpx.Response(400, json={"errors": [{"message": "no such project"}]})
            gid = self._next("task")
            memberships = body.get("memberships") or []
            section = memberships[0].get("section") if memberships else None
            self.tasks[gid] = {
                "gid": gid, "name": body.get("name") or "",
                "notes": body.get("notes") or "", "completed": False,
                "projects": list(body.get("projects") or []), "section": section,
            }
            return self._wrap(self.tasks[gid])

        if len(parts) == 2 and parts[0] == "tasks":
            task = self.tasks.get(parts[1])
            if task is None:
                return httpx.Response(404)
            if method == "GET":
                return self._wrap({
                    **self._task_view(task),
                    "projects": [{"gid": p} for p in task["projects"]],
                })
            if method == "PUT":
                if "completed" in body:
                    task["completed"] = bool(body["completed"])
                return self._wrap(self._task_view(task))
        if method == "POST" and len(parts) == 3 and parts[0] == "tasks" and parts[2] == "stories":
            if parts[1] not in self.tasks:
                return httpx.Response(404)
            self.stories.append((parts[1], body.get("text") or ""))
            return self._wrap({"gid": self._next("story")})

        if method == "POST" and len(parts) == 3 and parts[0] == "sections" and parts[2] == "addTask":
            task = self.tasks.get(body.get("task"))
            if task is None:
                return httpx.Response(404)
            task["section"] = parts[1]
            self.moves.append((task["gid"], parts[1]))
            return self._wrap({})

        return httpx.Response(404, text=f"unrouted {method} {path}")

    def _task_view(self, task):
        section = self.sections.get(task.get("section") or "")
        return {
            "gid": task["gid"], "name": task["name"], "notes": task["notes"],
            "completed": task["completed"],
            "memberships": [{"section": {"gid": section["gid"], "name": section["name"]}}]
            if section else [],
        }


def _wired(fake, monkeypatch) -> AsanaProvider:
    transport = fake.transport()
    real_init = httpx.Client.__init__
    monkeypatch.setattr(
        httpx.Client, "__init__",
        lambda self, *a, **kw: real_init(self, *a, **{**kw, "transport": transport}))
    return AsanaProvider(token="pat")


@pytest.fixture
def fake():
    return FakeAsana()


@pytest.fixture
def provider(fake, monkeypatch):
    return _wired(fake, monkeypatch)


class TestAsanaConformance(ProviderConformance):
    @pytest.fixture
    def provider(self, fake, monkeypatch):
        return _wired(fake, monkeypatch)

    @pytest.fixture
    def container(self, provider):
        space = provider.create_space("Acme")
        return provider.create_container("Conformance", space_id=space.id)


class TestStateIsAFlagPlusASection:
    """Neither asoode's status field nor Trello's list-is-the-state describes
    Asana, which is why it is worth having as the third provider."""

    @pytest.fixture
    def board(self, provider):
        space = provider.create_space("Acme")
        return provider.create_container("P", space_id=space.id)

    def test_done_writes_both_the_flag_and_the_section(self, provider, board, fake):
        task = provider.create_task(board.id, board.groups[0].id, "Finish me")
        provider.set_state(task.id, "done")

        assert fake.tasks[task.id]["completed"] is True, "the flag must be set"
        done = next(g for g in board.groups if g.title == "Done")
        assert fake.tasks[task.id]["section"] == done.id, "and the section moved"

    def test_a_non_done_state_clears_the_flag(self, provider, board, fake):
        task = provider.create_task(board.id, board.groups[0].id, "Reopen me")
        provider.set_state(task.id, "done")
        provider.set_state(task.id, "in_progress")
        assert fake.tasks[task.id]["completed"] is False

    def test_the_completed_flag_wins_on_read(self, provider, board, fake):
        """A task marked complete is complete whatever section it was left in -
        otherwise a done task parked in Blocked would read back as blocked."""
        task = provider.create_task(board.id, board.groups[0].id, "Odd one")
        fake.tasks[task.id]["completed"] = True          # flag set, section not moved
        fetched = provider.fetch_container(board.id, with_tasks=True)
        assert next(t for t in fetched.tasks if t.id == task.id).state == "done"

    def test_state_round_trips_through_the_section(self, provider, board):
        task = provider.create_task(board.id, board.groups[0].id, "Working")
        provider.set_state(task.id, "in_progress")
        fetched = provider.fetch_container(board.id, with_tasks=True)
        assert next(t for t in fetched.tasks if t.id == task.id).state == "in_progress"

    def test_a_state_with_no_section_still_lands_somewhere(self, provider, board, fake):
        task = provider.create_task(board.id, board.groups[0].id, "Blocked work")
        provider.set_state(task.id, "blocked")          # no Blocked section exists
        assert fake.tasks[task.id]["section"] in {g.id for g in board.groups}

    def test_garbage_states_are_rejected(self, provider, board):
        task = provider.create_task(board.id, board.groups[0].id, "X")
        with pytest.raises(ProviderError, match="unknown task state"):
            provider.set_state(task.id, "sort-of-done")


class TestAsanaSpecifics:
    def test_the_data_envelope_never_escapes(self, provider):
        """Every Asana response is {"data": ...}; unwrapped once, in _request."""
        spaces = provider.list_spaces()
        assert spaces and spaces[0].title == "Acme"
        assert not hasattr(spaces[0], "data")

    def test_a_workspace_cannot_be_created_so_an_existing_one_is_returned(self, provider):
        """The interface promises a space is always available. A workspace is an
        organisational fact the user sets up once, not something a task mirror
        should invent - and the API cannot create one anyway."""
        space = provider.create_space("Does Not Exist Yet")
        assert space.title == "Acme", "falls back to a real workspace"

    def test_no_workspace_at_all_is_an_honest_error(self, fake, monkeypatch):
        fake.workspaces = {}
        provider = _wired(fake, monkeypatch)
        with pytest.raises(ProviderError, match="no workspace"):
            provider.create_space("Anything")

    def test_there_is_no_idempotency_key(self, provider):
        assert provider.capabilities.supports_external_ref is False
        assert provider.find_container("anything") is None

    def test_time_tracking_is_declared_absent(self, provider):
        """actual_time_minutes is paid-tier and not writable here - claiming it
        would make the flusher send calls that silently do nothing."""
        assert provider.capabilities.supports_time_tracking is False

    def test_a_comment_becomes_a_story(self, provider, fake):
        space = provider.create_space("Acme")
        board = provider.create_container("P", space_id=space.id)
        task = provider.create_task(board.id, board.groups[0].id, "X")
        provider.comment(task.id, "a note")
        assert fake.stories == [(task.id, "a note")]


class TestTransport:
    def test_the_token_rides_as_a_bearer_header(self, fake, monkeypatch):
        seen = {}
        real_init = httpx.Client.__init__

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"data": []})

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            httpx.Client, "__init__",
            lambda self, *a, **kw: real_init(self, *a, **{**kw, "transport": transport}))
        AsanaProvider(token="my-pat").list_spaces()
        assert seen["auth"] == "Bearer my-pat"

    def test_a_missing_credential_says_where_to_get_one(self, monkeypatch):
        monkeypatch.setattr(
            "memory_mcp.providers.credentials.get_credential", lambda *a, **k: None)
        with pytest.raises(ProviderAuthError, match="my-apps"):
            AsanaProvider().list_spaces()

    def test_401_is_an_auth_error(self, monkeypatch):
        transport = httpx.MockTransport(lambda r: httpx.Response(401, json={}))
        real_init = httpx.Client.__init__
        monkeypatch.setattr(
            httpx.Client, "__init__",
            lambda self, *a, **kw: real_init(self, *a, **{**kw, "transport": transport}))
        with pytest.raises(ProviderAuthError):
            AsanaProvider(token="bad").list_spaces()

    def test_unreachable_names_the_platform(self, monkeypatch):
        def boom(request):
            raise httpx.ConnectError("refused")

        transport = httpx.MockTransport(boom)
        real_init = httpx.Client.__init__
        monkeypatch.setattr(
            httpx.Client, "__init__",
            lambda self, *a, **kw: real_init(self, *a, **{**kw, "transport": transport}))
        with pytest.raises(ProviderError, match="Asana unreachable"):
            AsanaProvider(token="t").list_spaces()
