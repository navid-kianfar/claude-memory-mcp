"""The outbound bridge: mirror a memory project's task list onto an asoode board.

Two operations, both safe to re-run:

`bootstrap` finds or creates the asoode project and its work package, works out
which board list each task state belongs in, and stores the result as a
`project_links` row. The work package carries `externalRef = <project_uid>`, so a
second bootstrap of the same memory project returns the same board rather than
adding another one.

`push` mirrors local tasks onto that board, each carrying `externalRef = <task
id>`. asoode returns the existing task for a repeated key, which makes the same
call serve as both create and lookup - so no local mapping table is needed to
stay idempotent, and a re-push after a crash cannot double anything.

What this is NOT, yet: the inbound half. Nothing here reads asoode back into the
local store, so a change made in asoode does not reach the task list. That is the
socket subscription plus the `updatedSince` reconcile poll, and it belongs in the
daemon's lifespan (see daemon.build_app).
"""

from memory_mcp.asoode import get_endpoints
from memory_mcp.asoode_client import STATE_TO_ORDINAL, AsoodeClient, AsoodeError
from memory_mcp.db.registry import (
    get_default_project_link,
    get_project_links,
    upsert_project_link,
)
from memory_mcp.models import TaskFilter

# Board-list titles a state maps onto, in preference order. asoode's Kanban
# template names its columns in English; anything unmatched falls back to the
# first list, because `state` - not the column - is what the local store means.
_LIST_ALIASES = {
    "todo": ("to do", "todo", "backlog", "new"),
    "in_progress": ("in progress", "doing", "wip"),
    "done": ("done", "complete", "completed", "finished"),
    "paused": ("paused", "on hold", "hold"),
    "blocked": ("blocked", "impediment"),
    "cancelled": ("cancelled", "canceled", "dropped"),
    "duplicate": ("duplicate",),
    "incomplete": ("incomplete",),
    "blocker": ("blocker",),
}


def _board_lists(board: dict) -> list[dict]:
    """The board's columns, whichever key this asoode build returns them under."""
    for key in ("lists", "workPackageLists", "boardLists"):
        value = board.get(key)
        if isinstance(value, list) and value:
            return value
    return []


def build_state_list_map(board: dict) -> tuple[dict[str, str], str | None]:
    """Map each local task state to a board list id.

    Returns (map, default_list_id). Matching is by column title; every state gets
    an entry, falling back to the first column, so a push never has to decide
    what to do with an unmapped state mid-flight.
    """
    lists = _board_lists(board)
    if not lists:
        return {}, None
    by_title = {
        (item.get("title") or "").strip().lower(): item.get("id") for item in lists
    }
    default_id = lists[0].get("id")
    mapping: dict[str, str] = {}
    for state in STATE_TO_ORDINAL:
        target = default_id
        for alias in _LIST_ALIASES.get(state, ()):
            if alias in by_title:
                target = by_title[alias]
                break
        mapping[state] = target
    return mapping, default_id


class AsoodeBridge:
    def __init__(self, project_service, task_service, client: AsoodeClient | None = None):
        self._projects = project_service
        self._tasks = task_service
        self._client = client

    @property
    def client(self) -> AsoodeClient:
        if self._client is None:
            self._client = AsoodeClient.from_settings()
        return self._client

    # ---------- linking ----------

    def bootstrap(
        self, slug: str, *, project_title: str | None = None,
        board_title: str | None = None, reuse_project_id: str | None = None,
    ) -> dict:
        """Create (or find) the asoode project + board for a memory project.

        `reuse_project_id` puts the board inside an existing asoode project
        instead of making a new one - the usual choice when a team already has a
        project and wants this repo as one more board in it.
        """
        project = self._projects.get(slug)
        title = project_title or project.display_name or slug
        board = board_title or project.display_name or slug

        if reuse_project_id:
            remote_project = self.client.fetch_project(reuse_project_id)
            if not remote_project:
                raise AsoodeError(f"no asoode project with id {reuse_project_id}")
        else:
            remote_project = self.client.find_project_by_title(title)
            if remote_project is None:
                remote_project = self.client.create_project(
                    title, description=project.description or "",
                )

        project_id = remote_project.get("id")
        if not project_id:
            raise AsoodeError("asoode returned a project without an id")

        # externalRef is what makes this re-runnable: the project's stable uid,
        # so the same memory project always resolves to the same board.
        ref = project.project_uid or slug
        work_package = self.client.create_work_package(
            project_id, board,
            description=f"Tasks mirrored from the {slug} memory project.",
            external_ref=f"memory-mcp:{ref}",
        )
        package_id = work_package.get("id")
        if not package_id:
            raise AsoodeError("asoode returned a work package without an id")

        state_map, default_list = build_state_list_map(work_package)
        endpoints = get_endpoints()
        link = upsert_project_link(
            slug,
            base_url=endpoints.api_url,
            socket_url=endpoints.socket_url,
            remote_project_id=project_id,
            remote_work_package_id=package_id,
            label=board,
            default_list_id=default_list,
            state_list_map=state_map,
        )
        return {
            "link": link,
            "project": {"id": project_id, "title": remote_project.get("title")},
            "work_package": {"id": package_id, "title": work_package.get("title")},
            "lists": [
                {"id": item.get("id"), "title": item.get("title")}
                for item in _board_lists(work_package)
            ],
            "url": f"{endpoints.app_url}/projects/{project_id}",
        }

    def links(self, slug: str) -> list[dict]:
        return get_project_links(slug)

    # ---------- pushing ----------

    def push(self, slug: str, *, limit: int = 500, include_done: bool = True) -> dict:
        """Mirror the local task list onto the linked board.

        Idempotent by `externalRef`, so this is the same call whether it is the
        first push or the fifth. Each task's state is applied after creation:
        asoode creates every task in ToDo, and the column a task sits in is
        cosmetic next to `state`, which is what the local store actually holds.
        """
        link = get_default_project_link(slug)
        if link is None:
            raise AsoodeError(
                f"'{slug}' is not linked to an asoode board yet - run bootstrap first."
            )
        state_map = link.get("state_list_map") or {}
        default_list = link.get("default_list_id")
        if not default_list and not state_map:
            raise AsoodeError("the stored link has no board lists; re-run bootstrap.")

        listing = self._tasks.list_tasks(
            slug,
            TaskFilter(include_done=include_done, include_subtasks=True),
            limit=limit,
        )
        pushed, failed = [], []
        for task in listing.tasks:
            list_id = state_map.get(task.state.value) or default_list
            try:
                remote = self.client.create_task(
                    list_id, task.title,
                    description=task.description or "",
                    external_ref=f"memory-mcp:{task.id}",
                )
                remote_id = (remote or {}).get("id")
                # asoode creates in ToDo; carry the real state over. Skipped for
                # todo so a re-push of an unchanged list makes no extra calls.
                if remote_id and task.state.value != "todo":
                    self.client.change_state(remote_id, task.state.value)
                pushed.append({
                    "task_id": task.id, "remote_id": remote_id,
                    "title": task.title, "state": task.state.value,
                })
            except AsoodeError as e:
                failed.append({"task_id": task.id, "title": task.title, "error": str(e)})

        return {
            "slug": slug,
            "work_package_id": link["remote_work_package_id"],
            "pushed": pushed,
            "failed": failed,
            "counts": {"pushed": len(pushed), "failed": len(failed),
                       "considered": len(listing.tasks)},
        }
