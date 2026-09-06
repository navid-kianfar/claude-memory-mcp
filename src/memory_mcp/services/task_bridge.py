"""The task bridge: mirror a memory project's task list onto a remote board.

Provider-agnostic since the TaskProvider interface landed - it holds a provider,
never a platform client, and every call routes through the link's provider. The
file was called asoode_bridge.py while asoode was the only implementation.

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

The flusher (`flush` / `_flush_row`) is the outbound half: every local mutation
lands in the outbox as an op, and each op maps onto the provider calls that make
the remote card match - state, fields, labels, assignee, parent, comments,
attachments, time, archive, delete. A card created by the flusher gets EVERY
field the local task already has, not just its title.

The inbound half is `reconcile` / `import_board` below, driven by the daemon's
socket subscription and its catch-up sweep. It only CREATES: a task on both
sides is never overwritten, because that needs the two-sided conflict policy
that is still an open decision. `import_all` is the explicit path that does
overwrite title and state.
"""

import logging
import threading

from memory_mcp.asoode import get_endpoints
from memory_mcp.providers import (
    Container, ProviderError, TaskProvider, TransientProviderError,
)
from memory_mcp.services.echo_log import EchoLog
from memory_mcp.db.registry import (
    get_default_project_link,
    get_project_links,
    upsert_project_link,
)
from memory_mcp.models import (
    CreateTaskRequest, TaskFilter, TaskSource, TaskState, UpdateTaskRequest,
)

# How many batches one flush call will drain before handing back. Bounded so a
# project that is being written to continuously cannot pin the flusher thread.
_MAX_FLUSH_PASSES = 20

# Task fields `update_fields` carries, in the shared vocabulary.
_FIELD_KEYS = frozenset({
    "title", "description", "priority", "due_at", "begin_at", "end_at",
    "estimated_minutes",
})

logger = logging.getLogger(__name__)

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


# The columns a board we create should have, and the colour each one carries.
#
# The hexes are NOT invented: they are six of the twelve swatches asoode's own
# board-column picker offers (AVAILABLE_COLORS in the frontend's
# WpBoardColumn.tsx), so a board this code builds is indistinguishable from one
# a person built with the picker. #59a8ef is additionally asoode's own
# In-Progress colour across its Gantt, calendar, list and roadmap views.
#
# Asked for by the user on 2026-09-05: "when creating the lists for the board,
# use the color from the pre selected list we have" - backlog gray, todo yellow,
# in progress blue, done green, cancelled red.
#
# Order matters: it is the order the columns are created in, so a board that is
# missing several gets them left to right the way a person reads a board.
BOARD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Backlog", "#626971"),      # slate gray
    ("To Do", "#fbb900"),        # yellow
    ("In Progress", "#59a8ef"),  # blue
    ("Done", "#4caf50"),         # green
    ("Cancelled", "#f44336"),    # red
)


def _comment_text(comment: dict) -> str:
    """What the board shows for a local comment.

    A rule or a decision pinned to a task must not read as chatter there any
    more than it does here, and the author matters when several agents write
    to one card.

    The kind gets its OWN line rather than sitting inline. Comment bodies are
    markdown now, so `[decision] ## Heading` would push the heading off the
    start of its line and render it as literal text - the one part of the
    comment the prefix was meant to dignify.
    """
    body = comment.get("body") or ""
    kind = (comment.get("kind") or "note").strip().lower()
    if kind and kind != "note":
        body = f"**[{kind}]**\n\n{body}"
    author = (comment.get("author") or "").strip()
    if author:
        body = f"{body}\n\n— {author}"
    return body


def build_state_list_map(board: Container) -> tuple[dict[str, str], str | None]:
    """Map each local task state to a board list id.

    Returns (map, default_list_id). Matching is by column title, and ONLY states
    with a real column get an entry.

    A state with no column is deliberately absent rather than pointed at the
    first one. The rule is "if there is a column for that status, move the task
    there" - so a board with To Do / In Progress / Done leaves a blocked task
    exactly where it sits, instead of yanking it into Backlog and losing the
    position someone put it in. Six of the nine states used to map to the same
    fallback column, which made a state change look like a demotion.

    `default_list_id` is still returned, because CREATING a task needs some
    column even when its state has none.
    """
    groups = list(board.groups)
    if not groups:
        return {}, None
    by_title = {g.title.strip().lower(): g.id for g in groups}
    default_id = groups[0].id
    mapping: dict[str, str] = {}
    for state, aliases in _LIST_ALIASES.items():
        for alias in aliases:
            if alias in by_title:
                mapping[state] = by_title[alias]
                break
    return mapping, default_id


