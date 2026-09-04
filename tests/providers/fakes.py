"""One fake platform, shared by every test that needs a provider.

Before this, four service tests each had their own stub shaped like whatever the
bridge happened to call at the time - so a change to the bridge broke four
unrelated fakes in four different ways. This one is held to the real contract by
tests/providers/test_protocol.py, which means a test using it is exercising a
provider that behaves like a real one rather than one that behaves like the
mock's author expected.
"""

from memory_mcp.providers import (
    Capabilities,
    Container,
    ContainerRef,
    Group,
    ProviderError,
    RemoteTask,
    SpaceRef,
)

STATES = (
    "todo", "in_progress", "done", "paused", "blocked",
    "cancelled", "duplicate", "incomplete", "blocker",
)

DEFAULT_GROUPS = (("l-backlog", "Backlog"), ("l-todo", "To Do"),
                  ("l-doing", "In Progress"), ("l-done", "Done"))


class FakeProvider:
    """A whole task platform in dictionaries.

    `fail` makes every call raise, for testing the unreachable path. `seed`
    installs a container with known ids, so a test can assert on "l-done" instead
    of chasing generated ones.
    """

    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self._spaces: dict[str, str] = {}
        self._containers: dict[str, dict] = {}
        self._tasks: dict[str, dict] = {}
        self.created_tasks: list[dict] = []
        self.states: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.time_logs: list[tuple[str, object, object]] = []
        self.attachments_sent: list[tuple[str, str, bytes, str | None]] = []
        self.archived: list[tuple[str, bool]] = []
        self.groups_archived: list[str] = []
        self.created_spaces: list[str] = []
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
            raise ProviderError(f"no task {task_id}")
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
            supports_attachments=True, supports_archive=True,
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
                           external_ref=t["ref"])
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

    def create_task(self, container_id, group_id, title, *, description="", external_ref=None):
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
        }
        self.created_tasks.append({"container_id": container_id, "list_id": group,
                                   "title": title, "external_ref": external_ref,
                                   "description": description})
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

    def log_time(self, task_id, begin, end=None):
        self._require_task(task_id)
        self.time_logs.append((task_id, begin, end))

    def attach(self, task_id, filename, content, content_type=None):
        self._require_task(task_id)
        self.attachments_sent.append((task_id, filename, content, content_type))

    def archive(self, task_id, archived=True):
        self._require_task(task_id)["archived"] = bool(archived)
        self.archived.append((task_id, bool(archived)))

    def archive_group(self, group_id):
        """Bulk: every task in the group, in one call - what asoode's
        lists/:id/archive-tasks does server-side with one updateMany."""
        for task_id, task in self._tasks.items():
            if task.get("group") == group_id and not task.get("archived"):
                task["archived"] = True
                self.archived.append((task_id, True))
        self.groups_archived.append(group_id)
