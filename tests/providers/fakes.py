"""One fake platform, shared by every test that needs a provider.

Before this, four service tests each had their own stub shaped like whatever the
bridge happened to call at the time - so a change to the bridge broke four
unrelated fakes in four different ways. This one is held to the real contract by
tests/providers/test_protocol.py, which means a test using it is exercising a
provider that behaves like a real one rather than one that behaves like the
mock's author expected.
"""

from datetime import timedelta, timezone

from memory_mcp.providers import (
    BulkResult,
    Capabilities,
    Container,
    ContainerRef,
    Group,
    ProviderError,
    ProviderNotFoundError,
    RemoteTask,
    RemoteTimeEntry,
    SpaceRef,
)

STATES = (
    "todo", "in_progress", "done", "paused", "blocked",
    "cancelled", "duplicate", "incomplete", "blocker",
)

DEFAULT_GROUPS = (("l-backlog", "Backlog"), ("l-todo", "To Do"),
                  ("l-doing", "In Progress"), ("l-done", "Done"))

_MILLISECOND = timedelta(milliseconds=1)


def board_instant(value):
    """What a board keeps of an instant it is sent: UTC, to the millisecond.

    A naive value is read as machine-local, exactly as asoode's adapter sends
    it - so a round trip through the fake crosses the same clock boundary a
    round trip through asoode does.
    """
    aware = value.astimezone(timezone.utc)
    return aware.replace(microsecond=aware.microsecond // 1000 * 1000)


def board_minutes(entries) -> int:
    """A card's total the way asoode computes it: closed stretches, in
    milliseconds, rounded half-up to whole minutes."""
    total = sum((entry.end - entry.begin) // _MILLISECOND for entry in entries)
    return (total + 30_000) // 60_000



class FakeProvider:
    """A whole task platform in dictionaries.

    `fail` makes every call raise, for testing the unreachable path. `seed`
    installs a container with known ids, so a test can assert on "l-done" instead
    of chasing generated ones.

    `with_bulk` turns on the bulk API - bulk, delete, archive_many and
    move_to_board - with asoode's tree semantics: remove deletes a card's
    sub-tasks, move carries them, archive applies to each. Off by default, so
    every existing test keeps exercising the per-card fallback.
    """

    def __init__(self, fail: Exception | None = None, with_bulk: bool = False):
        self.fail = fail
        self.with_bulk = with_bulk
        self.bulk_calls: list[tuple[str, tuple[str, ...]]] = []
        self.deleted: list[str] = []
        self.board_moves: list[tuple[str, str, str | None]] = []
        # Every remote write in call order, for tests about ORDER across kinds.
        self.calls: list[tuple] = []
        self._spaces: dict[str, str] = {}
        self._containers: dict[str, dict] = {}
        self._tasks: dict[str, dict] = {}
        self.created_tasks: list[dict] = []
        self.states: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.time_logs: list[tuple[str, object, object]] = []
        self.time_reads: list[str] = []
        self.attachments_sent: list[tuple[str, str, bytes, str | None]] = []
        self.archived: list[tuple[str, bool]] = []
        self.groups_archived: list[str] = []
        self.change_queries: list[object] = []
        self.role_labels: list[tuple] = []
        self.created_spaces: list[str] = []
        self.field_updates: list[tuple[str, dict]] = []
        self.label_syncs: list[tuple[str, str, list, list]] = []
        self.assignees: list[tuple[str, str | None, str | None]] = []
        self.promoted: list[str] = []
        self.attachments_removed: list[tuple[str, str]] = []
        self._n = 0

    # ---------- seeding ----------

    def seed(self, container_id="wp1", title="Board", space_id="p1",
             external_ref=None, groups=DEFAULT_GROUPS, tasks=()):
        """Install a container with fixed ids. Returns its ContainerRef."""
        self._spaces.setdefault(space_id, "Seeded Space")
        self._containers[container_id] = {
            "id": container_id, "title": title, "space": space_id,
            "ref": external_ref,
            "groups": [Group(id=g, title=t) for g, t in groups],
        }
        for task in tasks:
            tid = task["id"]
            self._tasks[tid] = {
                "id": tid, "container": container_id,
                "group": task.get("group_id", groups[0][0]),
                "title": task["title"], "description": task.get("description", ""),
                "state": task.get("state", "todo"), "ref": task.get("external_ref"),
                "time": list(task.get("time", ())),
            }
        return ContainerRef(id=container_id, title=title, external_ref=external_ref,
                            space_id=space_id)

    def _boom(self):
        if self.fail:
            raise self.fail

    def _next(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    def _require_container(self, container_id):
        self._boom()
        c = self._containers.get(container_id)
        if c is None:
            raise ProviderError(f"no container {container_id}")
        return c

    def _require_task(self, task_id):
        self._boom()
        t = self._tasks.get(task_id)
        if t is None:
            raise ProviderNotFoundError(f"no task {task_id}")
        return t

    # ---------- protocol ----------

    @property
    def name(self):
        return "fake"

    @property
    def capabilities(self):
        return Capabilities(
            supports_external_ref=True, supports_comments=True, supports_groups=True,
            supports_independent_state=True, supports_time_tracking=True,
            supports_time_readback=True,
            supports_attachments=True, supports_archive=True,
            supports_change_feed=True, supports_labels=True,
            supports_fields=True, supports_assignees=True, supports_subtasks=True,
            supports_bulk=self.with_bulk, supports_delete=self.with_bulk,
            supports_cross_board_move=self.with_bulk,
            states=STATES,
        )

    def list_spaces(self):
        self._boom()
        return [SpaceRef(id=i, title=t) for i, t in self._spaces.items()]

    def find_space(self, title):
        wanted = (title or "").strip().lower()
        for i, t in self._spaces.items():
            if t.strip().lower() == wanted:
                return SpaceRef(id=i, title=t)
        return None

    def create_space(self, title, *, description=""):
        self._boom()
        existing = self.find_space(title)
        if existing:
            return existing
        sid = self._next("s")
        self._spaces[sid] = title
        self.created_spaces.append(title)
        return SpaceRef(id=sid, title=title)

    def list_containers(self, space_id=None):
        self._boom()
        return [
            ContainerRef(id=c["id"], title=c["title"], external_ref=c["ref"],
                         space_id=c["space"], space_title=self._spaces.get(c["space"]))
            for c in self._containers.values()
            if space_id is None or c["space"] == space_id
        ]

    def find_container(self, external_ref):
        self._boom()
        for c in self._containers.values():
            if c["ref"] and c["ref"] == external_ref:
                return ContainerRef(id=c["id"], title=c["title"],
                                    external_ref=c["ref"], space_id=c["space"])
        return None

    def fetch_container(self, container_id, *, with_tasks=False):
        c = self._require_container(container_id)
        tasks = ()
        if with_tasks:
            tasks = tuple(
                RemoteTask(id=t["id"], title=t["title"], state=t["state"],
                           description=t["description"], group_id=t["group"],
                           external_ref=t["ref"],
                           minutes_spent=board_minutes(t.get("time", ())))
                for t in self._tasks.values() if t["container"] == container_id
            )
        return Container(id=c["id"], title=c["title"], external_ref=c["ref"],
                         space_id=c["space"], groups=tuple(c["groups"]), tasks=tasks)

    def create_container(self, title, *, description="", external_ref=None, space_id=None):
        self._boom()
        if external_ref:
            found = self.find_container(external_ref)
            if found:
                return self.fetch_container(found.id)
        cid = self._next("c")
        self._spaces.setdefault(space_id or "s0", "Default")
        self._containers[cid] = {
            "id": cid, "title": title, "space": space_id or "s0", "ref": external_ref,
            "groups": [Group(id=f"{cid}-{g}", title=t) for g, t in DEFAULT_GROUPS],
        }
        return self.fetch_container(cid)

    def create_task(self, container_id, group_id, title, *, description="", external_ref=None,
                    parent_id=None):
        self._require_container(container_id)
        if external_ref:
            for t in self._tasks.values():
                if t["ref"] == external_ref and t["container"] == container_id:
                    return RemoteTask(id=t["id"], title=t["title"], state=t["state"],
                                      description=t["description"], group_id=t["group"],
                                      external_ref=t["ref"])
        tid = self._next("r")
        group = group_id or self._containers[container_id]["groups"][0].id
        self._tasks[tid] = {
            "id": tid, "container": container_id, "group": group, "title": title,
            "description": description, "state": "todo", "ref": external_ref,
            "parent": parent_id, "labels": set(), "assignee": None,
        }
        self.created_tasks.append({"container_id": container_id, "list_id": group,
                                   "title": title, "external_ref": external_ref,
                                   "description": description, "parent_id": parent_id})
        return RemoteTask(id=tid, title=title, state="todo", description=description,
                          group_id=group, external_ref=external_ref)

    def set_state(self, task_id, state):
        task = self._require_task(task_id)
        if state not in STATES:
            raise ProviderError(f"unknown state {state!r}")
        task["state"] = state
        self.states.append((task_id, state))

    def move(self, task_id, group_id):
        self._require_task(task_id)["group"] = group_id
        self.moves.append((task_id, group_id))

    def comment(self, task_id, body):
        self._require_task(task_id)
        self.comments.append((task_id, body))

    def ensure_group(self, container_id, title, color=""):
        """Create the column if missing; colour it only when it has none."""
        container = self._containers[container_id]
        for group in container["groups"]:
            if group.title.strip().lower() == (title or "").strip().lower():
                if color and not (group.color or "").strip():
                    container["groups"] = [
                        Group(id=g.id, title=g.title, color=color)
                        if g.id == group.id else g
                        for g in container["groups"]
                    ]
                return group.id
        gid = self._next("g")
        container["groups"].append(Group(id=gid, title=title, color=color))
        return gid

    def log_time(self, task_id, begin, end=None):
        task = self._require_task(task_id)
        self.time_logs.append((task_id, begin, end))
        if end is None:
            return
        task.setdefault("time", []).append(RemoteTimeEntry(
            id=self._next("ts"), begin=board_instant(begin), end=board_instant(end),
            manual=True,
        ))

    def time_entries(self, task_id):
        task = self._require_task(task_id)
        self.time_reads.append(task_id)
        return tuple(task.get("time", ()))

    def attach(self, task_id, filename, content, content_type=None):
        self._require_task(task_id)
        self.attachments_sent.append((task_id, filename, content, content_type))

    def archive(self, task_id, archived=True):
        self._require_task(task_id)["archived"] = bool(archived)
        self.archived.append((task_id, bool(archived)))
        self.calls.append(("archive", task_id, bool(archived)))

    def set_role_label(self, task_id, container_id, role):
        self._require_task(task_id)["role_label"] = role
        self.role_labels.append((task_id, container_id, role))

    def update_fields(self, task_id, fields):
        task = self._require_task(task_id)
        if fields.get("title"):
            task["title"] = fields["title"]
        if "description" in fields:
            task["description"] = fields["description"] or ""
        for key in ("priority", "due_at", "begin_at", "end_at", "estimated_minutes"):
            if key in fields:
                task[key] = fields[key]
        self.field_updates.append((task_id, dict(fields)))

    def sync_labels(self, task_id, container_id, add, remove):
        task = self._require_task(task_id)
        labels = task.setdefault("labels", set())
        labels.difference_update(remove)
        labels.update(add)
        self.label_syncs.append((task_id, container_id, list(add), list(remove)))

    def set_assignee(self, task_id, container_id, assignee, previous=None):
        self._require_task(task_id)["assignee"] = assignee
        self.assignees.append((task_id, assignee, previous))

    def promote(self, task_id):
        self._require_task(task_id)["parent"] = None
        self.promoted.append(task_id)
        self.calls.append(("promote", task_id))

    # ---------- bulk (only when with_bulk) ----------

    def _tree(self, root_id):
        """The card and every descendant, root first."""
        tree, frontier = [root_id], [root_id]
        while frontier:
            children = [tid for tid, t in self._tasks.items()
                        if t.get("parent") in frontier and tid not in tree]
            tree.extend(children)
            frontier = children
        return tree

    def bulk(self, task_ids, action, *, state=None, container_id=None, group_id=None,
             dry_run=False):
        self._boom()
        if not self.with_bulk:
            raise ProviderError("bulk is not enabled on this fake")
        ids = tuple(dict.fromkeys(task_ids))
        self.bulk_calls.append((action, ids))
        self.calls.append(("bulk", action, ids))
        succeeded, failed, handled = [], [], set()
        for root in ids:
            if root in handled:
                continue
            if root not in self._tasks or (
                action == "move" and container_id not in self._containers
            ):
                failed.append(root)
                continue
            tree = [tid for tid in self._tree(root) if tid not in handled]
            handled.update(tree)
            succeeded.extend(tree)
            if not dry_run:
                self._apply_bulk(action, root, tree, state, container_id, group_id)
        return BulkResult(
            matched=ids, succeeded=() if dry_run else tuple(succeeded),
            failed=tuple(failed), affected_count=len(succeeded), dry_run=dry_run,
        )

    def _apply_bulk(self, action, root, tree, state, container_id, group_id):
        match action:
            case "remove":
                for tid in tree:
                    del self._tasks[tid]
                    self.deleted.append(tid)
            case "move":
                group = group_id or self._containers[container_id]["groups"][0].id
                for tid in tree:
                    self._tasks[tid]["container"] = container_id
                self._tasks[root]["group"] = group
                self.board_moves.append((root, container_id, group_id))
            case "archive" | "unarchive":
                for tid in tree:
                    self._tasks[tid]["archived"] = action == "archive"
                    self.archived.append((tid, action == "archive"))
            case "set_state":
                for tid in tree:
                    self._tasks[tid]["state"] = state
                    self.states.append((tid, state))
            case _:
                raise ProviderError(f"unknown bulk action {action!r}")

    def delete(self, task_ids):
        return self.bulk(task_ids, "remove")

    def archive_many(self, task_ids, archived=True):
        return self.bulk(task_ids, "archive" if archived else "unarchive")

    def move_to_board(self, task_id, container_id, group_id=None):
        result = self.bulk((task_id,), "move", container_id=container_id, group_id=group_id)
        if task_id not in result.succeeded:
            raise ProviderError(f"could not move {task_id} to {container_id}")

    def remove_attachment(self, task_id, filename):
        self._require_task(task_id)
        self.attachments_removed.append((task_id, filename))

    def changed_containers_since(self, since):
        """Everything, always - a fake has no clock worth trusting."""
        self.change_queries.append(since)
        return set(self._containers), "watermark"

    def archive_group(self, group_id):
        """Bulk: every task in the group, in one call - what asoode's
        lists/:id/archive-tasks does server-side with one updateMany."""
        for task_id, task in self._tasks.items():
            if task.get("group") == group_id and not task.get("archived"):
                task["archived"] = True
                self.archived.append((task_id, True))
        self.groups_archived.append(group_id)
