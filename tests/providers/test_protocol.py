"""The protocol, validated against a reference implementation.

An interface with no implementation is a guess. This in-memory provider is the
cheapest possible one, and running the conformance suite against it proves two
things before any real platform is touched: the protocol IS implementable, and
the suite itself is coherent.

It is also the answer to "what does a provider have to do?" - deliberately kept
small enough to read in one go.
"""

import pytest

from memory_mcp.providers import (
    Capabilities,
    Container,
    ContainerRef,
    Group,
    ProviderError,
    RemoteTask,
)
from tests.providers.conformance import ProviderConformance

STATES = (
    "todo", "in_progress", "done", "paused", "blocked",
    "cancelled", "duplicate", "incomplete", "blocker",
)


class FakeProvider:
    """A whole task platform in a dict. Groups mirror asoode's Kanban columns."""

    def __init__(self):
        self._containers: dict[str, dict] = {}
        self._tasks: dict[str, dict] = {}
        self._comments: list[tuple[str, str]] = []
        self._n = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_external_ref=True, supports_comments=True,
            supports_groups=True, supports_independent_state=True, states=STATES,
        )

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def _require_container(self, container_id: str) -> dict:
        container = self._containers.get(container_id)
        if container is None:
            raise ProviderError(f"no container {container_id}")
        return container

    def _require_task(self, task_id: str) -> dict:
        task = self._tasks.get(task_id)
        if task is None:
            raise ProviderError(f"no task {task_id}")
        return task

    # ---------- discovery ----------

    def list_containers(self, space_id=None):
        return [
            ContainerRef(id=c["id"], title=c["title"], external_ref=c["ref"])
            for c in self._containers.values()
            if space_id is None or c.get("space") == space_id
        ]

    def find_container(self, external_ref):
        for c in self._containers.values():
            if c["ref"] and c["ref"] == external_ref:
                return ContainerRef(id=c["id"], title=c["title"], external_ref=c["ref"])
        return None

    def fetch_container(self, container_id, *, with_tasks=False):
        c = self._require_container(container_id)
        tasks = ()
        if with_tasks:
            tasks = tuple(
                RemoteTask(
                    id=t["id"], title=t["title"], state=t["state"],
                    description=t["description"], group_id=t["group"],
                    external_ref=t["ref"],
                )
                for t in self._tasks.values() if t["container"] == container_id
            )
        return Container(
            id=c["id"], title=c["title"], external_ref=c["ref"],
            groups=tuple(c["groups"]), tasks=tasks,
        )

    # ---------- writes ----------

    def create_container(self, title, *, description="", external_ref=None, space_id=None):
        if external_ref:
            existing = self.find_container(external_ref)
            if existing:                       # idempotent, like the real ones
                return self.fetch_container(existing.id)
        cid = self._next("c")
        self._containers[cid] = {
            "id": cid, "title": title, "ref": external_ref, "space": space_id,
            "groups": [
                Group(id=f"{cid}-todo", title="To Do"),
                Group(id=f"{cid}-doing", title="In Progress"),
                Group(id=f"{cid}-done", title="Done"),
            ],
        }
        return self.fetch_container(cid)

    def create_task(self, container_id, group_id, title, *, description="", external_ref=None):
        self._require_container(container_id)
        if external_ref:
            for t in self._tasks.values():
                if t["ref"] == external_ref and t["container"] == container_id:
                    return RemoteTask(
                        id=t["id"], title=t["title"], state=t["state"],
                        description=t["description"], group_id=t["group"],
                        external_ref=t["ref"],
                    )
        tid = self._next("t")
        self._tasks[tid] = {
            "id": tid, "container": container_id, "group": group_id, "title": title,
            "description": description, "state": "todo", "ref": external_ref,
        }
        return RemoteTask(id=tid, title=title, state="todo", description=description,
                          group_id=group_id, external_ref=external_ref)

    def set_state(self, task_id, state):
        task = self._require_task(task_id)
        if state not in STATES:
            raise ProviderError(f"unknown state {state!r}")
        task["state"] = state

    def move(self, task_id, group_id):
        self._require_task(task_id)["group"] = group_id

    def comment(self, task_id, body):
        self._require_task(task_id)
        self._comments.append((task_id, body))


class TestFakeConformance(ProviderConformance):
    @pytest.fixture
    def provider(self):
        return FakeProvider()


class TestTheInterfaceItself:
    """Properties of the contract, not of any one implementation."""

    def test_capabilities_is_immutable(self):
        caps = Capabilities()
        with pytest.raises(Exception):
            caps.supports_comments = True

    def test_defaults_are_the_conservative_reading(self):
        """An unset flag must mean 'cannot', so a provider has to opt in to a
        promise rather than inherit one it does not keep."""
        caps = Capabilities()
        assert caps.supports_external_ref is False
        assert caps.supports_comments is False
        assert caps.states == ()

    def test_auth_failure_is_distinguishable_from_a_normal_failure(self):
        """The flusher retries a ProviderError and surfaces an auth one - retrying
        a revoked token forever is the failure mode this prevents."""
        from memory_mcp.providers import ProviderAuthError

        assert issubclass(ProviderAuthError, ProviderError)
        assert not issubclass(ProviderError, ProviderAuthError)

    def test_remote_task_speaks_local_state_vocabulary(self):
        """No platform ordinal or status name may reach shared code."""
        from memory_mcp.models import TaskState

        assert RemoteTask(id="1", title="x").state in {s.value for s in TaskState}

    def test_a_container_carries_its_groups_not_a_lookup(self):
        c = Container(id="c1", title="Board", groups=(Group(id="g1", title="To Do"),))
        assert c.groups[0].title == "To Do"
