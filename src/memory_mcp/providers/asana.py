"""Asana as a TaskProvider.

Asana's shape maps onto the interface almost directly - workspace -> project ->
section -> task is exactly space -> container -> group -> task - so the
interesting parts are the two places it does NOT line up:

1. STATE IS A BOOLEAN PLUS A SECTION. A task has `completed: true|false`, which
   is independent of which section it sits in. So `supports_independent_state` is
   True, but the independent part only distinguishes done from not-done: the
   other eight local states have to live in sections. `set_state` therefore
   writes BOTH - the flag and the section - and reading prefers the flag, because
   a task marked complete is complete whatever section it was left in.

2. EVERY RESPONSE IS WRAPPED IN {"data": ...}. Unwrapped once, in `_request`, so
   nothing above this file ever sees Asana's envelope.

There is no caller-supplied idempotency key, so `supports_external_ref=False` and
identity comes from task_sync's stored remote id - the same as Trello.

Auth is a personal access token as a Bearer header, and Asana is a single global
endpoint, so the credential needs no account scoping.
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

API = "https://app.asana.com/api/1.0"

# Section names that mean a given state, best match first.
_SECTION_ALIASES = {
    "todo": ("to do", "todo", "backlog", "untitled section", "new"),
    "in_progress": ("in progress", "doing", "wip", "started"),
    "done": ("done", "complete", "completed", "shipped"),
    "paused": ("paused", "on hold", "hold"),
    "blocked": ("blocked", "impediment", "stuck"),
    "cancelled": ("cancelled", "canceled", "dropped"),
    "duplicate": ("duplicate",),
    "incomplete": ("incomplete",),
    "blocker": ("blocker",),
}

# States that survive a write-then-read on a conventional project: `completed`
# carries done, and the two common sections carry the rest.
_ROUND_TRIP_STATES = ("todo", "in_progress", "done")

# Local states that mean the work is finished, so `completed` goes true.
_COMPLETED_STATES = {"done"}

_CAPABILITIES = Capabilities(
    supports_external_ref=False,
    supports_comments=True,
    supports_groups=True,
    # `completed` is a real field, separate from the section.
    supports_independent_state=True,
    # Asana has actual_time_minutes on paid tiers only, and writing it is not
    # available through the plain task update - claiming it would make the
    # flusher send calls that silently do nothing.
    supports_time_tracking=False,
    # POST /attachments?parent=<task gid>, multipart
    supports_attachments=True,
    states=_ROUND_TRIP_STATES,
)


class AsanaProvider:
    def __init__(self, token: str | None = None, timeout: float = 20.0):
        self._token = token
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "asana"

    @property
    def capabilities(self) -> Capabilities:
        return _CAPABILITIES

    # ---------- transport ----------

    def _bearer(self) -> str:
        if self._token:
            return self._token
        from memory_mcp.providers.credentials import get_credential

        token = get_credential("asana")
        if not token:
            raise ProviderAuthError(
                "No Asana credential stored. Create a personal access token at "
                "https://app.asana.com/0/my-apps and store it with "
                "`memory-mcp provider set-credential asana`."
            )
        self._token = token
        return token

    def _request(self, method: str, path: str, *, params=None, json=None):
        headers = {"Authorization": f"Bearer {self._bearer()}",
                   "Accept": "application/json"}
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.request(
                    method, API + path, params=params, json=json, headers=headers,
                )
        except httpx.HTTPError as e:
            raise ProviderError(f"Asana unreachable: {e}") from e

        if resp.status_code in (401, 403):
            raise ProviderAuthError(
                f"Asana rejected the token ({resp.status_code}) on {path}. It may be "
                "revoked, or lack access to that workspace."
            )
        if resp.status_code == 404:
            raise ProviderError(f"Asana 404 on {path} - no such object")
        if resp.status_code >= 400:
            raise ProviderError(f"Asana {resp.status_code} on {path}: {resp.text[:200]}")
        if not resp.content:
            return None
        try:
            payload = resp.json()
        except ValueError as e:
            raise ProviderError(f"Asana returned non-JSON on {path}") from e
        # Every Asana response is {"data": ...}. Unwrapped here, once, so the
        # envelope never reaches shared code.
        return payload.get("data") if isinstance(payload, dict) else payload

    # ---------- spaces (workspaces) ----------

    def list_spaces(self) -> list[SpaceRef]:
        spaces = self._request("GET", "/workspaces") or []
        return [
            SpaceRef(id=s["gid"], title=s.get("name") or "")
            for s in spaces if s.get("gid")
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
        """Asana workspaces cannot be created through the API.

        Returning the first existing one rather than raising: the interface
        promises a space is always available to create a container in, and a
        workspace is an organisational fact a user sets up once, not something a
        task mirror should be inventing.
        """
        existing = self.find_space(title)
        if existing is not None:
            return existing
        spaces = self.list_spaces()
        if not spaces:
            raise ProviderError(
                "this Asana token can see no workspace, so there is nowhere to "
                "create a project"
            )
        return spaces[0]

    # ---------- containers (projects) ----------

    def list_containers(self, space_id: str | None = None) -> list[ContainerRef]:
        params = {"limit": 100}
        if space_id:
            params["workspace"] = space_id
        projects = self._request("GET", "/projects", params=params) or []
        return [
            ContainerRef(id=p["gid"], title=p.get("name") or "",
                         external_ref=None,
                         space_id=(p.get("workspace") or {}).get("gid"))
            for p in projects if p.get("gid")
        ]

    def find_container(self, external_ref: str) -> ContainerRef | None:
        """Asana has no caller-supplied key, so there is nothing to find by."""
        return None

    def fetch_container(self, container_id: str, *, with_tasks: bool = False) -> Container:
        project = self._request("GET", f"/projects/{container_id}")
        if not project:
            raise ProviderError(f"no Asana project {container_id}")
        sections = self._request("GET", f"/projects/{container_id}/sections") or []
        groups = tuple(
            Group(id=s["gid"], title=s.get("name") or "")
            for s in sections if s.get("gid")
        )
        tasks: tuple[RemoteTask, ...] = ()
        if with_tasks:
            rows = self._request(
                "GET", f"/projects/{container_id}/tasks",
                params={"opt_fields": "name,notes,completed,memberships.section.name",
                        "limit": 100},
            ) or []
            tasks = tuple(self._to_task(r) for r in rows if r.get("gid"))
        return Container(
            id=project["gid"], title=project.get("name") or "", external_ref=None,
            space_id=(project.get("workspace") or {}).get("gid"),
            groups=groups, tasks=tasks, url=project.get("permalink_url"),
        )

    def create_container(
        self, title: str, *, description: str = "", external_ref: str | None = None,
        space_id: str | None = None,
    ) -> Container:
        if not space_id:
            space = self.create_space(title)
            space_id = space.id
        created = self._request("POST", "/projects", json={"data": {
            "name": title, "notes": description, "workspace": space_id,
        }})
        if not created or not created.get("gid"):
            raise ProviderError("Asana returned a project without a gid")
        return self.fetch_container(created["gid"])

    # ---------- tasks ----------

    def create_task(
        self, container_id: str, group_id: str | None, title: str, *,
        description: str = "", external_ref: str | None = None,
    ) -> RemoteTask:
        data = {"name": title, "notes": description, "projects": [container_id]}
        if group_id:
            data["memberships"] = [{"project": container_id, "section": group_id}]
        created = self._request("POST", "/tasks", json={"data": data})
        if not created or not created.get("gid"):
            raise ProviderError("Asana returned a task without a gid")
        return RemoteTask(
            id=created["gid"], title=created.get("name") or title,
            state="todo", description=created.get("notes") or description,
            group_id=group_id, external_ref=None,
        )

    def set_state(self, task_id: str, state: str) -> None:
        """Write BOTH halves: the `completed` flag and the section.

        The flag alone cannot express blocked or paused, and the section alone
        leaves a task Asana still shows as open in every "my tasks" view. Writing
        both is what makes a state round-trip look the same as it does on a
        platform with one status field.
        """
        if state not in {s.value for s in TaskState}:
            raise ProviderError(f"unknown task state: {state!r}")
        task = self._request("GET", f"/tasks/{task_id}",
                             params={"opt_fields": "projects"})
        if not task:
            raise ProviderError(f"no Asana task {task_id}")
        self._request("PUT", f"/tasks/{task_id}",
                      json={"data": {"completed": state in _COMPLETED_STATES}})

        projects = [p.get("gid") for p in (task.get("projects") or []) if p.get("gid")]
        if projects:
            section = self._section_for_state(projects[0], state)
            if section:
                self.move(task_id, section)

    def move(self, task_id: str, group_id: str) -> None:
        self._request("POST", f"/sections/{group_id}/addTask",
                      json={"data": {"task": task_id}})

    def comment(self, task_id: str, body: str) -> None:
        self._request("POST", f"/tasks/{task_id}/stories",
                      json={"data": {"text": body}})

    def attach(self, task_id: str, filename: str, content: bytes,
               content_type: str | None = None) -> None:
        headers = {"Authorization": f"Bearer {self._bearer()}"}
        files = {"file": (filename, content, content_type or "application/octet-stream")}
        try:
            with httpx.Client(timeout=max(self._timeout, 60.0)) as client:
                resp = client.post(
                    f"{API}/attachments", params={"parent": task_id},
                    files=files, headers=headers,
                )
        except httpx.HTTPError as e:
            raise ProviderError(f"Asana unreachable: {e}") from e
        if resp.status_code in (401, 403):
            raise ProviderAuthError("Asana rejected the token on attach")
        if resp.status_code >= 400:
            raise ProviderError(f"Asana {resp.status_code} on attach: {resp.text[:200]}")

    def log_time(self, task_id: str, begin, end=None) -> None:  # pragma: no cover
        raise ProviderError("Asana time tracking is not writable through this API")

    # ---------- translation ----------

    def _to_task(self, row: dict) -> RemoteTask:
        memberships = row.get("memberships") or []
        section = (memberships[0].get("section") or {}) if memberships else {}
        return RemoteTask(
            id=row["gid"], title=row.get("name") or "",
            # The flag wins: a task marked complete is complete whatever section
            # it happens to have been left in.
            state="done" if row.get("completed") else self._state_of(section.get("name")),
            description=row.get("notes") or "",
            group_id=section.get("gid"), external_ref=None,
        )

    def _sections(self, container_id: str) -> list[Group]:
        rows = self._request("GET", f"/projects/{container_id}/sections") or []
        return [Group(id=r["gid"], title=r.get("name") or "")
                for r in rows if r.get("gid")]

    def _section_for_state(self, container_id: str, state: str) -> str | None:
        sections = self._sections(container_id)
        if not sections:
            return None
        by_title = {s.title.strip().lower(): s.id for s in sections}
        for alias in _SECTION_ALIASES.get(state, ()):
            if alias in by_title:
                return by_title[alias]
        return sections[0].id

    @staticmethod
    def _state_of(section_title: str | None) -> str:
        name = (section_title or "").strip().lower()
        for state, aliases in _SECTION_ALIASES.items():
            if name in aliases:
                return state
        return "todo"
