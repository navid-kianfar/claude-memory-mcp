"""Trello as a TaskProvider - the second implementation, and the one that tests
whether the interface actually abstracts anything.

Trello differs from asoode in the two ways the capability flags exist for:

1. THE LIST *IS* THE STATE. A card has no status field; "done" means the card
   sits in the Done list. So `supports_independent_state=False`, and `set_state`
   MOVES the card - callers never learn the difference. Reading works the same
   way round: a card's state is derived from the name of the list holding it.

2. THERE IS NO IDEMPOTENCY KEY. Trello has no externalRef equivalent, so
   `supports_external_ref=False` and a repeated create WOULD duplicate. Identity
   therefore comes from task_sync's stored remote id, which the flusher already
   consults before creating - that is exactly why the mapping is stored rather
   than derived.

It also has no time tracking (Power-Ups aside), so `supports_time_tracking` is
False and the flusher keeps those entries unsent rather than losing them.

Auth is a key AND a token, both on the query string - which is why credentials
are keyed by (provider, account) rather than by a server URL.

STATE VOCABULARY: `capabilities.states` reports only the states that ROUND-TRIP
on a conventional board (todo/in_progress/done), because a Trello board's states
are whatever lists it happens to have. `set_state` still accepts every local
state and maps the rest onto the nearest list - a blocked task belongs somewhere
visible, not rejected - but it will read back as that list's state, which is
honest rather than pretending Trello stores something it does not.
"""

import httpx

from memory_mcp.models import TaskState
from memory_mcp.providers.base import (
    Capabilities,
    Container,
    ContainerRef,
    Group,
    ProviderAuthError,
    ProviderError,
    RemoteTask,
    SpaceRef,
)

API = "https://api.trello.com/1"

# List titles that mean a given state, best match first. A board that uses none
# of these still works: everything falls back to the first list.
_LIST_ALIASES = {
    "todo": ("to do", "todo", "backlog", "inbox", "new"),
    "in_progress": ("in progress", "doing", "wip", "started"),
    "done": ("done", "complete", "completed", "finished", "shipped"),
    "paused": ("paused", "on hold", "hold"),
    "blocked": ("blocked", "impediment", "stuck"),
    "cancelled": ("cancelled", "canceled", "dropped", "won't do", "wontfix"),
    "duplicate": ("duplicate",),
    "incomplete": ("incomplete",),
    "blocker": ("blocker",),
}

# What survives a write-then-read on a conventional board. Anything outside this
# is still settable, but reads back as whichever list it landed in.
_ROUND_TRIP_STATES = ("todo", "in_progress", "done")

_CAPABILITIES = Capabilities(
    # No externalRef: identity comes from the stored remote id in task_sync.
    supports_external_ref=False,
    supports_comments=True,
    supports_groups=True,
    # The list IS the state - set_state moves the card.
    supports_independent_state=False,
    supports_time_tracking=False,
    # POST /1/cards/{id}/attachments, multipart
    supports_attachments=True,
    states=_ROUND_TRIP_STATES,
)


