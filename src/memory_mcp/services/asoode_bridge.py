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

import threading

from memory_mcp.asoode import get_endpoints
from memory_mcp.providers import Container, ProviderError, TaskProvider
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


def build_state_list_map(board: Container) -> tuple[dict[str, str], str | None]:
    """Map each local task state to a board list id.

    Returns (map, default_list_id). Matching is by column title; every state gets
    an entry, falling back to the first column, so a push never has to decide
    what to do with an unmapped state mid-flight.
    """
    groups = list(board.groups)
    if not groups:
        return {}, None
    by_title = {g.title.strip().lower(): g.id for g in groups}
    default_id = groups[0].id
    mapping: dict[str, str] = {}
    for state in _LIST_ALIASES:
        target = default_id
        for alias in _LIST_ALIASES.get(state, ()):
            if alias in by_title:
                target = by_title[alias]
                break
        mapping[state] = target
    return mapping, default_id


class AsoodeBridge:
    def __init__(
        self, project_service, task_service, provider: TaskProvider | None = None,
        outbox_repo=None,
    ):
        self._projects = project_service
        self._tasks = task_service
        self._provider = provider
        self._outbox = outbox_repo
        # One flush per project at a time, whoever calls it. The background
        # mirror had a lock; a direct flush() bypassed it, so a manual flush
        # could race the mirror and both would DELETE the same outbox row -
        # DuckDB fails that with "Failed to delete all rows from index". The
        # lock belongs HERE, where every flush passes, not at one call site.
        self._flush_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _flush_lock(self, slug: str) -> threading.Lock:
        with self._locks_guard:
            return self._flush_locks.setdefault(slug, threading.Lock())

    @property
    def provider(self) -> TaskProvider:
        """The platform this bridge talks to.

        Built lazily and defaulting to asoode, which is the only implementation
        today; the provider registry replaces this default with a per-link lookup
        so one project can hold links to different platforms at once.
        """
        if self._provider is None:
            from memory_mcp.providers import AsoodeProvider

            self._provider = AsoodeProvider()
        return self._provider

    # Kept so existing callers and tests that say `.client` keep working; the
    # bridge itself no longer uses it.
    @property
    def client(self) -> TaskProvider:
        return self.provider

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
            space = next(
                (s for s in self.provider.list_spaces() if s.id == reuse_project_id),
                None,
            )
            if space is None:
                raise ProviderError(f"no space with id {reuse_project_id}")
        else:
            space = self.provider.find_space(title) or self.provider.create_space(
                title, description=project.description or "",
            )

        # externalRef is what makes this re-runnable: the project's stable uid,
        # so the same memory project always resolves to the same container.
        ref = project.project_uid or slug
        container = self.provider.create_container(
            board,
            description=f"Tasks mirrored from the {slug} memory project.",
            external_ref=f"memory-mcp:{ref}",
            space_id=space.id,
        )
        project_id, package_id = space.id, container.id
        state_map, default_list = build_state_list_map(container)
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
            "project": {"id": project_id, "title": space.title},
            "work_package": {"id": package_id, "title": container.title},
            "lists": [{"id": g.id, "title": g.title} for g in container.groups],
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
            raise ProviderError("give work_package_id or external_ref")

        if work_package_id:
            container = self.provider.fetch_container(work_package_id)
        else:
            found = self.provider.find_container(external_ref)
            if not found:
                raise ProviderError(
                    f"no board with externalRef {external_ref!r} is visible to this "
                    "credential. `memory-mcp asoode boards` lists what is."
                )
            container = self.provider.fetch_container(found.id)

        package_id = container.id
        project_id = container.space_id
        if not package_id or not project_id:
            raise ProviderError("the provider returned a container without ids")

        state_map, default_list = build_state_list_map(container)
        endpoints = get_endpoints()
        link = upsert_project_link(
            slug,
            base_url=endpoints.api_url,
            socket_url=endpoints.socket_url,
            remote_project_id=project_id,
            remote_work_package_id=package_id,
            label=label or container.title or package_id,
            is_default=is_default,
            default_list_id=default_list,
            state_list_map=state_map,
            match_paths=match_paths,
        )
        return {
            "link": link,
            "work_package": {"id": package_id, "title": container.title,
                             "external_ref": container.external_ref},
            "project": {"id": project_id},
            "lists": [{"id": g.id, "title": g.title} for g in container.groups],
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
            raise ProviderError(
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
        raise ProviderError(
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
            raise ProviderError(
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
        with self._flush_lock(slug):
            return self._flush_locked(slug, limit)

    def _flush_locked(self, slug: str, limit: int) -> dict:
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

        flushed = failed = skipped = abandoned = 0
        for row in pending:
            try:
                if self._flush_row(slug, row):
                    flushed += 1
                else:
                    skipped += 1
                self._outbox.resolve(slug, row["id"])
            except ProviderError as e:
                given_up = self._outbox.fail(slug, row["id"], str(e))
                failed += 1
                if given_up:
                    abandoned += 1
                # Stop at the first failure: the remote is unreachable or the
                # token is bad, and hammering it with the rest of the queue only
                # multiplies the wait. The rows are still there for next time.
                break
            except Exception as e:  # noqa: BLE001
                given_up = self._outbox.fail(slug, row["id"], f"{type(e).__name__}: {e}")
                failed += 1
                if given_up:
                    abandoned += 1
                break
        return {"flushed": flushed, "failed": failed, "skipped": skipped,
                "abandoned": abandoned, "remaining": self._outbox.depth(slug)}

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
            remote = self.provider.create_task(
                link["remote_work_package_id"], list_id, task.title,
                description=task.description or "",
                external_ref=f"memory-mcp:{task.id}",
            )
            remote_id = remote.id
            if not remote_id:
                raise ProviderError("the provider returned a task without an id")
            self._outbox.remember(slug, task.id, link["id"], remote_id, task.state.value)
            if task.state.value == "todo":
                return True  # created in ToDo already; no state call needed

        op = row["op"]
        if op == "comment":
            body = (row.get("payload") or {}).get("body")
            if body and self.provider.capabilities.supports_comments:
                self.provider.comment(remote_id, body)
            return True
        # create/state/update all reconcile to "make the remote match".
        self.provider.set_state(remote_id, task.state.value)
        # asoode keeps state and column independent, so a Done card would sit in
        # To Do forever without this. Best-effort: the state is the truth, the
        # column is presentation, and failing to move it must not fail the flush.
        target_list = (link.get("state_list_map") or {}).get(task.state.value)
        if target_list and self.provider.capabilities.supports_groups:
            try:
                self.provider.move(remote_id, target_list)
            except ProviderError:
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
        container = self.provider.fetch_container(
            link["remote_work_package_id"], with_tasks=True,
        )
        created, updated, skipped = [], [], 0
        suppressed = getattr(self._tasks, "_outbox", None)
        self._tasks._outbox = None
        try:
            return self._import_rows(slug, link, container, limit)
        finally:
            self._tasks._outbox = suppressed

    def _import_rows(self, slug, link, container, limit) -> dict:
        created, updated, skipped = [], [], 0
        for remote in list(container.tasks)[:limit]:
            remote_id = remote.id
            title = (remote.title or "").strip()
            if not remote_id or not title:
                skipped += 1
                continue
            state = remote.state
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
                description=remote.description or None,
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
            raise ProviderError(f"'{slug}' is not linked to any board.")
        boards, failed = [], []
        for link in links:
            try:
                boards.append(self.import_board(slug, link))
            except ProviderError as e:
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
        return [
            {"id": c.id, "title": c.title, "external_ref": c.external_ref,
             "project_id": c.space_id, "project_title": c.space_title}
            for c in self.provider.list_containers(project_id)
        ]

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
            container = self.provider.fetch_container(
                link["remote_work_package_id"], with_tasks=True,
            )
        except ProviderError as e:
            status["error"] = str(e)
            return status
        except Exception as e:  # noqa: BLE001 - session start must survive anything
            status["error"] = f"{type(e).__name__}: {e}"
            return status

        status["reachable"] = True
        # Closed work, in the shared vocabulary rather than one platform's ordinals.
        closed = {"done", "cancelled", "duplicate"}
        remote_open = [
            {"id": t.id, "title": t.title, "state": t.state}
            for t in container.tasks if t.state not in closed
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
            raise ProviderError(
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
            except ProviderError as e:
                failed.append({"task_id": task.id, "title": task.title, "error": str(e)})
                continue
            state_map = link.get("state_list_map") or {}
            default_list = link.get("default_list_id")
            list_id = state_map.get(task.state.value) or default_list
            try:
                remote = self.provider.create_task(
                    link["remote_work_package_id"], list_id, task.title,
                    description=task.description or "",
                    external_ref=f"memory-mcp:{task.id}",
                )
                remote_id = remote.id
                # asoode creates in ToDo; carry the real state over. Skipped for
                # todo so a re-push of an unchanged list makes no extra calls.
                if remote_id and task.state.value != "todo":
                    self.provider.set_state(remote_id, task.state.value)
                pushed.append({
                    "task_id": task.id, "remote_id": remote_id,
                    "title": task.title, "state": task.state.value,
                    "board": link.get("label"),
                    "work_package_id": link["remote_work_package_id"],
                })
            except ProviderError as e:
                failed.append({"task_id": task.id, "title": task.title, "error": str(e)})

        return {
            "slug": slug,
            "boards": sorted({p["board"] for p in pushed if p["board"]}),
            "pushed": pushed,
            "failed": failed,
            "counts": {"pushed": len(pushed), "failed": len(failed),
                       "considered": len(listing.tasks)},
        }
