"""Pydantic models for Memory MCP Server.

Domain models (Memory, ProjectInfo, Session) represent stored entities.
Request models (Store*Request, Search*Request) are inputs to service methods.
Response models wrap service outputs consistently.
"""

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class MemoryCategory(str, Enum):
    DECISION = "decision"
    SESSION = "session"
    SPRINT = "sprint"
    PROJECT_PLAN = "project_plan"
    ARCHITECTURE = "architecture"
    DEVOPS = "devops"
    MANDATORY_RULES = "mandatory_rules"
    FORBIDDEN_RULES = "forbidden_rules"
    DEVELOPER_DOCS = "developer_docs"
    FEEDBACK = "feedback"
    REFERENCE = "reference"


class TaskState(str, Enum):
    """Task lifecycle states.

    This vocabulary is asoode's WorkPackageTaskState, verbatim, so the Phase 2
    bridge maps names to ordinals with nothing lost in translation:
    todo 1, in_progress 2, done 3, paused 4, blocked 5, cancelled 6,
    duplicate 7, incomplete 8, blocker 9.
    """

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    PAUSED = "paused"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    DUPLICATE = "duplicate"
    INCOMPLETE = "incomplete"
    BLOCKER = "blocker"


# States that still want something from someone. Session start surfaces exactly
# these, so a task can only leave the brief by being finished, withdrawn, or
# archived - never by ageing out. `incomplete` counts as open: it means the work
# did not get finished, which is precisely the thing worth resurfacing.
OPEN_TASK_STATES = [
    TaskState.TODO, TaskState.IN_PROGRESS, TaskState.PAUSED,
    TaskState.BLOCKED, TaskState.BLOCKER, TaskState.INCOMPLETE,
]


class TaskSource(str, Enum):
    """Who put the task in the list."""

    USER = "user"
    CLAUDE = "claude"
    # Imported from an asoode board - created there by a person, not here.
    ASOODE = "asoode"


class TaskCommentKind(str, Enum):
    """What a comment IS, so a rule pinned to a task is not read as chatter."""

    NOTE = "note"
    RULE = "rule"
    DECISION = "decision"
    REMINDER = "reminder"


RULE_CATEGORIES = {MemoryCategory.MANDATORY_RULES, MemoryCategory.FORBIDDEN_RULES}

# Reserved project holding org-wide rules (server mode). Its approved rules are
# injected into every project's rule block. The "__" prefix guarantees it can
# never collide with a real folder-derived slug.
GLOBAL_PROJECT_SLUG = "__global__"


def is_global_project(slug: str | None) -> bool:
    return slug == GLOBAL_PROJECT_SLUG

RULE_TYPE_TO_CATEGORY = {
    "mandatory": MemoryCategory.MANDATORY_RULES,
    "forbidden": MemoryCategory.FORBIDDEN_RULES,
}


def rule_category(rule_type: str) -> MemoryCategory:
    """Map a 'mandatory'/'forbidden' rule_type to its MemoryCategory."""
    category = RULE_TYPE_TO_CATEGORY.get((rule_type or "").strip().lower())
    if category is None:
        raise ValueError(
            f"rule_type must be 'mandatory' or 'forbidden', got {rule_type!r}"
        )
    return category


# --- Domain Models ---


class Memory(BaseModel):
    id: str
    category: MemoryCategory
    title: str
    content: str
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict | None = None
    embedding: list[float] | None = None
    status: str = "active"
    priority: int = 0
    source: str | None = None
    related_ids: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    access_count: int = 0
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Rule approval lifecycle (server mode). Defaults keep local mode unchanged:
    # every memory is "approved" (enforced) and unattributed unless the server
    # governance flow sets these. approval_status is only consulted for rules.
    created_by: str | None = None
    approval_status: str = "approved"  # "approved" | "proposed" | "revoked"
    approved_by: str | None = None
    approved_at: datetime | None = None
    # Imported from another project and not yet rewritten for this one. A pending
    # memory is stored but deliberately invisible: it is kept out of the rule
    # block, search, session context and the git snapshot until an agent adapts
    # it, so another project's specifics can never quietly steer this one.
    pending: bool = False


class ProjectInfo(BaseModel):
    slug: str
    # Stable identity written into .claude-memory/manifest.json. Survives the
    # folder being moved or renamed; shared with teammates through git.
    project_uid: str | None = None
    display_name: str
    description: str | None = None
    created_at: datetime | None = None
    last_accessed: datetime | None = None
    db_path: str | None = None
    project_path: str | None = None  # source folder this project syncs with
    owner: str | None = None  # user id that owns this project (server mode)
    backend: str = "local"  # "local" (private, on this machine) | "remote" (org)
    remote_url: str | None = None  # org server base URL when backend == "remote"


class TemplateItem(BaseModel):
    id: int
    template_id: int
    category: MemoryCategory
    title: str
    content: str
    priority: int = 0