#: `_resolve_local`'s answer for a card whose externalRef names a task that was
#: deleted HERE - distinct from None ("never seen"), which creates.
_DELETED_HERE = "__deleted_here__"


class TaskBridge:
    def __init__(
        self, project_service, task_service, provider: TaskProvider | None = None,
        outbox_repo=None, attachment_repo=None,
    ):
        self._projects = project_service
        self._tasks = task_service
        self._provider = provider
        self._outbox = outbox_repo
        self._attachments = attachment_repo
        # One flush per project at a time, whoever calls it. The background
        # mirror had a lock; a direct flush() bypassed it, so a manual flush
        # could race the mirror and both would DELETE the same outbox row -
        # DuckDB fails that with "Failed to delete all rows from index". The
        # lock belongs HERE, where every flush passes, not at one call site.
        self._flush_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # What we have just written to the remote, so the socket can ignore the
        # broadcast of our own change. asoode does no actor exclusion and drops
        # the actor id before the client sees it, so the writer has to say.
        self.echo = EchoLog()

    def _flush_lock(self, slug: str) -> threading.Lock:
        with self._locks_guard:
            return self._flush_locks.setdefault(slug, threading.Lock())

    @property
    def provider(self) -> TaskProvider:
        """The provider for calls that are not scoped to a link.

        `boards()` and a first `bootstrap` have no link to route by, so they use
        this. An explicitly injected provider wins - that is how a test drives
        the whole bridge with one fake - and otherwise the registry's default
        answers.
        """
        if self._provider is not None:
            return self._provider
        from memory_mcp.providers import get_provider

        return get_provider()

    def provider_for(self, link: dict | None) -> TaskProvider:
        """The provider a LINK routes to - the point of the registry.

        One memory project holds links to different platforms at once, so which
        implementation to use is a per-link question. An injected provider still
        overrides, so a fake stays a fake for every link in a test.
        """
        if self._provider is not None:
            return self._provider
        from memory_mcp.providers import provider_for_link

        return provider_for_link(link)

    # Kept so existing callers and tests that say `.client` keep working; the
    # bridge itself no longer uses it.
    @property
    def client(self) -> TaskProvider:
        return self.provider

    # ---------- linking ----------

    def bootstrap(
        self, slug: str, *, project_title: str | None = None,
        board_title: str | None = None, reuse_project_id: str | None = None,
        provider: str | None = None, backfill: bool = False,
    ) -> dict:
        """Create (or find) the asoode project + board for a memory project.

        `reuse_project_id` puts the board inside an existing asoode project
        instead of making a new one - the usual choice when a team already has a
        project and wants this repo as one more board in it.
        """
        project = self._projects.get(slug)
        title = project_title or project.display_name or slug
        board = board_title or project.display_name or slug

        impl = self.provider_for({"provider": provider} if provider else None)
        if reuse_project_id:
            space = next(
                (s for s in impl.list_spaces() if s.id == reuse_project_id),
                None,
            )
            if space is None:
                raise ProviderError(f"no space with id {reuse_project_id}")
        else:
            space = impl.find_space(title) or impl.create_space(
                title, description=project.description or "",
            )

        # externalRef is what makes this re-runnable: the project's stable uid,
        # so the same memory project always resolves to the same container.
        ref = project.project_uid or slug
        container = impl.create_container(
            board,
            description=f"Tasks mirrored from the {slug} memory project.",
            external_ref=f"memory-mcp:{ref}",
            space_id=space.id,
        )
        project_id, package_id = space.id, container.id
        # Give the board its columns and their colours before the state map is
        # built - a Cancelled column created here is one the map can then find,
        # and a `cancelled` task stops falling back to whatever column is first.
        container = self._ensure_columns(impl, container)
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
            provider=impl.name,
        )
        return {
            "link": link,
            "project": {"id": project_id, "title": space.title},
            "work_package": {"id": package_id, "title": container.title},
            "lists": [{"id": g.id, "title": g.title} for g in container.groups],
            "url": f"{endpoints.app_url}/projects/{project_id}",
            **self._backfill_offer(slug, backfill),
        }

    def column_plan(self, slug: str) -> list[dict]:
        """What ensure_board_columns WOULD do, changing nothing.

        A board is a shared artefact, so the default answer to "make my columns
        standard" is a preview rather than an edit.

        EVERY linked board, not just the default. A monorepo project has one
        board per app, and reporting on the default alone said "all done" while
        eight boards still had unstyled columns and no Cancelled - which is
        exactly what happened to the asoode project on 2026-09-06.
        """
        links = get_project_links(slug)
        if not links:
            raise ProviderError(f"'{slug}' is not linked to a board")
        out = []
        for link in links:
            board = link.get("label") or link["remote_work_package_id"]
            try:
                provider = self.provider_for(link)
                container = provider.fetch_container(link["remote_work_package_id"])
            except Exception as e:  # noqa: BLE001 - one unreachable board must
                # not hide the plan for the others.
                out.append({"board": board, "error": str(e)})
                continue
            have = {g.title.strip().lower(): g for g in container.groups}
            for title, color in BOARD_COLUMNS:
                group = have.get(title.strip().lower())
                if group is None:
                    out.append({"board": board, "column": title,
                                "action": "create", "color": color})
                elif (group.color or "").strip():
                    out.append({
                        "board": board, "column": title, "action": "keep",
                        "color": group.color,
                        "note": "colour already set - never repainted",
                    })
                else:
                    out.append({"board": board, "column": title,
                                "action": "colour", "color": color})
            for item in self._role_label_plan(provider, container.id):
                out.append({
                    "board": board, "label": item["title"], "action": "recolour",
                    "from": item["from"], "color": item["to"],
                })
        return out

    def ensure_board_columns(self, slug: str) -> dict:
        """Apply BOARD_COLUMNS to EVERY board this project is linked to.

        Separate from `bootstrap` on purpose. Bootstrap styles a board WE just
        created, which is uncontroversial. This one touches a board that already
        existed and may be someone else's, so it is only ever run when asked -
        never on attach, and never on a sweep. It still cannot repaint: the
        provider fills a blank colour and leaves a chosen one alone.
        """
        links = get_project_links(slug)
        if not links:
            raise ProviderError(f"'{slug}' is not linked to a board")
        boards = []
        for link in links:
            name = link.get("label") or link["remote_work_package_id"]
            try:
                provider = self.provider_for(link)
                before = provider.fetch_container(link["remote_work_package_id"])
                after = self._ensure_columns(provider, before)
                relabelled = self._ensure_role_labels(provider, after.id)
            except Exception as e:  # noqa: BLE001 - a board that is gone or
                # unreachable is reported, never fatal to the rest.
                boards.append({"board": name, "error": str(e)})
                continue
            boards.append({
                "board": name,
                "columns": [
                    {"title": g.title, "color": g.color} for g in after.groups
                ],
                "added": [
                    g.title for g in after.groups
                    if g.title.strip().lower() not in {
                        b.title.strip().lower() for b in before.groups
                    }
                ],
                "labels_recoloured": [
                    {"title": r["title"], "from": r["from"], "to": r["to"]}
                    for r in relabelled
                ],
            })
        return {"boards": boards}

    def _role_label_plan(self, provider, container_id: str) -> list[dict]:
        """Role labels off-convention on one board; [] when unsupported."""
        if not getattr(provider, "role_label_plan", None):
            return []
        if not provider.capabilities.supports_labels:
            return []
        try:
            return provider.role_label_plan(container_id)
        except Exception:  # noqa: BLE001 - cosmetics never break a report
            return []

    def _ensure_role_labels(self, provider, container_id: str) -> list[dict]:
        """Repaint off-convention role labels on one board.

        Only `agent:` labels. An ordinary label's colour is somebody's choice
        and is left alone, the same way a column's chosen colour is.
        """
        if not getattr(provider, "ensure_role_label_colors", None):
            return []
        if not provider.capabilities.supports_labels:
            return []
        try:
            return provider.ensure_role_label_colors(container_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("could not fix role labels on %s: %s", container_id, e)
            return []

    def _ensure_columns(self, provider, container: Container) -> Container:
        """Give a board the BOARD_COLUMNS it is missing, with their colours.

        Best-effort and non-fatal: a board with the wrong columns is a cosmetic
        problem, and failing `bootstrap` over it would leave the project
        unlinked - much worse. Re-fetches only when something actually changed.

        The provider does the not-repainting: `ensure_group` fills in a blank
        colour and leaves a set one alone, so re-running this on a board whose
        columns someone has styled changes nothing.
        """
        if not provider.capabilities.supports_group_style:
            return container
        changed = False
        for title, color in BOARD_COLUMNS:
            existing = next(
                (g for g in container.groups
                 if g.title.strip().lower() == title.strip().lower()),
                None,
            )
            # Nothing to do for a column that is present and already coloured.
            if existing is not None and (existing.color or "").strip():
                continue
            try:
                provider.ensure_group(container.id, title, color)
                changed = True
            except Exception as e:  # noqa: BLE001 - never fail a bootstrap on cosmetics
                logger.warning(
                    "could not ensure column %r on %s: %s", title, container.id, e,
                )
        if not changed:
            return container
        try:
            return provider.fetch_container(container.id)
        except Exception:  # noqa: BLE001
            return container

    def attach(
        self, slug: str, *, work_package_id: str | None = None,
        external_ref: str | None = None, label: str | None = None,
        is_default: bool = True, match_paths: list | None = None,
        provider: str | None = None, backfill: bool = False,
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

        # Resolve against the platform this link will belong to, not the default:
        # attaching a Trello board must not look for it in asoode.
        impl = self.provider_for({"provider": provider} if provider else None)
        if work_package_id:
            container = impl.fetch_container(work_package_id)
        else:
            found = impl.find_container(external_ref)
            if not found:
                raise ProviderError(
                    f"no board with externalRef {external_ref!r} is visible to this "
                    "credential. `memory-mcp asoode boards` lists what is."
                )
            container = impl.fetch_container(found.id)

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
            provider=impl.name,
        )
        return {
            "link": link,
            "work_package": {"id": package_id, "title": container.title,
                             "external_ref": container.external_ref},
            "project": {"id": project_id},
            "lists": [{"id": g.id, "title": g.title} for g in container.groups],
            "url": f"{endpoints.app_url}/projects/{project_id}",
            "created": False,
            **self._backfill_offer(slug, backfill),
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
            # Keep going while there is more and progress is being made. A row
            # enqueued while a batch was in flight used to wait for the NEXT
            # mutation to nudge the flusher - done() queues two rows back to
            # back and hit that every time.
            total: dict = {"flushed": 0, "failed": 0, "skipped": 0, "abandoned": 0}
            for _ in range(_MAX_FLUSH_PASSES):
                result = self._flush_locked(slug, limit)
                for key in ("flushed", "failed", "skipped", "abandoned"):
                    total[key] += result.get(key, 0)
                if result.get("reason"):
                    total["reason"] = result["reason"]
                if result.get("failed") or not result.get("remaining"):
                    break
            total["remaining"] = self._outbox.depth(slug)
            return total

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
            except TransientProviderError as e:
                # An outage is not the row's fault: keep it, note the error,
                # spend no attempt. The change is still pending, not failing.
                self._outbox.fail(slug, row["id"], str(e), count=False)
                failed += 1
                break
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
        op = row["op"]
        payload = row.get("payload") or {}
        if op == "delete":
            # The local row is gone; the payload carries where the card lives.
            return self._flush_delete(slug, payload)
        try:
            task = self._tasks.get(slug, row["task_id"])
        except Exception:  # noqa: BLE001 - deleted locally before the flush ran
            return False
        link = self.route(slug, task)
        if link is None:
            return False
        provider = self.provider_for(link)
        caps = provider.capabilities
        container_id = link["remote_work_package_id"]

        remote_id, created_here = self._ensure_remote(slug, task, link, provider)
        # Everything from here on is a write WE make, and asoode will broadcast
        # it straight back to us. Noted before the call so the echo cannot beat
        # the record home.
        self.echo.note(remote_id)

        if op == "role":
            if created_here or not caps.supports_labels:
                return created_here  # a new card already carries its role
            provider.set_role_label(remote_id, container_id, task.role)
            return True
        if op == "archive":
            if not caps.supports_archive:
                # Keep the local archive, send nothing. A platform without the
                # feature must not fail a local operation.
                return False
            provider.archive(remote_id, bool(payload.get("archived", True)))
            return True
        if op == "attachment":
            return self._flush_attachments(slug, task, link, remote_id)
        if op == "detach":
            if not caps.supports_attachments or not payload.get("filename"):
                return False
            provider.remove_attachment(remote_id, payload["filename"])
            return True
        if op == "time":
            return self._flush_time(slug, task, link, remote_id)
        if op == "comment":
            return self._flush_comments(slug, task, link, remote_id)
        if op == "parent":
            if not caps.supports_subtasks or created_here:
                return created_here
            if payload.get("parent_id") is None:
                provider.promote(remote_id)
                return True
            return False  # re-parenting an existing card has no route yet
        if op == "update" and not created_here:
            changed = set(payload.get("changed") or [])
            self._apply_fields(task, link, provider, remote_id, changed, payload)
            if "state" in changed:
                self._apply_state(slug, task, link, provider, remote_id)
            return True
        # create / state, or an update on a card that was just created with
        # everything it has: reconcile the state and the column.
        if created_here:
            return True
        self._apply_state(slug, task, link, provider, remote_id)
        return True

    def _ensure_remote(self, slug: str, task, link: dict, provider) -> tuple[str, bool]:
        """The remote id for a task, creating the card when there is none.

        Returns (remote_id, created_here). create_task with the task's
        externalRef is BOTH create and lookup: asoode returns the existing row
        for a repeated ref, so recovering a lost mapping costs one call and can
        never duplicate. A card created here is then brought up to date with
        EVERY field the local task has - priority, dates, estimate, labels,
        assignee, role, state - because the create route only carries a title
        and a description.

        A sub-task's parent is created first when the board has not seen it
        yet, so nesting survives even if the parent's own row was lost.
        """
        # No outbox (a bare bridge in a test, or a push before Phase 2 wired
        # one): create-by-externalRef is still a lookup, just without a cache.
        remote_id = (
            self._outbox.remote_id(slug, task.id, link["id"]) if self._outbox else None
        )
        if remote_id:
            return remote_id, False

        parent_remote = None
        if task.parent_id and provider.capabilities.supports_subtasks:
            try:
                parent = self._tasks.get(slug, task.parent_id)
                parent_remote, _ = self._ensure_remote(slug, parent, link, provider)
            except Exception:  # noqa: BLE001 - parent gone; create it flat
                parent_remote = None

        state_map = link.get("state_list_map") or {}
        list_id = state_map.get(task.state.value) or link.get("default_list_id")
        remote = provider.create_task(
            link["remote_work_package_id"], list_id, task.title,
            description=task.description or "",
            external_ref=f"memory-mcp:{task.id}",
            parent_id=parent_remote,
        )
        remote_id = remote.id
        if not remote_id:
            raise ProviderError("the provider returned a task without an id")
        self.echo.note(remote_id)
        # Fields first, mapping second. Remembered before the sync, a card
        # whose field sync failed was found on the retry, reported as "already
        # there", and never got its fields - the mirror looked healthy and the
        # priority, dates, labels and assignee were simply gone. Unremembered,
        # the retry creates-by-externalRef again (a lookup) and syncs again;
        # every call in the sync is idempotent, so landing twice is harmless.
        self._sync_new_card(slug, task, link, provider, remote_id)
        self._remember(slug, task.id, link["id"], remote_id, task.state.value)
        return remote_id, True

    def _remember(self, slug: str, task_id: str, link_id: int, remote_id: str,
                  state: str) -> None:
        if self._outbox is not None:
            self._outbox.remember(slug, task_id, link_id, remote_id, state)

    def _sync_new_card(self, slug: str, task, link: dict, provider, remote_id: str) -> None:
        """Send everything a freshly created card is missing."""
        caps = provider.capabilities
        container_id = link["remote_work_package_id"]
        fields = {}
        if task.priority:
            fields["priority"] = task.priority
        for key in ("due_at", "begin_at", "end_at"):
            if getattr(task, key) is not None:
                fields[key] = getattr(task, key)
        if task.estimated_minutes:
            fields["estimated_minutes"] = task.estimated_minutes
        if fields and caps.supports_fields:
            provider.update_fields(remote_id, fields)
        if task.labels and caps.supports_labels:
            provider.sync_labels(remote_id, container_id, list(task.labels), [])
        if task.assignee and caps.supports_assignees:
            provider.set_assignee(remote_id, container_id, task.assignee, None)
        if task.role and caps.supports_labels:
            provider.set_role_label(remote_id, container_id, task.role)
        if task.state.value != "todo":
            self._apply_state(slug, task, link, provider, remote_id)
        if task.archived_at is not None and caps.supports_archive:
            provider.archive(remote_id, True)

    def _apply_fields(
        self, task, link: dict, provider, remote_id: str, changed: set, payload: dict,
    ) -> None:
        """Send the fields an update changed, from the task's CURRENT values so
        several queued updates converge on what the local store holds now."""
        caps = provider.capabilities
        container_id = link["remote_work_package_id"]
        keys = _FIELD_KEYS & changed
        if keys and caps.supports_fields:
            provider.update_fields(remote_id, {k: getattr(task, k) for k in keys})
        if "labels" in changed and caps.supports_labels:
            before = set(payload.get("labels_before") or [])
            now = set(task.labels)
            add, remove = sorted(now - before), sorted(before - now)
            if add or remove:
                provider.sync_labels(remote_id, container_id, add, remove)
        if "assignee" in changed and caps.supports_assignees:
            provider.set_assignee(
                remote_id, container_id, task.assignee, payload.get("assignee_before"),
            )

    def _apply_state(self, slug: str, task, link: dict, provider, remote_id: str) -> None:
        """Make the remote state - and the column - match the local one."""
        provider.set_state(remote_id, task.state.value)
        # asoode keeps state and column independent, so a Done card would sit in
        # To Do forever without this. Best-effort: the state is the truth, the
        # column is presentation, and failing to move it must not fail the flush.
        target_list = (link.get("state_list_map") or {}).get(task.state.value)
        if target_list and provider.capabilities.supports_groups:
            try:
                provider.move(remote_id, target_list)
            except ProviderError:
                pass
        self._remember(slug, task.id, link["id"], remote_id, task.state.value)

    def _flush_delete(self, slug: str, payload: dict) -> bool:
        """A task deleted locally: archive its card(s). asoode has no delete
        route, and a card that stays live for a task nobody has is the shape
        that re-imports itself."""
        remotes = payload.get("remote") or {}
        if not remotes:
            return False
        links = {link["id"]: link for link in get_project_links(slug)}
        sent = False
        for link_id, remote_id in remotes.items():
            link = links.get(int(link_id))
            if link is None or not remote_id:
                continue
            provider = self.provider_for(link)
            if not provider.capabilities.supports_archive:
                continue
            self.echo.note(remote_id)
            provider.archive(remote_id, True)
            sent = True
        return sent

    # ---------- inbound ----------

    def import_board(
        self, slug: str, link: dict, *, limit: int = 500, update_existing: bool = True,
    ) -> dict:
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
        # Suppress mirroring for the duration: these changes CAME FROM asoode.
        # Scoped to this context, never the shared service: blanking the
        # service's outbox dropped every concurrent tool call's mirror too.
        container = self.provider_for(link).fetch_container(
            link["remote_work_package_id"], with_tasks=True,
        )
        with self._tasks.suppress_mirroring():
            return self._import_rows(slug, link, container, limit, update_existing)

    def _import_rows(self, slug, link, container, limit, update_existing=True) -> dict:
        created, updated, skipped = [], [], 0
        # Fallback identity, built once per board. See _resolve_local. Archived
        # tasks included: one whose mapping was lost must match by title too,
        # or it comes back as a new open task.
        existing_by_title: dict[str, str] = {}
        for task in self._tasks.list_tasks(
            slug,
            TaskFilter(include_done=True, include_subtasks=True, include_archived=True),
            limit=1000,
        ).tasks:
            existing_by_title.setdefault(task.title.strip().lower(), task.id)

        for remote in list(container.tasks)[:limit]:
            title = (remote.title or "").strip()
            if not remote.id or not title:
                skipped += 1
                continue
            state = remote.state
            local_id = self._resolve_local(slug, link, remote, existing_by_title)
            if local_id is _DELETED_HERE:
                # Our own card, for a task deleted locally. Its archive is on
                # its way (or landed); importing it would resurrect the task.
                skipped += 1
                continue

            if local_id:
                if not update_existing:
                    # Reconcile mode: a task that already exists locally is left
                    # alone. Overwriting needs a two-sided conflict policy, which
                    # is undecided - guessing would discard local edits silently.
                    continue
                current = self._tasks.get(slug, local_id)
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
                source=TaskSource.ASOODE,
            ))
            if state != "todo":
                self._tasks.update(UpdateTaskRequest(
                    project=slug, task_id=task.id, state=TaskState(state),
                ))
            self._tasks.set_link(slug, task.id, link["id"])
            self._outbox.remember(slug, task.id, link["id"], remote.id, state)
            existing_by_title.setdefault(title.lower(), task.id)
            created.append({"task_id": task.id, "title": title, "state": state})

        return {
            "board": link.get("label"),
            "work_package_id": link["remote_work_package_id"],
            "created": created, "updated": updated, "skipped": skipped,
            "counts": {"created": len(created), "updated": len(updated)},
        }

    def _resolve_local(self, slug: str, link: dict, remote, existing_by_title: dict):
        """Which local task a remote one is, if any.

        THREE identities, in descending order of trust, because relying on only
        the first one duplicated 54 tasks across two projects:

        1. The stored task_sync mapping. Exact, but ABSENT for anything mirrored
           before that mapping was reliably written - and a missing mapping used
           to mean "this is new", which is how the duplicates happened.
        2. The remote externalRef, which for anything pushed from here is
           "memory-mcp:<local task id>" - a perfect identity when the platform
           returns it. asoode's board fetch does (work-packages.service.ts maps
           it), and it leaves archived cards out, so this is live on asoode and
           the tombstone check below is what keeps a deleted task from coming
           back through it.
        3. The title, within this board. Imperfect - two tasks can share one -
           but far better than creating a duplicate, which is unrecoverable
           without a human reading both.

        A match found by 2 or 3 BACKFILLS the mapping, so the gap that caused
        this heals itself the first time a board is read.
        """
        remote_id = remote.id
        mapped = self._outbox.local_id_for_remote(slug, link["id"], remote_id)
        if mapped:
            return mapped

        candidate = None
        ref = (remote.external_ref or "")
        if ref.startswith("memory-mcp:"):
            candidate = ref.split(":", 1)[1]
            if self._tombstoned(slug, candidate):
                return _DELETED_HERE
        if candidate is None:
            candidate = existing_by_title.get((remote.title or "").strip().lower())
        if not candidate:
            return None
        try:
            self._tasks.get(slug, candidate)
        except Exception:  # noqa: BLE001 - the title matched a task since deleted
            return None
        self._outbox.remember(slug, candidate, link["id"], remote_id, remote.state)
        return candidate

    def _tombstoned(self, slug: str, task_id: str) -> bool:
        repo = getattr(self._tasks, "_task_repo", None)
        if repo is None or not hasattr(repo, "is_tombstoned"):
            return False
        try:
            return bool(repo.is_tombstoned(slug, task_id))
        except Exception:  # noqa: BLE001
            return False

    def reconcile(self, slug: str) -> dict:
        """Pull tasks that exist remotely but NOT locally. Never overwrites.

        The safe half of the inbound direction, and safe by construction: a task
        that is not in the local store cannot have local edits to lose. Someone
        adding a task on the board therefore sees it in the session, without any
        conflict policy being needed.

        Updating a task that exists on BOTH sides is the other half, and it stays
        behind `import_all` (explicit) until the two-sided conflict policy is
        decided - last-write-wins would silently discard local work.
        """
        links = get_project_links(slug)
        if not links:
            return {"slug": slug, "imported": 0, "reason": "not linked"}
        imported, failed = 0, []
        for link in links:
            try:
                result = self.import_board(slug, link, update_existing=False)
                imported += result["counts"]["created"]
            except ProviderError as e:
                failed.append({"board": link.get("label"), "error": str(e)})
        return {"slug": slug, "imported": imported, "failed": failed}

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

    def _flush_attachments(self, slug: str, task, link: dict, remote_id: str) -> bool:
        """Upload every attachment on this task that has not been sent yet.

        Reads the LOCAL unmirrored rows and marks each as it lands - the same
        send-once discipline comments and time entries needed, and it matters
        more here: a repeated 5 MB screenshot costs storage on both sides, not
        just noise.
        """
        provider = self.provider_for(link)
        if not provider.capabilities.supports_attachments or self._attachments is None:
            return False
        pending = self._attachments.unmirrored(slug, task.id)
        if not pending:
            return False
        from pathlib import Path as _Path

        for row in pending:
            blob = _Path(row["path"])
            if not blob.is_file():
                # The blob is gone - a cleared data dir, a manual delete. Mark it
                # rather than retrying forever against a file that will not return.
                self._attachments.mark_mirrored(slug, row["id"])
                continue
            provider.attach(
                remote_id, row["filename"], blob.read_bytes(), row["content_type"],
            )
            self._attachments.mark_mirrored(slug, row["id"])
        return True

    def _flush_comments(self, slug: str, task, link: dict, remote_id: str) -> bool:
        """Send every comment on this task that has not been sent yet.

        Reads the LOCAL comments rather than the outbox row's payload. That is
        the whole fix: a payload is re-sent on every retry, and since no platform
        gives a comment an idempotency key - and asoode has no delete endpoint -
        a retry loop left the same comment on a task nine times. Marked one at a
        time as each lands, so a failure partway through cannot re-post what
        already went.
        """
        provider = self.provider_for(link)
        if not provider.capabilities.supports_comments:
            return False
        pending = self._outbox.unmirrored_comments(slug, task.id)
        if not pending:
            return False
        for comment in pending:
            provider.comment(remote_id, _comment_text(comment))
            self._outbox.mark_comment_mirrored(slug, comment["id"])
        return True

    def _flush_time(self, slug: str, task, link: dict, remote_id: str) -> bool:
        """Send every closed, unsent stretch of work for this task.

        Marked one at a time rather than in a batch: if the third of five fails,
        the first two must stay marked or the retry sends them again and the
        remote total drifts upward. Over-reporting time is worse than a delay.
        """
        provider = self.provider_for(link)
        if not provider.capabilities.supports_time_tracking:
            return False
        entries = self._outbox.unmirrored_time(slug, task.id)
        if not entries:
            return False
        for entry in entries:
            provider.log_time(remote_id, entry["begin_at"], entry["end_at"])
            self._outbox.mark_time_mirrored(slug, entry["id"])
        return True

    def _backfill_offer(self, slug: str, backfill: bool) -> dict:
        """What linking would move, and optionally move it.

        OFFERED, NOT AUTOMATIC. Linking a project that already has a long task
        history would otherwise push everything to a board someone just created,
        which is occasionally what is wanted and usually a surprise - and there
        is no bulk undo on the other side. So the default reports the count and
        waits to be asked.
        """
        link = get_default_project_link(slug)
        every = self._tasks.list_tasks(
            slug, TaskFilter(include_done=True, include_subtasks=True), limit=1000,
        ).tasks
        # Only what is genuinely NOT on the board. Counting every local task
        # would report "27 not mirrored" for a project whose 27 tasks are all
        # already there, which is worse than saying nothing.
        pending = [
            t for t in every
            if link is None
            or not self._outbox.remote_id(slug, t.id, link["id"])
        ] if self._outbox else every
        if not backfill:
            return {
                "backfill_available": len(pending),
                "backfill_hint": (
                    f"{len(pending)} existing task(s) are not on this board yet. "
                    "Re-run with backfill=True, or call memory_asoode_push, to "
                    "mirror them."
                ) if pending else None,
            }
        result = self.push(slug)
        return {
            "backfill_available": len(pending),
            "backfilled": result["counts"],
        }

    def boards(
        self, project_id: str | None = None, provider: str | None = None,
    ) -> list[dict]:
        """Every board a platform's credential can see - what to attach to."""
        impl = self.provider_for({"provider": provider} if provider else None)
        return [
            {"id": c.id, "title": c.title, "external_ref": c.external_ref,
             "project_id": c.space_id, "project_title": c.space_title,
             "provider": impl.name}
            for c in impl.list_containers(project_id)
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
            container = self.provider_for(link).fetch_container(
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
            try:
                provider = self.provider_for(link)
                # The flusher's own path: create-or-look-up by externalRef, a
                # new card gets every field, and the mapping is remembered.
                # Without the mapping a pushed task looks unmirrored to the
                # backfill count and NEW to reconcile - the 54-duplicate shape.
                remote_id, created = self._ensure_remote(slug, task, link, provider)
                self.echo.note(remote_id)
                # An existing card: carry the real state and column over.
                # Skipped for todo so a re-push of an unchanged list is cheap.
                if not created and task.state.value != "todo":
                    self._apply_state(slug, task, link, provider, remote_id)
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