class TrelloProvider:
    def __init__(self, key: str | None = None, token: str | None = None,
                 timeout: float = 20.0):
        self._key = key
        self._token = token
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "trello"

    @property
    def capabilities(self) -> Capabilities:
        return _CAPABILITIES

    # ---------- transport ----------

    def _auth(self) -> tuple[str, str]:
        """Trello wants a key AND a token. Stored as "key:token" under one entry,
        because they are useless apart and asking for them separately would let a
        half-configured install look configured."""
        if self._key and self._token:
            return self._key, self._token
        from memory_mcp.providers.credentials import get_credential

        raw = get_credential("trello") or ""
        key, _, token = raw.partition(":")
        if not key or not token:
            raise ProviderAuthError(
                "No Trello credential stored. It is an API key and a token together, "
                'as "key:token" - get both from https://trello.com/app-key.'
            )
        self._key, self._token = key, token
        return key, token

    def _request(self, method: str, path: str, *, params=None, json=None):
        key, token = self._auth()
        query = {"key": key, "token": token, **(params or {})}
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.request(method, API + path, params=query, json=json)
        except httpx.HTTPError as e:
            raise ProviderError(f"Trello unreachable: {e}") from e

        if resp.status_code in (401, 403):
            raise ProviderAuthError(
                f"Trello rejected the credential ({resp.status_code}) on {path}. "
                "The token may be revoked or lack the needed scope."
            )
        if resp.status_code == 404:
            raise ProviderError(f"Trello 404 on {path} - no such object")
        if resp.status_code >= 400:
            raise ProviderError(f"Trello {resp.status_code} on {path}: {resp.text[:200]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderError(f"Trello returned non-JSON on {path}") from e

    # ---------- spaces (organizations) ----------

    def list_spaces(self) -> list[SpaceRef]:
        orgs = self._request("GET", "/members/me/organizations") or []
        return [
            SpaceRef(id=o["id"], title=o.get("displayName") or o.get("name") or "")
            for o in orgs if o.get("id")
        ]

    def find_space(self, title: str) -> SpaceRef | None:
        wanted = (title or "").strip().lower()
        if not wanted:
            return None
        for space in self.list_spaces():
            if space.title.strip().lower() == wanted:
                return space
        return None

    def create_space(self, title: str, *, description: str = "") -> SpaceRef:
        existing = self.find_space(title)
        if existing is not None:
            return existing
        created = self._request("POST", "/organizations", params={
            "displayName": title, "desc": description,
        })
        if not created or not created.get("id"):
            raise ProviderError("Trello returned an organization without an id")
        return SpaceRef(id=created["id"], title=created.get("displayName") or title)

    # ---------- containers (boards) ----------

    def list_containers(self, space_id: str | None = None) -> list[ContainerRef]:
        path = f"/organizations/{space_id}/boards" if space_id else "/members/me/boards"
        boards = self._request("GET", path, params={"filter": "open"}) or []
        return [
            ContainerRef(id=b["id"], title=b.get("name") or "",
                         external_ref=None, space_id=b.get("idOrganization"))
            for b in boards if b.get("id")
        ]

    def find_container(self, external_ref: str) -> ContainerRef | None:
        """Trello has no caller-supplied ref, so there is nothing to find by.

        Returns None rather than raising: the bridge's attach path tries this
        first and falls through to a work-package id, which for Trello is the
        board id and is the only way to identify one.
        """
        return None

    def fetch_container(self, container_id: str, *, with_tasks: bool = False) -> Container:
        board = self._request("GET", f"/boards/{container_id}")
        if not board:
            raise ProviderError(f"no Trello board {container_id}")
        lists = self._request("GET", f"/boards/{container_id}/lists",
                              params={"filter": "open"}) or []
        groups = tuple(
            Group(id=l["id"], title=l.get("name") or "") for l in lists if l.get("id")
        )
        tasks: tuple[RemoteTask, ...] = ()
        if with_tasks:
            cards = self._request("GET", f"/boards/{container_id}/cards",
                                  params={"filter": "open"}) or []
            by_id = {g.id: g.title for g in groups}
            tasks = tuple(
                RemoteTask(
                    id=c["id"], title=c.get("name") or "",
                    state=self._state_of(by_id.get(c.get("idList"), "")),
                    description=c.get("desc") or "",
                    group_id=c.get("idList"), external_ref=None,
                )
                for c in cards if c.get("id")
            )
        return Container(
            id=board["id"], title=board.get("name") or "", external_ref=None,
            space_id=board.get("idOrganization"), groups=groups, tasks=tasks,
            url=board.get("url"),
        )

    def create_container(
        self, title: str, *, description: str = "", external_ref: str | None = None,
        space_id: str | None = None,
    ) -> Container:
        params = {"name": title, "desc": description, "defaultLists": "true"}
        if space_id:
            params["idOrganization"] = space_id
        board = self._request("POST", "/boards", params=params)
        if not board or not board.get("id"):
            raise ProviderError("Trello returned a board without an id")
        return self.fetch_container(board["id"])

    # ---------- cards ----------

    def create_task(
        self, container_id: str, group_id: str | None, title: str, *,
        description: str = "", external_ref: str | None = None,
    ) -> RemoteTask:
        list_id = group_id or self._first_group(container_id)
        params = {"idList": list_id, "name": title}
        if description:
            params["desc"] = description
        card = self._request("POST", "/cards", params=params)
        if not card or not card.get("id"):
            raise ProviderError("Trello returned a card without an id")
        return RemoteTask(
            id=card["id"], title=card.get("name") or title,
            state=self._state_of(self._group_title(container_id, list_id)),
            description=card.get("desc") or description,
            group_id=list_id, external_ref=None,
        )

    def set_state(self, task_id: str, state: str) -> None:
        """Move the card, because on Trello the list is the state.

        Every valid local state is accepted, not only the ones that round-trip: a
        blocked task belongs somewhere visible on the board, and refusing would
        make the flusher's job depend on which lists a board happens to have.
        """
        if state not in {s.value for s in TaskState}:
            raise ProviderError(f"unknown task state: {state!r}")
        card = self._request("GET", f"/cards/{task_id}", params={"fields": "idBoard"})
        if not card:
            raise ProviderError(f"no Trello card {task_id}")
        target = self._group_for_state(card["idBoard"], state)
        if target:
            self._request("PUT", f"/cards/{task_id}", params={"idList": target})

    def move(self, task_id: str, group_id: str) -> None:
        self._request("PUT", f"/cards/{task_id}", params={"idList": group_id})

    def comment(self, task_id: str, body: str) -> None:
        self._request("POST", f"/cards/{task_id}/actions/comments",
                      params={"text": body})

    def attach(self, task_id: str, filename: str, content: bytes,
               content_type: str | None = None) -> None:
        key, token = self._auth()
        files = {"file": (filename, content, content_type or "application/octet-stream")}
        try:
            with httpx.Client(timeout=max(self._timeout, 60.0)) as client:
                resp = client.post(
                    f"{API}/cards/{task_id}/attachments",
                    params={"key": key, "token": token, "name": filename}, files=files,
                )
        except httpx.HTTPError as e:
            raise ProviderError(f"Trello unreachable: {e}") from e
        if resp.status_code in (401, 403):
            raise ProviderAuthError(f"Trello rejected the credential on attach")
        if resp.status_code >= 400:
            raise ProviderError(f"Trello {resp.status_code} on attach: {resp.text[:200]}")

    def log_time(self, task_id: str, begin, end=None) -> None:  # pragma: no cover
        raise ProviderError("Trello has no native time tracking")

    # ---------- translation ----------

    def _groups(self, container_id: str) -> list[Group]:
        lists = self._request("GET", f"/boards/{container_id}/lists",
                              params={"filter": "open"}) or []
        return [Group(id=l["id"], title=l.get("name") or "") for l in lists if l.get("id")]

    def _first_group(self, container_id: str) -> str:
        groups = self._groups(container_id)
        if not groups:
            raise ProviderError(f"Trello board {container_id} has no lists")
        return groups[0].id

    def _group_title(self, container_id: str, group_id: str) -> str:
        for group in self._groups(container_id):
            if group.id == group_id:
                return group.title
        return ""

    def _group_for_state(self, container_id: str, state: str) -> str | None:
        """The list a state belongs in, by name, falling back to the first."""
        groups = self._groups(container_id)
        if not groups:
            return None
        by_title = {g.title.strip().lower(): g.id for g in groups}
        for alias in _LIST_ALIASES.get(state, ()):
            if alias in by_title:
                return by_title[alias]
        return groups[0].id

    @staticmethod
    def _state_of(list_title: str) -> str:
        """The state a list name means. Unrecognised lists read as todo, which is
        the honest answer: the board is not saying anything more specific."""
        name = (list_title or "").strip().lower()
        for state, aliases in _LIST_ALIASES.items():
            if name in aliases:
                return state
        return "todo"