class Template(BaseModel):
    id: int
    name: str
    description: str | None = None
    created_at: datetime | None = None
    items: list[TemplateItem] = Field(default_factory=list)


class SessionRecord(BaseModel):
    id: str
    started_at: datetime
    ended_at: datetime | None = None
    summary: str | None = None
    memories_created: int = 0
    memories_accessed: int = 0


class ProvenanceEntry(BaseModel):
    id: int
    memory_id: str
    operation: str
    details: dict | None = None
    actor: str | None = None  # user id that performed the operation (server mode)
    created_at: datetime | None = None


class Task(BaseModel):
    """A queued requirement with its own lifecycle.

    Tasks are NOT memories and NOT a MemoryCategory: they live in their own
    DuckDB tables, which is what keeps them out of the git-committed
    .claude-memory/ snapshot no matter how many of them pile up.
    """

    id: str
    title: str
    description: str | None = None
    state: TaskState = TaskState.TODO
    priority: int = 0
    assignee: str | None = None
    labels: list[str] = Field(default_factory=list)
    # Planned window; the actual clock lives in TaskTimeEntry.
    due_at: datetime | None = None
    begin_at: datetime | None = None
    end_at: datetime | None = None
    estimated_minutes: int | None = None
    parent_id: str | None = None
    position: int = 0
    # Open string rather than an enum, matching Memory.source: a value written
    # by a newer build (Phase 2 adds "asoode") must still read back here.
    source: str = "user"
    # Which linked asoode board this task belongs to. None routes to the
    # project's default link - see TaskBridge.route.
    link_id: int | None = None
    # Phase 2: inbound items awaiting a decision, mirroring memories.pending.
    # Nothing sets it in Phase 1.
    triage: bool = False
    # Which agent role this task is for ("frontend", "backend", "e2e", ...).
    # None means anyone may claim it - see TaskService.claim_next.
    role: str | None = None
    # The multi-session claim: which session is holding this task, and until
    # when. NULL claimed_by means free. See TaskService.claim_next.
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    done_at: datetime | None = None
    archived_at: datetime | None = None


class TaskComment(BaseModel):
    id: str
    task_id: str
    body: str
    kind: str = "note"  # note | rule | decision | reminder
    author: str | None = None
    created_at: datetime | None = None


class TaskTimeEntry(BaseModel):
    """One stretch of work. `end_at is None` means the clock is still running."""

    id: str
    task_id: str
    begin_at: datetime
    end_at: datetime | None = None
    manual: bool = False
    # The memory session that clocked on, so a session end can stop exactly the
    # clocks it started. None for a clock started from the UI.
    session_id: str | None = None


# --- Request Models ---


class StoreMemoryRequest(BaseModel):
    project: str
    category: MemoryCategory
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    metadata: dict | None = None
    priority: int = Field(default=0, ge=0, le=3)
    source: str = "assistant"
    related_ids: list[str] = Field(default_factory=list)
    created_by: str | None = None  # user id of the proposer (server mode)
    pending: bool = False  # stored, but inert until adapted to this project


class UpdateMemoryRequest(BaseModel):
    project: str
    memory_id: str
    title: str | None = Field(default=None, min_length=1, max_length=500)
    content: str | None = None
    tags: list[str] | None = None
    metadata: dict | None = None
    status: str | None = None
    priority: int | None = Field(default=None, ge=0, le=3)
    related_ids: list[str] | None = None


class SearchRequest(BaseModel):
    project: str
    query: str = Field(min_length=1)
    category: MemoryCategory | None = None
    tags: list[str] | None = None
    status: str = "active"
    limit: int = Field(default=10, ge=1, le=100)
    min_similarity: float = Field(default=0.3, ge=0.0, le=1.0)
    token_budget: int | None = Field(default=None, ge=1)


class MemoryFilter(BaseModel):
    status: str = "active"
    category: str | None = None
    tags: list[str] | None = None
    # False (default) hides un-adapted imports, True shows only those, None both.
    pending: bool | None = False


class Pagination(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    sort_by: Literal["created_at", "updated_at", "title", "priority", "access_count", "category"] = "updated_at"
    sort_order: Literal["asc", "desc"] = "desc"


class CreateTaskRequest(BaseModel):
    project: str
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    priority: int = Field(default=0, ge=0, le=3)
    labels: list[str] = Field(default_factory=list)
    assignee: str | None = None
    due_at: datetime | None = None
    begin_at: datetime | None = None
    end_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=0)
    parent_id: str | None = None
    source: TaskSource = TaskSource.USER
    # Which agent role should pick this up. None means anyone.
    role: str | None = None
    # Names the asoode board this task belongs to: a link label, a work package
    # externalRef, or its id. Resolved to link_id before storage; None routes to
    # the project's default link.
    target: str | None = None


