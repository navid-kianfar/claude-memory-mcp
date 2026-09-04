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


class TransientProviderError(ProviderError):
    """The platform could not be reached, or answered with a server error.

    Separate because retrying IS the fix: the flusher keeps the outbox row
    without counting the attempt. Before this, five mutations during an outage
    burned the five attempts a row gets and the pending change was dropped.
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
    #: Files can be attached to a remote task.
    supports_attachments: bool = False
    #: Time spent can be logged against a remote task. When False the flusher
    #: keeps the local entries and sends nothing, rather than losing them.
    supports_time_tracking: bool = False
    #: A task can be archived - taken off the board without being deleted.
    #: When False the flusher keeps the local archive and sends nothing,
    #: rather than failing a local operation on a remote shortcoming.
    supports_archive: bool = False
    #: The platform can say what changed since an instant, so a catch-up
    #: need not re-read every container. Without it the caller MUST fall
    #: back to the full sweep rather than silently syncing nothing.
    supports_change_feed: bool = False
    #: A task can carry labels/tags. Used to show WHICH AGENT a card is for,
    #: since no platform has a field for that, and to mirror the local labels.
    #: False means skip, not fail.
    supports_labels: bool = False
    #: Title, description, priority, planned dates and estimate can be changed
    #: on an EXISTING task (`update_fields`). Without it an edit after creation
    #: stays local, which is how a rename used to vanish silently.
    supports_fields: bool = False
    #: A task can be assigned to a person by name (`set_assignee`).
    supports_assignees: bool = False
    #: A task can be created under a parent and promoted out of one.
    supports_subtasks: bool = False
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
        parent_id: str | None = None,
    ) -> RemoteTask:
        """Create a task inside a container.

        `container_id` is required even when `group_id` alone would identify the
        destination: the bridge always knows the container, and requiring it here
        keeps a provider from having to reverse-look-up its parent.

        With `supports_external_ref`, repeating a create with the same ref MUST
        return the existing task rather than a duplicate - that property is what
        lets the flusher retry safely.

        `parent_id` is the REMOTE id of the parent, when `supports_subtasks`;
        a provider without sub-tasks ignores it rather than failing.
        """

    def update_fields(self, task_id: str, fields: dict) -> None:
        """Apply changed fields to an existing task, in OUR vocabulary.

        Keys: title, description, priority (0-3), due_at / begin_at / end_at
        (datetimes, None to clear), estimated_minutes (int, None to clear).
        Only the keys present are touched. Only called when `supports_fields`.
        A provider IGNORES a key it cannot hold rather than failing: the local
        edit has already happened, and a partial mirror beats none.
        """

    def sync_labels(
        self, task_id: str, container_id: str, add: list[str], remove: list[str],
    ) -> None:
        """Attach `add` and detach `remove`, both by title.

        Only called when `supports_labels`. Touches nothing outside the two
        lists, so a label a human put on the card by hand survives a local
        edit. Role labels (`agent:<role>`) are set_role_label's business and
        never appear here.
        """

    def set_assignee(
        self, task_id: str, container_id: str, assignee: str | None,
        previous: str | None = None,
    ) -> None:
        """Assign the task to a person named by a free string - an email, a
        username, a full name - or unassign when `assignee` is None.

        Only called when `supports_assignees`. The provider resolves the string
        against the container's members and does NOTHING when nobody matches:
        a name the platform does not know must not fail the local edit.
        `previous` is the assignee being replaced, so it can be removed.
        """

    def promote(self, task_id: str) -> None:
        """Make a sub-task a top-level task. Only called when `supports_subtasks`."""

    def remove_attachment(self, task_id: str, filename: str) -> None:
        """Remove every attachment on the task with that filename.

        Only called when `supports_attachments`. By filename because the upload
        route returns no id to remember; a provider that does return one may
        key on it instead.
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

    def attach(
        self, task_id: str, filename: str, content: bytes,
        content_type: str | None = None,
    ) -> None:
        """Attach a file to a remote task. Only called when `supports_attachments`.

        Content is BYTES, not a path: a provider must never reach into this
        server's filesystem layout, and every platform's upload is a multipart
        body anyway.
        """

    def archive(self, task_id: str, archived: bool = True) -> None:
        """Take a task off the board, or put it back. Only called when
        `supports_archive`.

        Takes the BOOLEAN rather than being one-way: the local store can
        un-archive, and a one-way call would make that unmirrorable, so the two
        sides would drift the moment anyone restored a task.
        """

    def set_role_label(self, task_id: str, container_id: str,
                       role: str | None) -> None:
        """Show on the remote task which agent role it is for.

        Only called when `supports_labels`. `role` of None clears it.

        Takes the CONTAINER because on some platforms - asoode among them - a
        label is an entity scoped to the board rather than a free string, so the
        provider has to resolve or create it there before attaching.
        """

    def changed_containers_since(self, since) -> tuple[set[str], str | None]:
        """Which containers have changed since `since`, and the new watermark.

        Only called when `supports_change_feed`. Returns (container_ids,
        watermark); the watermark is passed as the next `since`.

        Deliberately returns CONTAINERS rather than tasks: the caller already
        knows how to reconcile a container, and this only has to answer "which
        ones are worth looking at". An empty set means nothing changed, which is
        the whole point - the common answer should cost one call.
        """

    def archive_group(self, group_id: str) -> None:
        """Archive every task in one group/column, in a single call.

        Only called when `supports_archive`. A provider without a bulk route may
        implement it as a loop, but it must exist so callers need not choose.
        """

    def log_time(self, task_id: str, begin, end=None) -> None:
        """Record a stretch of work against a task. Only called when
        `supports_time_tracking`.

        `begin` and `end` are datetimes; `end` is None for a stretch still
        running, which a platform may reject - the flusher only sends CLOSED
        entries, because an open one has no duration to report and would have to
        be corrected later.
        """
