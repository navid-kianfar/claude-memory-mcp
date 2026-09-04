"""The contract every task platform implements.

WHY THIS EXISTS: the bridge grew asoode-shaped - asoode's OperationResult
envelope, its WorkPackageTaskState ordinals and its separate state/list fields
reach into code that has nothing to do with asoode. Adding Asana beside that would
duplicate the routing, the outbox, the idempotency and the state mapping, which
are the parts worth having exactly once.

THE SPLIT: everything about WHICH task goes WHERE and WHEN stays shared -
project_links routing, task_outbox and the flusher, task_sync, the session brief,
the local store. A provider owns only the transport and the vocabulary: how to
reach the platform, and how its words map to ours.

THE SHAPE, and the one thing that is not negotiable:

    space (optional)  ->  CONTAINER (required)  ->  group  ->  task

A task ALWAYS lives inside a container. asoode has no route attaching a task to a
project - `POST /tasks/:listId/create` is the only create path and a list exists
only inside a work package - and every other platform worth supporting has the
same level under a different name:

    asoode   project      -> work package -> list     -> task
    Asana    workspace    -> project      -> section  -> task
    Monday   workspace    -> board        -> group    -> item
    Trello   workspace    -> board        -> list     -> card
    Jira     -            -> project      -> status   -> issue

So `container_id` is required on every write path. A platform where the container
is implicit supplies a synthetic one rather than the interface making it optional
- an optional container would push "is there a board?" into the shared code,
which is exactly the asoode-shaped leak this replaces.

A provider is SYNCHRONOUS and may block: every call runs on the flusher's thread,
never on the one serving a tool call.
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class ProviderError(Exception):
    """A platform call failed: unreachable, rejected, or refused.

    Providers translate their own failure shapes into this, so the flusher's
    retry logic never has to know one platform's error envelope from another's.
    """


class ProviderAuthError(ProviderError):
    """The stored credential is missing, expired, revoked or refused.

    Separate because it is the one failure retrying cannot fix: the flusher
    surfaces it rather than queueing behind it forever.
    """


@dataclass(frozen=True)
class Capabilities:
    """What a platform can actually do, so shared code stops guessing.

    Read these instead of testing the provider's name. `supports_external_ref` is
    the important one: with it, "create" doubles as "look up", and re-sending a
    task is free. Without it the bridge must rely on task_sync's stored remote id
    and must never retry a create blindly.
    """

    #: A caller-supplied idempotency key that survives on the remote record.
    supports_external_ref: bool = False
    #: Comments can be posted onto a remote task.
    supports_comments: bool = False
    #: The container has named groups (columns/sections) worth mapping states to.
    supports_groups: bool = True
    #: A task carries a state independent of the group it sits in. False for
    #: Trello, where the list IS the state - moving the card is the state change.
    supports_independent_state: bool = True
    #: Local task states this platform can represent. A state outside this set is
    #: mapped to the nearest one by the provider, never dropped silently.
    states: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpaceRef:
    """The level ABOVE a container: asoode project, Asana workspace, Jira site.

    Optional in the model but real on most platforms, and `bootstrap` needs it -
    creating a board somewhere requires knowing where "somewhere" is. A platform
    with no such level returns a single synthetic space, for the same reason a
    platform with no groups returns one: shared code must never branch on whether
    a level exists.
    """

    id: str
    title: str


@dataclass(frozen=True)
class Group:
    """A column/section inside a container: asoode list, Trello list, Asana section."""

    id: str
    title: str


@dataclass(frozen=True)
class ContainerRef:
    """Enough to identify a container without fetching it - what `boards` lists."""

    id: str
    title: str
    external_ref: str | None = None
    space_id: str | None = None
    space_title: str | None = None


@dataclass(frozen=True)
class RemoteTask:
    """A task as the platform holds it, in OUR vocabulary.

    `state` is a local TaskState value, already translated by the provider -
    shared code must never see a platform's own ordinal or status name.
    """

    id: str
    title: str
    state: str = "todo"
    description: str = ""
    group_id: str | None = None
    external_ref: str | None = None


@dataclass(frozen=True)
class Container:
    """A container with its groups, and its tasks when they were fetched."""

    id: str
    title: str
    external_ref: str | None = None
    space_id: str | None = None
    groups: tuple[Group, ...] = ()
    tasks: tuple[RemoteTask, ...] = field(default=())
    #: A URL a human can open, when the platform has one.
    url: str | None = None


@runtime_checkable
class TaskProvider(Protocol):
    """One task platform.

    Every method raises ProviderError on failure and returns plain data. No
    method may return a platform-native structure: translating at this boundary
    is the entire point, and a leak here becomes an `if provider == "asoode"`
    somewhere else later.
    """

    @property
    def name(self) -> str:
        """Stable identifier stored in project_links.provider, e.g. "asoode"."""

    @property
    def capabilities(self) -> Capabilities:
        ...

    # ---------- spaces ----------

    def list_spaces(self) -> list[SpaceRef]:
        """Every space the credential can see. One synthetic entry when the
        platform has no such level."""

    def find_space(self, title: str) -> SpaceRef | None:
        """A space by exact title, case-insensitively, or None.

        By title rather than by ref because spaces are the level a human names
        and platforms rarely give them a caller-supplied key.
        """

    def create_space(self, title: str, *, description: str = "") -> SpaceRef:
        """Create a space. A platform without the level returns its synthetic one
        rather than raising - callers must not have to ask first."""

    # ---------- discovery ----------

    def list_containers(self, space_id: str | None = None) -> list[ContainerRef]:
        """Every container the credential can see. What the user attaches to."""

    def find_container(self, external_ref: str) -> ContainerRef | None:
        """Locate a container by its caller-supplied ref, or None."""

    def fetch_container(self, container_id: str, *, with_tasks: bool = False) -> Container:
        """One container, its groups, and optionally its tasks (for import)."""

    # ---------- writes ----------

    def create_container(
        self, title: str, *, description: str = "", external_ref: str | None = None,
        space_id: str | None = None,
    ) -> Container:
        """Create a container. Idempotent on `external_ref` where supported."""

    def create_task(
        self, container_id: str, group_id: str | None, title: str, *,
        description: str = "", external_ref: str | None = None,
    ) -> RemoteTask:
        """Create a task inside a container.

        `container_id` is required even when `group_id` alone would identify the
        destination: the bridge always knows the container, and requiring it here
        keeps a provider from having to reverse-look-up its parent.

        With `supports_external_ref`, repeating a create with the same ref MUST
        return the existing task rather than a duplicate - that property is what
        lets the flusher retry safely.
        """

    def set_state(self, task_id: str, state: str) -> None:
        """Set a task's state, given a local TaskState value.

        On a platform without independent state this moves the task to the group
        that represents the state, so callers never branch on the difference.
        """

    def move(self, task_id: str, group_id: str) -> None:
        """Move a task into a group. Presentation, not truth - a provider whose
        state and group are the same thing may implement this as a no-op after
        set_state, but must not fail."""

    def comment(self, task_id: str, body: str) -> None:
        """Post a comment. Only called when `supports_comments`."""