class UpdateTaskRequest(BaseModel):
    project: str
    task_id: str
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    state: TaskState | None = None
    priority: int | None = Field(default=None, ge=0, le=3)
    assignee: str | None = None
    labels: list[str] | None = None
    due_at: datetime | None = None
    begin_at: datetime | None = None
    end_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=0)
    position: int | None = None
    role: str | None = None


class TaskFilter(BaseModel):
    state: TaskState | None = None
    source: str | None = None
    parent_id: str | None = None
    # Sub-tasks belong to their parent, so the top-level list hides them the way
    # a board does; pass parent_id to list one task's children, or set this to
    # see everything flat.
    include_subtasks: bool = False
    # Finished and withdrawn work is out of the way by default; archived tasks
    # are hidden until explicitly asked for.
    include_done: bool = False
    include_archived: bool = False


# --- Response Models ---


class SearchHit(BaseModel):
    memory: Memory
    similarity: float
    relevance_score: float


class SearchResponse(BaseModel):
    results: list[SearchHit]
    total: int
    query: str


class SearchResponseTokenBudgeted(BaseModel):
    index: list[dict]
    details: list[SearchHit]
    total: int
    tokens_used: int
    has_more: bool
    query: str


class ListResponse(BaseModel):
    memories: list[Memory]
    total: int
    limit: int
    offset: int


class RulesResponse(BaseModel):
    mandatory_rules: list[Memory]
    forbidden_rules: list[Memory]
    total: int


class TaskRowMeta(BaseModel):
    """What a list row shows beyond the task's own columns."""

    comments: int = 0
    subtasks_total: int = 0
    subtasks_done: int = 0
    minutes_spent: int = 0
    running: bool = False
    attachments: int = 0


class TaskAttachment(BaseModel):
    """A file attached to a task. Bytes live on disk; this is the metadata."""

    id: str
    task_id: str
    filename: str
    content_type: str | None = None
    size_bytes: int = 0
    sha256: str
    created_at: datetime | None = None
    mirrored_at: datetime | None = None


class TaskDetail(BaseModel):
    task: Task
    comments: list[TaskComment] = Field(default_factory=list)
    time_entries: list[TaskTimeEntry] = Field(default_factory=list)
    subtasks: list[Task] = Field(default_factory=list)
    attachments: list[TaskAttachment] = Field(default_factory=list)
    minutes_spent: int = 0
    #: This task's own minutes PLUS every sub-task's. Equal to `minutes_spent`
    #: for a task with no children.
    #:
    #: A parent's work happens on its sub-tasks, so its own clock is legitimately
    #: 0 - and asoode shows exactly that, because time there belongs to the task
    #: it was worked on. Rolling the total up REMOTELY would double-count in
    #: asoode's package and project reports, which sum every task. So the roll-up
    #: lives here, on the read side, where it can be shown without corrupting
    #: anyone's totals. Decided with the user on 2026-09-05.
    minutes_spent_total: int = 0
    running: bool = False
    # Set only when a task was closed with no clock ever running: either the
    # stretch recovered from the state history, or the reason none could be.
    # Present so the agent SEES that a task went to Done at zero minutes,
    # instead of the close succeeding in silence, which is how 39% of done
    # tasks ended up with no time at all.
    time_note: dict | None = None


class TaskListResponse(BaseModel):
    tasks: list[Task]
    total: int
    # Open tasks matching the filter, i.e. what is still waiting.
    open_count: int = 0
    # Ids of tasks with a running clock. Not derivable from `state`: stopping
    # the clock leaves the state alone, on purpose.
    running_ids: list[str] = Field(default_factory=list)
    # Per-row counts (comments, sub-tasks, tracked time), keyed by task id.
    meta: dict[str, TaskRowMeta] = Field(default_factory=dict)


class SessionContext(BaseModel):
    session_id: str
    project: str
    mandatory_rules: list[Memory]
    forbidden_rules: list[Memory]
    last_session_summary: str | None = None
    active_sprint: list[Memory]
    recent_decisions: list[Memory]
    orphaned_sessions_closed: int = 0
    # Tasks a session that never came back was holding: released, and the clocks
    # it left running stopped, at this session's start.
    expired_claims_released: int = 0
    stale_clocks_stopped: int = 0
    # Rules imported from other projects, waiting to be rewritten for this one.
    # They are NOT in mandatory_rules/forbidden_rules above and are not in force.
    pending_adaptations: list[Memory] = Field(default_factory=list)
    pending_instructions: str | None = None
    # Requirements parked in the task list, open ones first. These are things
    # WAITING, not instructions for this session: they are surfaced so the user
    # can see the queue, and none of them is started unless the user asks.
    queued_tasks: list[Task] = Field(default_factory=list)
    task_instructions: str | None = None
    # Set only when the project is bound to an asoode board. Its presence is what
    # flips task_instructions from "surface these, start nothing" to "this board
    # is the work queue" - so the loop never has to be re-told per project.
    asoode: dict | None = None
