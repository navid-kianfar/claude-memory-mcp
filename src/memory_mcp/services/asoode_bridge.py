"""The outbound bridge: mirror a memory project's task list onto an asoode board.

A MEMORY PROJECT LINKS TO WORK PACKAGES, NEVER TO A PROJECT. asoode's hierarchy
is project -> work package (board) -> list (column) -> task, and there is no route
that attaches a task to a project: POST /tasks/:listId/create is the only create
path, and a listId exists only inside a work package. "Linked to an asoode
project" is therefore not a state the API can represent - which is why bootstrap
CREATES a work package and attach RESOLVES one, and why project_links keys on
remote_work_package_id.

This generalises past asoode, and the planned provider interface must keep it:
every platform worth supporting has a mandatory container between the workspace
and the task - Asana project/section, Monday board/group, Trello board/list, Jira
project plus issue type. A provider with one implicit container supplies it; the
interface never makes the container optional.

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
from memory_mcp.asoode_client import (
    ORDINAL_TO_STATE, STATE_TO_ORDINAL, AsoodeClient, AsoodeError,
)
from memory_mcp.db.registry import (
    get_default_project_link,
    get_project_links,
    upsert_project_link,
)
from memory_mcp.models import (
    CreateTaskRequest, TaskFilter, TaskSource, TaskState, UpdateTaskRequest,
)

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
    def __init__(
        self, project_service, task_service, client: AsoodeClient | None = None,
        outbox_repo=None,
    ):
        self._projects = project_service
        self._tasks = task_service
        self._client = client
        self._outbox = outbox_repo

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

    def attach(
        self, slug: str, *, work_package_id: str | None = None,
        external_ref: str | None = None, label: str | None = None,
        is_default: bool = True, match_paths: list | None = None,
    ) -> dict:
        """Link a memory project to a board that ALREADY EXISTS. Creates nothing.

        `bootstrap` is for a project with no board yet. This is for the far more
        common case once a workspace is set up: the boards exist - one per app in
        a monorepo, say - and the memory project needs to point at them. Running
        bootstrap there would add a duplicate board beside the real ones.

        One memory project attaches to MANY boards; `is_default` picks the one a
        task with no explicit target routes to, and promoting a link demotes the
        others (see upsert_project_link).
        """
        if not work_package_id and not external_ref:
            raise AsoodeError("give work_package_id or external_ref")

        if work_package_id:
            board = self.client.fetch_work_package(work_package_id)
            if not board:
                raise AsoodeError(f"no asoode work package with id {work_package_id}")
        else:
            board = self.client.find_work_package(external_ref)
            if not board:
                raise AsoodeError(
                    f"no work package with externalRef {external_ref!r} is visible to "
                    "this token. `memory-mcp asoode boards` lists what is."
                )
            board = self.client.fetch_work_package(board["id"])

        package_id = board.get("id")
        project_id = board.get("projectId") or board.get("project_id")
        if not package_id or not project_id:
            raise AsoodeError("asoode returned a work package without ids")

        state_map, default_list = build_state_list_map(board)
        endpoints = get_endpoints()
        link = upsert_project_link(
            slug,
            base_url=endpoints.api_url,
            socket_url=endpoints.socket_url,
            remote_project_id=project_id,
            remote_work_package_id=package_id,
            label=label or board.get("title") or package_id,
            is_default=is_default,
            default_list_id=default_list,
            state_list_map=state_map,
            match_paths=match_paths,
        )
        return {
            "link": link,
            "work_package": {"id": package_id, "title": board.get("title"),
                             "external_ref": board.get("externalRef")},
            "project": {"id": project_id},
            "lists": [
                {"id": item.get("id"), "title": item.get("title")}
                for item in _board_lists(board)
            ],
            "url": f"{endpoints.app_url}/projects/{project_id}",
            "created": False,
        }

    # ---------- routing ----------
    #
    # THE RULE, stated once: a task goes to the link named by its `link_id`; a
    # task with no link_id goes to the project's DEFAULT link. Defaulting rather
    # than refusing is deliberate - every task created before link_id existed has
    # None, and every single-board project would otherwise stop working. The one
    # case that refuses is a project that has links but no default, because there
    # is then nothing to guess with.

    def resolve_link(self, slug: str, target: str | None) -> int | None:
        """Turn a board name into a link id. None means "use the default".

        `target` may be a link label, a work package externalRef, or a work
        package id - whichever the caller happens to have. Matching is
        case-insensitive on the label because it is the human-typed one.
        """
        if not target or not target.strip():
            return None
        wanted = target.strip().lower()
        links = get_project_links(slug)
        if not links:
            raise AsoodeError(
                f"'{slug}' is not linked to any asoode board, so it cannot target "
                f"{target!r}. Attach one with memory_asoode_attach."
            )
        for link in links:
            if (link.get("label") or "").strip().lower() == wanted:
                return link["id"]
        for link in links:
            if (link.get("remote_work_package_id") or "").lower() == wanted:
                return link["id"]
        known = ", ".join(sorted(l.get("label") or "?" for l in links))
        raise AsoodeError(
            f"no board named {target!r} is linked to '{slug}'. Linked boards: {known}."
        )

    def route(self, slug: str, task) -> dict | None:
        """The link a task belongs to, applying the rule above."""
        links = get_project_links(slug)
        if not links:
            return None
        link_id = getattr(task, "link_id", None)
        if link_id is not None:
            for link in links:
                if link["id"] == link_id:
                    return link
            # The link was deleted out from under the task. Fall through to the
            # default rather than dropping the task on the floor.
        default = next((l for l in links if l["is_default"]), None)
        if default is None:
            raise AsoodeError(
                f"'{slug}' has {len(links)} linked boards and no default, so a task "
                "with no target cannot be routed. Re-attach one as the default."
            )
        return default

    # ---------- the flusher ----------

    def flush(self, slug: str, *, limit: int = 200) -> dict:
        """Drain the outbox: mirror what changed locally, in order, per task.

        Incremental by design. `push` re-POSTs every task in the project, which
        is one network call per task on every mirror; this sends only what
        actually changed, which is what makes mirroring on every mutation
        affordable at all.

        A row that fails STAYS in the outbox with attempts incremented, so an
        unreachable asoode is a delay rather than a lost edit. Ordering is
        preserved per task because rows drain oldest-first.
        """
        if self._outbox is None:
            return {"flushed": 0, "failed": 0, "skipped": 0, "reason": "no outbox"}
        pending = self._outbox.pending(slug, limit)
        if not pending:
            return {"flushed": 0, "failed": 0, "skipped": 0}
        if not get_project_links(slug):
            # Unlinked project: the rows describe work with nowhere to go. Drop
            # them rather than retrying forever against a board that is not there.
            for row in pending:
                self._outbox.resolve(slug, row["id"])
            return {"flushed": 0, "failed": 0, "skipped": len(pending),
                    "reason": "project is not linked"}

        flushed = failed = skipped = 0
        for row in pending:
            try:
                if self._flush_row(slug, row):
                    flushed += 1
                else:
                    skipped += 1
                self._outbox.resolve(slug, row["id"])
            except AsoodeError as e:
                self._outbox.fail(slug, row["id"], str(e))
                failed += 1
                # Stop at the first failure: the remote is unreachable or the
                # token is bad, and hammering it with the rest of the queue only
                # multiplies the wait. The rows are still there for next time.
                break
            except Exception as e:  # noqa: BLE001
                self._outbox.fail(slug, row["id"], f"{type(e).__name__}: {e}")
                failed += 1
                break
        return {"flushed": flushed, "failed": failed, "skipped": skipped,
                "remaining": self._outbox.depth(slug)}

    def _flush_row(self, slug: str, row: dict) -> bool:
        """Mirror one outbox row. False when there is nothing to do."""
        try:
            task = self._tasks.get(slug, row["task_id"])
        except Exception:  # noqa: BLE001 - deleted locally before the flush ran
            return False
        link = self.route(slug, task)
        if link is None:
            return False

        remote_id = self._outbox.remote_id(slug, task.id, link["id"])
        if not remote_id:
            # create_task with the task's externalRef is BOTH create and lookup:
            # asoode returns the existing row for a repeated ref, so recovering a
            # lost mapping costs one call and can never duplicate.
            state_map = link.get("state_list_map") or {}
            list_id = state_map.get(task.state.value) or link.get("default_list_id")
            remote = self.client.create_task(
                list_id, task.title,
                description=task.description or "",
                external_ref=f"memory-mcp:{task.id}",
            )
            remote_id = (remote or {}).get("id")
            if not remote_id:
                raise AsoodeError("asoode returned a task without an id")
            self._outbox.remember(slug, task.id, link["id"], remote_id, task.state.value)
            if task.state.value == "todo":
                return True  # created in ToDo already; no state call needed

        op = row["op"]
        if op == "comment":
            body = (row.get("payload") or {}).get("body")
            if body:
                self.client.comment(remote_id, body)
            return True
        # create/state/update all reconcile to "make the remote match".
        self.client.change_state(remote_id, task.state.value)
        # asoode keeps state and column independent, so a Done card would sit in
        # To Do forever without this. Best-effort: the state is the truth, the
        # column is presentation, and failing to move it must not fail the flush.
        target_list = (link.get("state_list_map") or {}).get(task.state.value)
        if target_list:
            try:
                self.client.reposition(remote_id, target_list)
            except AsoodeError:
                pass
        self._outbox.remember(slug, task.id, link["id"], remote_id, task.state.value)
        return True

    # ---------- inbound ----------

    def import_board(self, slug: str, link: dict, *, limit: int = 500) -> dict:
        """Pull one board's tasks into the local store.

        IDENTITY IS THE REMOTE ID, held in task_sync - not the title, and not
        externalRef, because a task created in asoode by a human has no
        externalRef at all. That is why re-importing updates rather than
        duplicating.

        Import-only, deliberately: local edits are not pushed back from here and
        a remote change overwrites the local title/state/description. Two-way
        merge needs a conflict policy, which is its own (still blocked) decision.
        """
        # An import writes through TaskService, which queues a mirror for every
        # write - so importing 35 tasks would immediately push 35 of them back.
        # Suppress the outbox for the duration: these changes CAME FROM asoode.
        board = self.client.fetch_work_package(link["remote_work_package_id"])
        if not board:
            raise AsoodeError(
                f"work package {link['remote_work_package_id']} is not readable"
            )
        created, updated, skipped = [], [], 0
        suppressed = getattr(self._tasks, "_outbox", None)
        self._tasks._outbox = None
        try:
            return self._import_rows(slug, link, board, limit)
        finally:
            self._tasks._outbox = suppressed

    def _import_rows(self, slug, link, board, limit) -> dict:
        created, updated, skipped = [], [], 0
        for board_list in _board_lists(board):
            for remote in (board_list.get("tasks") or [])[:limit]:
                remote_id = remote.get("id")
                title = (remote.get("title") or "").strip()
                if not remote_id or not title:
                    skipped += 1
                    continue
                state = ORDINAL_TO_STATE.get(remote.get("state"), "todo")
                local_id = self._outbox.local_id_for_remote(slug, link["id"], remote_id)

                if local_id:
                    try:
                        current = self._tasks.get(slug, local_id)
                    except Exception:  # noqa: BLE001 - deleted locally since
                        current = None
                    if current is not None:
                        if current.title != title or current.state.value != state:
                            self._tasks.update(UpdateTaskRequest(
                                project=slug, task_id=local_id, title=title,
                                state=TaskState(state),
                            ))
                            updated.append({"task_id": local_id, "title": title})
                        continue

                task = self._tasks.create(CreateTaskRequest(
                    project=slug, title=title,
                    description=remote.get("description") or None,
                    source=TaskSource.ASOODE if hasattr(TaskSource, "ASOODE") else TaskSource.USER,
                ))
                if state != "todo":
                    self._tasks.update(UpdateTaskRequest(
                        project=slug, task_id=task.id, state=TaskState(state),
                    ))
                self._tasks.set_link(slug, task.id, link["id"])
                self._outbox.remember(slug, task.id, link["id"], remote_id, state)
                created.append({"task_id": task.id, "title": title, "state": state})

        return {
            "board": link.get("label"),
            "work_package_id": link["remote_work_package_id"],
            "created": created, "updated": updated, "skipped": skipped,
            "counts": {"created": len(created), "updated": len(updated)},
        }

    def import_all(self, slug: str) -> dict:
        """Pull every linked board into the local store."""
        links = get_project_links(slug)
        if not links:
            raise AsoodeError(f"'{slug}' is not linked to any asoode board.")
        boards, failed = [], []
        for link in links:
            try:
                boards.append(self.import_board(slug, link))
            except AsoodeError as e:
                failed.append({"board": link.get("label"), "error": str(e)})
        return {
            "slug": slug, "boards": boards, "failed": failed,
            "counts": {
                "created": sum(b["counts"]["created"] for b in boards),
                "updated": sum(b["counts"]["updated"] for b in boards),
                "boards": len(boards),
            },
        }

    def boards(self, project_id: str | None = None) -> list[dict]:
        """Every board this token can see - what to attach to."""
        return self.client.list_work_packages(project_id)

    def links(self, slug: str) -> list[dict]:
        return get_project_links(slug)

    def queue_status(self, slug: str, *, timeout: float = 6.0) -> dict | None:
        """What the bound board currently holds. None when the project is unbound.

        Called on the session-start path, so it must never raise and never hang:
        an unreachable asoode, a revoked PAT and a project with no link are all
        ordinary outcomes reported in the return value. The short timeout is
        deliberate - a session opening is not the place to wait on a network.

        `remote_only` is the interesting half: tasks that exist on the board but
        not in the local list, i.e. requirements someone added in asoode. Matched
        by title today, because asoode's board fetch drops externalRef (see the
        queued task about it) - good enough to report, not good enough to import,
        which is why this reports rather than writing.
        """
        link = get_default_project_link(slug)
        if link is None:
            return None

        endpoints = get_endpoints()
        board_url = (
            f"{endpoints.app_url}/projects/{link['remote_project_id']}"
            if link.get("remote_project_id") else endpoints.app_url
        )
        status = {
            "bound": True,
            "board_url": board_url,
            "work_package_id": link["remote_work_package_id"],
            "reachable": False,
            "error": None,
            "remote_open": [],
            "remote_only": [],
        }
        try:
            client = self._client or AsoodeClient.from_settings(timeout=timeout)
            board = client.fetch_work_package(link["remote_work_package_id"])
        except AsoodeError as e:
            status["error"] = str(e)
            return status
        except Exception as e:  # noqa: BLE001 - session start must survive anything
            status["error"] = f"{type(e).__name__}: {e}"
            return status

        status["reachable"] = True
        # asoode states 3/6/7 are Done/Cancelled/Duplicate - closed work.
        closed = {3, 6, 7}
        remote_open = [
            {"id": task.get("id"), "title": task.get("title"), "state": task.get("state")}
            for board_list in _board_lists(board)
            for task in (board_list.get("tasks") or [])
            if task.get("state") not in closed
        ]
        status["remote_open"] = remote_open

        local_titles = {
            task.title.strip().lower()
            for task in self._tasks.list_tasks(
                slug, TaskFilter(include_done=True, include_subtasks=True), limit=500,
            ).tasks
        }
        status["remote_only"] = [
            task["title"] for task in remote_open
            if (task["title"] or "").strip().lower() not in local_titles
        ]
        return status

    # ---------- pushing ----------

    def push(self, slug: str, *, limit: int = 500, include_done: bool = True) -> dict:
        """Mirror the local task list onto the linked board.

        Idempotent by `externalRef`, so this is the same call whether it is the
        first push or the fifth. Each task's state is applied after creation:
        asoode creates every task in ToDo, and the column a task sits in is
        cosmetic next to `state`, which is what the local store actually holds.
        """
        if not get_project_links(slug):
            raise AsoodeError(
                f"'{slug}' is not linked to an asoode board yet - attach one with "
                "memory_asoode_attach, or create one with memory_asoode_link."
            )

        listing = self._tasks.list_tasks(
            slug,
            TaskFilter(include_done=include_done, include_subtasks=True),
            limit=limit,
        )
        pushed, failed = [], []
        for task in listing.tasks:
            # Each task goes to ITS OWN board: a monorepo's tasks are spread
            # across per-app boards, so one default for all of them is wrong.
            try:
                link = self.route(slug, task)
            except AsoodeError as e:
                failed.append({"task_id": task.id, "title": task.title, "error": str(e)})
                continue
            state_map = link.get("state_list_map") or {}
            default_list = link.get("default_list_id")
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
                    "board": link.get("label"),
                    "work_package_id": link["remote_work_package_id"],
                })
            except AsoodeError as e:
                failed.append({"task_id": task.id, "title": task.title, "error": str(e)})

        return {
            "slug": slug,
            "boards": sorted({p["board"] for p in pushed if p["board"]}),
            "pushed": pushed,
            "failed": failed,
            "counts": {"pushed": len(pushed), "failed": len(failed),
                       "considered": len(listing.tasks)},
        }
