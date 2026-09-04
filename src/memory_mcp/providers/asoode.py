"""asoode as a TaskProvider.

Everything asoode-specific stops here. Above this line the bridge sees spaces,
containers, groups and local state names; below it are work packages, lists,
WorkPackageTaskState ordinals and an OperationResult envelope.

The three translations that earn the adapter:

1. STATE ORDINALS. asoode's WorkPackageTaskState is 1-9; ours are names. The two
   vocabularies are the same list in the same order, which is why this is a
   lookup rather than a mapping with a fallback - and why an unknown state is an
   error instead of a silent default.

2. STATE AND COLUMN ARE INDEPENDENT. `change-state` does not move the card, so a
   Done task keeps sitting in To Do. `set_state` therefore also repositions,
   best-effort: the state is the truth, the column is presentation, and a failed
   move must not fail the state change.

3. A TASK IS CREATED INTO A LIST, NOT A BOARD. `POST /tasks/:listId/create` is
   the only create route, so `create_task` resolves the container's first list
   when the caller names no group. That resolution costs a fetch, which is why
   callers that know the group pass it.
"""

from memory_mcp.asoode_client import (
    ORDINAL_TO_STATE,
    STATE_TO_ORDINAL,
    AsoodeClient,
    AsoodeError,
)
from memory_mcp.providers.base import (
    Capabilities,
    Container,
    ContainerRef,
    Group,
    RemoteTask,
    SpaceRef,
)

_CAPABILITIES = Capabilities(
    # externalRef is unique per parent and a repeated create returns the existing
    # row, so create doubles as lookup and the flusher can retry safely.
    supports_external_ref=True,
    supports_comments=True,
    supports_groups=True,
    # asoode keeps `state` and `listId` separate - see translation 2 above.
    supports_independent_state=True,
    # POST /tasks/:id/spend-time {begin, end}
    supports_time_tracking=True,
    states=tuple(STATE_TO_ORDINAL),
)


class AsoodeProvider:
    """The asoode implementation of TaskProvider."""

    def __init__(self, client: AsoodeClient | None = None):
        self._client = client

    @property
    def client(self) -> AsoodeClient:
        """Built lazily: constructing a provider must not require a credential."""
        if self._client is None:
            self._client = AsoodeClient.from_settings()
        return self._client

    @property
    def name(self) -> str:
        return "asoode"

    @property
    def capabilities(self) -> Capabilities:
        return _CAPABILITIES

    # ---------- spaces (asoode projects) ----------

    def list_spaces(self) -> list[SpaceRef]:
        return [
            SpaceRef(id=p["id"], title=p.get("title") or "")
            for p in self.client.list_projects()
            if p.get("id")
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
        # Match by title first: asoode projects carry no externalRef, so the
        # title is the only handle, and creating a second "AchaSoft" is worse
        # than reusing the one that exists.
        existing = self.find_space(title)
        if existing is not None:
            return existing
        created = self.client.create_project(title, description=description)
        if not created or not created.get("id"):
            raise AsoodeError("asoode returned a project without an id")
        return SpaceRef(id=created["id"], title=created.get("title") or title)

    # ---------- containers (work packages) ----------

    def list_containers(self, space_id: str | None = None) -> list[ContainerRef]:
        return [
            ContainerRef(
                id=b["id"], title=b.get("title") or "",
                external_ref=b.get("external_ref"),
                space_id=b.get("project_id"), space_title=b.get("project_title"),
            )
            for b in self.client.list_work_packages(space_id)
            if b.get("id")
        ]

    def find_container(self, external_ref: str) -> ContainerRef | None:
        board = self.client.find_work_package(external_ref)
        if not board:
            return None
        return ContainerRef(
            id=board["id"], title=board.get("title") or "",
            external_ref=board.get("externalRef"),
            space_id=board.get("projectId"),
        )

    def fetch_container(self, container_id: str, *, with_tasks: bool = False) -> Container:
        board = self.client.fetch_work_package(container_id)
        if not board:
            raise AsoodeError(f"no asoode work package with id {container_id}")
        return self._to_container(board, with_tasks=with_tasks)

    def create_container(
        self, title: str, *, description: str = "", external_ref: str | None = None,
        space_id: str | None = None,
    ) -> Container:
        if not space_id:
            raise AsoodeError(
                "asoode cannot hold a work package outside a project - pass space_id"
            )
        board = self.client.create_work_package(
            space_id, title, description=description, external_ref=external_ref,
        )
        if not board or not board.get("id"):
            raise AsoodeError("asoode returned a work package without an id")
        return self._to_container(board)

    # ---------- tasks ----------

    def create_task(
        self, container_id: str, group_id: str | None, title: str, *,
        description: str = "", external_ref: str | None = None,
    ) -> RemoteTask:
        list_id = group_id or self._first_group(container_id)
        remote = self.client.create_task(
            list_id, title, description=description, external_ref=external_ref,
        )
        remote_id = (remote or {}).get("id")
        if not remote_id:
            raise AsoodeError("asoode returned a task without an id")
        return RemoteTask(
            id=remote_id, title=remote.get("title") or title,
            state=ORDINAL_TO_STATE.get(remote.get("state"), "todo"),
            description=remote.get("description") or description,
            group_id=list_id, external_ref=external_ref,
        )

    def set_state(self, task_id: str, state: str) -> None:
        if state not in STATE_TO_ORDINAL:
            raise AsoodeError(f"unknown task state: {state!r}")
        self.client.change_state(task_id, state)

    def move(self, task_id: str, group_id: str) -> None:
        self.client.reposition(task_id, group_id)

    def comment(self, task_id: str, body: str) -> None:
        self.client.comment(task_id, body)

    def log_time(self, task_id: str, begin, end=None) -> None:
        """asoode takes ISO instants; a datetime is what the local store holds."""
        self.client.spend_time(
            task_id,
            begin.isoformat() if hasattr(begin, "isoformat") else str(begin),
            (end.isoformat() if hasattr(end, "isoformat") else end) if end else None,
        )

    # ---------- translation ----------

    def _first_group(self, container_id: str) -> str:
        container = self.fetch_container(container_id)
        if not container.groups:
            raise AsoodeError(
                f"work package {container_id} has no lists to create a task in"
            )
        return container.groups[0].id

    @staticmethod
    def _board_lists(board: dict) -> list[dict]:
        for key in ("lists", "workPackageLists", "boardLists"):
            value = board.get(key)
            if isinstance(value, list) and value:
                return value
        return []

    def _to_container(self, board: dict, *, with_tasks: bool = False) -> Container:
        lists = self._board_lists(board)
        tasks: list[RemoteTask] = []
        if with_tasks:
            for board_list in lists:
                for task in board_list.get("tasks") or []:
                    if not task.get("id"):
                        continue
                    tasks.append(RemoteTask(
                        id=task["id"],
                        title=(task.get("title") or "").strip(),
                        state=ORDINAL_TO_STATE.get(task.get("state"), "todo"),
                        description=task.get("description") or "",
                        group_id=board_list.get("id"),
                        external_ref=task.get("externalRef"),
                    ))
        return Container(
            id=board["id"],
            title=board.get("title") or "",
            external_ref=board.get("externalRef"),
            space_id=board.get("projectId"),
            groups=tuple(
                Group(id=item["id"], title=item.get("title") or "")
                for item in lists if item.get("id")
            ),
            tasks=tuple(tasks),
        )
