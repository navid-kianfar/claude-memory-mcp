"""FastMCP server - thin tool layer over the service container.

Each @mcp.tool() is a minimal wrapper:
1. Resolve the project (explicit > active > CWD-detected)
2. Build a request model from inputs
3. Call the service method
4. Return a dict response (or error)
"""

import os

from fastmcp import FastMCP

from memory_mcp.config import settings
from memory_mcp.container import container
from memory_mcp.context import (
    load_active_project, resolve_project, set_active_project,
)
from memory_mcp.enforcement import rules_digest
from memory_mcp.services.adaptation import adaptation_brief
from memory_mcp.exceptions import MemoryMCPError, MemoryNotFoundError
from memory_mcp.models import (
    CreateTaskRequest, MemoryCategory, StoreMemoryRequest, UpdateMemoryRequest,
    SearchRequest, MemoryFilter, Pagination, RULE_CATEGORIES, TaskFilter,
    TaskSource, TaskState, UpdateTaskRequest, rule_category,
)

# Load persisted state at startup
container.model_service.load_persisted()
load_active_project()

SERVER_INSTRUCTIONS = """\
This server gives Claude persistent, per-project memory: decisions, rules,
architecture notes, sprint goals, and session summaries.

ALWAYS, at the very start of a conversation that involves a project:
  1. Call memory_session_start(project="<slug>") (or memory_use first, then
     memory_session_start). This loads the project's mandatory rules, forbidden
     rules, last session summary, sprint goals, and recent decisions.
  2. Treat the returned mandatory_rules and forbidden_rules as BINDING for the
     whole conversation. If a request conflicts with a rule, say so rather than
     silently violating it.

DURING the conversation:
  - When a decision, architecture choice, or important context is established,
    store it with memory_store (categories: decision, architecture, devops,
    feedback, sprint, reference, developer_docs, project_plan).
  - When the user sets a rule ("always X", "never Y"), use memory_add_rule with
    rule_type 'mandatory' or 'forbidden'. Edit rules with memory_update_rule and
    remove them with memory_delete_rule.
  - Before significant work, if rules may have drifted out of context, call
    memory_get_rules to reload them. Many tool responses also include an
    "active_rules" reminder - keep honoring it.
  - Use memory_search to recall prior context before answering questions about
    past decisions.

TASKS are a separate thing from memories: a list of requirements the user has
parked. memory_session_start returns them as `queued_tasks`.
  - They are QUEUED, NOT INSTRUCTIONS. Surface what is waiting at the start of a
    session, then leave it alone. Do not start a task because it is in the list -
    the user decides what gets picked up and when.
  - When the user asks for one: memory_task_start, then memory_task_done.
    memory_task_stop only stops the clock and leaves the state alone.
  - "Add a task to do X" means memory_task_add(title="X") and nothing else -
    keep doing what you were doing. Recording a requirement is precisely how the
    user avoids interrupting the work in progress.
  - THE OTHER CASE: when you START work the user asked for, create a task for it
    and memory_task_start it immediately. "Tasks are parked requirements" is
    about not STARTING someone else's queued work - it is not a reason to leave
    your own in-flight work unrecorded. Work that lives only in the conversation
    is gone when the session ends, and on a linked project it never reaches the
    board. Comment as you go; memory_task_done when finished.
  - Out-of-scope work you noticed goes in with memory_task_add(source='claude').
  - Several sessions may share a project, so a task is taken by claiming it:
    memory_task_claim_next(session_id) ONLY when you have finished what you were
    doing. A busy session must not claim. memory_session_end releases claims.

WHEN A REQUEST HAS SEVERAL DELIVERABLES, record it before working it. Call
memory_task_plan(request, tasks) with the user's wording verbatim and one task
per SEPARABLE deliverable - something that could be committed or reviewed on its
own. "Add the endpoint, wire the UI and write the docs" is three tasks; then work
them top-down. The point is that the queue, not the transcript, holds what was
asked: if the session ends after the first one, the rest are still there.
  - Do NOT decompose a question, a lookup, an explanation, or a single change
    described in several clauses. That is one job - just do it, or use
    memory_task_add. Over-decomposition buries a board in rows nobody would plan
    around, which is worse than not decomposing at all.
  - Decompose by deliverable, never by step. Steps hang off a deliverable with
    parent_index.
  - Every task needs a description stating the requirement in full. The tool
    rejects one without it.

ASOODE is the task manager this server bridges to. A project can be BOUND to an
asoode work package (a board), and the binding changes what the task list means.
  - memory_asoode_status shows which asoode and whether a PAT is stored. The PAT
    is machine-wide: stored once, shared by every project, never per project and
    never asked for twice. If none is stored, tell the user to run
    `memory-mcp asoode set-pat` - do NOT accept a token in chat, because a PAT
    pasted into a message stays in the transcript.
  - memory_asoode_link creates or finds the asoode project + board for this
    project; pass asoode_project_id to put the board inside a project that
    already exists. memory_asoode_push mirrors the local task list onto it. Both
    are idempotent, so re-running pushes changes rather than duplicating.
  - `memory-mcp asoode open <slug>` opens that board in a browser already signed
    in, via asoode's /auth/token deep link.
  - WHEN A PROJECT IS BOUND, ITS BOARD IS THE WORK QUEUE. memory_session_start
    returns an `asoode` block and a brief saying to work it: take the
    highest-priority actionable task, memory_task_start it, mirror the state to
    asoode, comment as you go, memory_task_done it, then take the next. This
    deliberately inverts the "queued tasks are not instructions" rule above,
    which still governs every UNBOUND project. Do not auto-start tasks in state
    blocked/blocker/paused/cancelled, and stop to ask when the work needs a
    decision only the user can make.
  - A task must carry enough detail to be implemented without the conversation:
    give memory_task_add a description stating the requirement, the constraint
    and the files involved, and comment on the task as you learn things. A bare
    title loses exactly what the list exists to preserve.

AT THE END of the conversation, call memory_session_end with a summary so the
next session has continuity.
"""

mcp = FastMCP("memory-mcp", instructions=SERVER_INSTRUCTIONS)

# Server mode: require a valid bearer token on every MCP call. FastMCP wraps the
# /mcp endpoint in auth middleware when `mcp.auth` is set. In local mode (the
# default) this stays None and /mcp is mounted exactly as before - no auth.
if settings.server_mode:
    from memory_mcp.auth import RegistryTokenVerifier

    mcp.auth = RegistryTokenVerifier(
        base_url=f"http://{settings.daemon_hostname}:{settings.daemon_port}"
    )


# ---------- Helpers ----------


def _resolve(project: str | None) -> str:
    """Resolve project slug: explicit > active > CWD-detected. Raises if none.

    In server mode the daemon's own working directory is meaningless (clients are
    remote), so CWD auto-detection is disabled - callers pass project= or rely on
    their per-user active project.
    """
    cwd = None if settings.server_mode else os.getcwd()
    slug = resolve_project(project, cwd)
    if not slug:
        raise ValueError(
            "No project specified and none detected. "
            "Use memory_use('slug') to set active project, or pass project= explicitly."
        )
    return slug


def _remote(slug: str):
    """Return a RemoteBackend when this project is bound to a remote server, else
    None. When set, the tool must serve the project from the org server so its
    data never touches local storage (the gateway)."""
    try:
        proj = container.project_repo.get(slug)
    except Exception:  # noqa: BLE001
        return None
    if proj and proj.backend == "remote" and proj.remote_url:
        from memory_mcp.remote_backend import for_project

        return for_project(proj)
    return None


def _safe(fn):
    """Wrap a tool body with uniform error handling."""
    try:
        return fn()
    except MemoryMCPError as e:
        return {"error": str(e), "type": type(e).__name__}
    except ValueError as e:
        return {"error": str(e), "type": "ValueError"}
    except Exception as e:  # noqa: BLE001
        # Includes RemoteError from the gateway path.
        return {"error": str(e), "type": type(e).__name__}


# ---------- Version ----------


@mcp.tool()
def memory_asoode_link(
    project: str | None = None,
    project_title: str | None = None,
    board_title: str | None = None,
    asoode_project_id: str | None = None,
) -> dict:
    """Link this memory project to an asoode board, creating what is missing.

    Finds or creates the asoode project, then creates the work package that
    mirrors this project's task list, and remembers the pairing. Pass
    asoode_project_id to put the board inside a project that already exists
    instead of making a new one.

    Safe to re-run: the work package carries the memory project's stable uid as
    its externalRef, so a second call returns the same board rather than a
    duplicate. Creating anything on the user's asoode account is outward-facing
    though - ask before the first link, and say afterwards what was created.
    """
    def _run():
        slug = _resolve(project)
        return container.asoode_bridge.bootstrap(
            slug, project_title=project_title, board_title=board_title,
            reuse_project_id=asoode_project_id,
        )
    return _safe(_run)


@mcp.tool()
def memory_asoode_attach(
    external_ref: str | None = None,
    work_package_id: str | None = None,
    label: str | None = None,
    is_default: bool = True,
    provider: str | None = None,
    project: str | None = None,
) -> dict:
    """Link this project to an asoode board that ALREADY EXISTS. Creates nothing.

    Use this, not memory_asoode_link, whenever the boards are already set up -
    which is the normal case for a monorepo where each app has its own work
    package. memory_asoode_link CREATES a board, so running it there adds a
    duplicate beside the real ones.

    Identify the board by `external_ref` (its externalRef, e.g. "asoode-worker")
    or by `work_package_id`. memory_asoode_boards lists what is available.

    One project attaches to MANY boards, and they may be on DIFFERENT platforms:
    `provider` names which (memory_asoode_status lists them), defaulting to
    asoode. `is_default` picks the board a task with no explicit target routes
    to; promoting a link demotes the others.
    """
    def _run():
        slug = _resolve(project)
        return container.asoode_bridge.attach(
            slug, external_ref=external_ref, work_package_id=work_package_id,
            label=label, is_default=is_default, provider=provider,
        )
    return _safe(_run)


@mcp.tool()
def memory_asoode_boards(
    asoode_project_id: str | None = None, provider: str | None = None,
) -> dict:
    """List the boards a platform's credential can see, to pick one to attach to.

    Returns each board's id, title, externalRef and owning project. `provider`
    picks the platform (default asoode). Read-only.
    """
    def _run():
        return {
            "boards": container.asoode_bridge.boards(asoode_project_id, provider),
        }
    return _safe(_run)


@mcp.tool()
def memory_asoode_push(project: str | None = None, include_done: bool = True) -> dict:
    """Mirror this project's local tasks onto its linked asoode board.

    RARELY NEEDED NOW: a linked project mirrors automatically on every task
    create, update, completion and comment. Use this for a full reconciliation -
    after working offline, or to seed a board that was linked after the tasks
    already existed. Each task goes to ITS OWN board when the project has
    several.

    Each task carries its local id as externalRef, so asoode returns the task
    that already exists rather than creating a second one.

    STILL ONE-WAY, local -> asoode. Nothing reads asoode back, so a task created
    or edited in asoode does not reach the local list. Never tell the user the
    two sides are in sync.
    """
    def _run():
        slug = _resolve(project)
        return container.asoode_bridge.push(slug, include_done=include_done)
    return _safe(_run)


@mcp.tool()
def memory_asoode_import(project: str | None = None) -> dict:
    """Pull tasks FROM the linked asoode boards into this project's task list.

    For tasks created in asoode by a person - they have no externalRef, so the
    remote id is the identity, held in task_sync. Re-importing therefore updates
    rather than duplicating.

    IMPORT-ONLY, not a two-way sync: a remote change overwrites the local title
    and state, and local edits are not merged back from here. Never describe the
    two sides as "in sync" on the strength of this.
    """
    def _run():
        slug = _resolve(project)
        return container.asoode_bridge.import_all(slug)
    return _safe(_run)


@mcp.tool()
def memory_asoode_links(project: str | None = None) -> dict:
    """Show which asoode boards this memory project is linked to."""
    def _run():
        slug = _resolve(project)
        return {"slug": slug, "links": container.asoode_bridge.links(slug)}
    return _safe(_run)


@mcp.tool()
def memory_task_attach(
    task_id: str,
    path: str,
    filename: str | None = None,
    project: str | None = None,
) -> dict:
    """Attach a file on disk to a task, and mirror it to the linked board.

    USE THIS FOR EVIDENCE. A screenshot proving a fix works, a failing log, a
    generated report, a diff - anything the work produced that someone reading
    the task later would want to see. A file described in prose is not the same
    as the file.

    `path` is a local path; the bytes are COPIED into the task store, so a
    scratch file that gets cleaned up later is safe to attach. `filename` renames
    it for display. Attachments mirror to the remote task automatically on the
    next flush, once and only once, on every platform that supports them.

    Limits: 25 MB, and an empty file is refused.
    """
    def _run():
        slug = _resolve(project)
        attachment = container.task_service.attach(slug, task_id, path, filename)
        return {"status": "ok", "attachment": attachment.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_task_attachments(task_id: str, project: str | None = None) -> dict:
    """List the files attached to a task, with whether each has been mirrored."""
    def _run():
        slug = _resolve(project)
        return {
            "attachments": [
                a.model_dump(mode="json")
                for a in container.task_service.attachments(slug, task_id)
            ]
        }
    return _safe(_run)


@mcp.tool()
def memory_task_plan(
    request: str,
    tasks: list[dict],
    project: str | None = None,
) -> dict:
    """Record a multi-part request as an ordered set of tasks, then work them.

    CALL THIS FIRST when a request contains two or more SEPARABLE DELIVERABLES -
    things that could each be committed or reviewed on their own ("add the
    endpoint, wire the UI, write the docs" is three). The queue then holds what
    was asked for, so nothing is lost if the session ends after the first one.

    DO NOT call it for a question, a lookup, an explanation, or a single change
    described in several clauses - that is one job, and one job is
    memory_task_add or just doing it. A plan of fewer than 2 tasks is rejected,
    and more than 20 is over the cap: decompose by deliverable, never by step.
    Steps go under a parent task via parent_index.

    `request` is the user's wording, verbatim - it is stored on every task the
    plan produces, so what was actually asked survives later edits to a title.

    Each item in `tasks` takes:
      title           short imperative name
      description     REQUIRED - the requirement in full: what, why, the
                      constraint that shapes it, and the files or endpoints
                      involved. A bare title loses the detail the list exists for.
      priority        0-3, optional
      labels          list of strings, optional
      parent_index    index of an EARLIER item in this list, to hang a step off a
                      deliverable, optional

    List them in dependency order - the queue is worked top-down. They are
    mirrored to the asoode board immediately when the project is bound.
    """
    def _run():
        slug = _resolve(project)
        return container.task_planner.plan(slug, request, tasks)
    return _safe(_run)


@mcp.tool()
def memory_asoode_status() -> dict:
    """Show the asoode integration config: which server, and whether a PAT is stored.

    The endpoints default to asoode's hosted service, so nothing needs configuring
    unless this is an on-premise install. The PAT is stored once for the whole
    machine and shared by every project - it is never per-project, and never in
    the committed .claude-memory snapshot.

    Returns a FINGERPRINT of the token (prefix + last4), never the token itself.
    There is deliberately no tool to set it: a PAT pasted into a chat message
    lives on in the transcript. Direct the user to `memory-mcp asoode set-pat`
    (a hidden prompt) or the Integrations screen in the UI.
    """
    from memory_mcp import asoode
    from memory_mcp.providers import available

    status = asoode.status()
    # Which platforms this build can talk to. A link names one; asoode is the
    # default for every link written before the column was read.
    status["providers"] = available()
    return status


@mcp.tool()
def memory_version() -> dict:
    """Get the current version of the Memory MCP server and configuration."""
    from memory_mcp import __version__
    from memory_mcp.context import get_active_project

    return {
        "version": __version__,
        "model": settings.embedding_model,
        "model_preset": settings.model_preset,
        "embedding_dim": settings.embedding_dim,
        "data_dir": str(settings.data_dir),
        "active_project": get_active_project(),
    }


# ---------- Active Project ----------


@mcp.tool()
def memory_use(project: str) -> dict:
    """Set the active project. Subsequent tools use it by default."""
    set_active_project(project)
    return {"status": "ok", "active_project": project}


# ---------- Projects ----------


@mcp.tool()
def memory_init_project(
    slug: str,
    display_name: str,
    description: str | None = None,
    set_active: bool = True,
    project_path: str | None = None,
) -> dict:
    """Initialize a new project namespace (creates DuckDB + registers it).

    Pass project_path (the project's source folder) to enable git-synced
    memory: rules/decisions mirror to <project_path>/.claude-memory/.
    """
    def _run():
        project = container.project_service.init_project(
            slug, display_name, description, project_path,
        )
        result = {"status": "ok", "project": project.model_dump(mode="json")}
        if set_active:
            set_active_project(project.slug)
            result["active"] = True
        return result
    return _safe(_run)


@mcp.tool()
def memory_load_from_folder(path: str) -> dict:
    """Load a project from a local folder.

    The project name is taken from the folder's package.json ("name") or the
    folder name. If the folder already contains a portable .memory-mcp.duckdb
    it is attached as-is; otherwise the project is created and a CLAUDE.md, if
    present, is imported into memory. The project is auto-activated.
    """
    def _run():
        from memory_mcp.folder_import import load_project_from_folder
        return load_project_from_folder(path)
    return _safe(_run)


@mcp.tool()
def memory_link_folder(path: str, project: str | None = None) -> dict:
    """Bind a project to a source folder for git-synced memory.

    Once linked, the project's rules/decisions mirror to a committable
    <path>/.claude-memory/ snapshot - so memory travels with the code across
    devices and teammates via git push/pull.
    """
    def _run():
        slug = _resolve(project)
        info = container.project_service.link_folder(slug, path)
        return {"status": "ok", "project": info.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_rename_project(
    display_name: str,
    project: str | None = None,
    description: str | None = None,
) -> dict:
    """Rename a project (its display name) and optionally update its description."""
    def _run():
        slug = _resolve(project)
        info = container.project_service.update_project(
            slug, display_name=display_name, description=description,
        )
        return {"status": "ok", "project": info.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_list_projects() -> dict:
    """List all registered projects."""
    projects = container.project_service.list_all()
    return {"projects": [p.model_dump(mode="json") for p in projects]}


@mcp.tool()
def memory_project_info(project: str | None = None) -> dict:
    """Get detailed info for a project."""
    def _run():
        p = container.project_service.get(_resolve(project))
        return p.model_dump(mode="json")
    return _safe(_run)


# ---------- Memory CRUD ----------


@mcp.tool()
def memory_store(
    category: str,
    title: str,
    content: str,
    project: str | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    priority: int = 0,
    source: str = "assistant",
    related_ids: list[str] | None = None,
) -> dict:
    """Store a new memory with auto-embedding, summary, entity extraction, and TTL."""
    def _run():
        slug = _resolve(project)
        rb = _remote(slug)
        if rb:
            return rb.store(slug, category, title, content,
                            tags=tags or [], priority=priority,
                            metadata=metadata, source=source)
        req = StoreMemoryRequest(
            project=slug,
            category=MemoryCategory(category),
            title=title,
            content=content,
            tags=tags or [],
            metadata=metadata,
            priority=priority,
            source=source,
            related_ids=related_ids or [],
        )
        memory = container.memory_service.store(req)
        result = {"status": "ok", "memory": memory.model_dump(mode="json")}
        digest = rules_digest(req.project)
        if digest:
            result["active_rules"] = digest
        return result
    return _safe(_run)


@mcp.tool()
def memory_search(
    query: str,
    project: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    status: str = "active",
    limit: int = 10,
    min_similarity: float = 0.3,
    token_budget: int | None = None,
) -> dict:
    """Semantic search with composite relevance scoring."""
    def _run():
        slug = _resolve(project)
        rb = _remote(slug)
        if rb:
            return rb.list(slug, q=query, category=category,
                           status=status, limit=limit)
        req = SearchRequest(
            project=slug,
            query=query,
            category=MemoryCategory(category) if category else None,
            tags=tags,
            status=status,
            limit=limit,
            min_similarity=min_similarity,
            token_budget=token_budget,
        )
        response = container.search_service.search(req)
        result = response.model_dump(mode="json")
        digest = rules_digest(req.project)
        if digest:
            result["active_rules"] = digest
        return result
    return _safe(_run)


@mcp.tool()
def memory_recall(
    project: str | None = None,
    memory_id: str | None = None,
    title: str | None = None,
) -> dict:
    """Recall a specific memory by ID or exact title."""
    def _run():
        slug = _resolve(project)
        rb = _remote(slug)
        if rb:
            if not memory_id:
                raise ValueError("Recall by memory_id is required for remote projects")
            return rb.get_memory(slug, memory_id)
        memory = container.memory_service.recall(slug, memory_id, title)
        return {"memory": memory.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_update(
    memory_id: str,
    project: str | None = None,
    title: str | None = None,
    content: str | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    status: str | None = None,
    priority: int | None = None,
    related_ids: list[str] | None = None,
) -> dict:
    """Update an existing memory. Re-embeds if title/content changed."""
    def _run():
        slug = _resolve(project)
        rb = _remote(slug)
        if rb:
            fields = {
                k: v for k, v in {
                    "title": title, "content": content, "tags": tags,
                    "metadata": metadata, "status": status, "priority": priority,
                }.items() if v is not None
            }
            return rb.update_memory(slug, memory_id, fields)
        req = UpdateMemoryRequest(
            project=slug, memory_id=memory_id,
            title=title, content=content, tags=tags, metadata=metadata,
            status=status, priority=priority, related_ids=related_ids,
        )
        memory = container.memory_service.update(req)
        return {"status": "ok", "memory": memory.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_delete(
    memory_id: str,
    project: str | None = None,
    hard: bool = False,
    reason: str | None = None,
) -> dict:
    """Soft-delete (archive) or hard-delete a memory."""
    def _run():
        slug = _resolve(project)
        rb = _remote(slug)
        if rb:
            return rb.delete_memory(slug, memory_id, hard=hard)
        return container.memory_service.delete(
            slug, memory_id, hard=hard, reason=reason,
        )
    return _safe(_run)


@mcp.tool()
def memory_list(
    project: str | None = None,
    category: str | None = None,
    status: str = "active",
    tags: list[str] | None = None,
    sort_by: str = "updated_at",
    sort_order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List memories with filtering, sorting, and pagination."""
    def _run():
        slug = _resolve(project)
        rb = _remote(slug)
        if rb:
            return rb.list(slug, category=category, status=status,
                           limit=limit, offset=offset)
        filters = MemoryFilter(status=status, category=category, tags=tags)
        pagination = Pagination(
            limit=limit, offset=offset, sort_by=sort_by, sort_order=sort_order,
        )
        memories, total = container.memory_repo.list(slug, filters, pagination)
        return {
            "memories": [m.model_dump(mode="json") for m in memories],
            "total": total, "limit": limit, "offset": offset,
        }
    return _safe(_run)


# ---------- Provenance ----------


@mcp.tool()
def memory_provenance(memory_id: str, project: str | None = None) -> dict:
    """Get the full audit trail for a memory."""
    def _run():
        slug = _resolve(project)
        entries = container.provenance_repo.for_memory(slug, memory_id)
        return {
            "memory_id": memory_id,
            "provenance": [e.model_dump(mode="json") for e in entries],
            "total": len(entries),
        }
    return _safe(_run)


# ---------- Rules ----------


@mcp.tool()
def memory_get_rules(project: str | None = None) -> dict:
    """Get all mandatory and forbidden rules (direct SQL, cached)."""
    def _run():
        slug = _resolve(project)
        rb = _remote(slug)
        if rb:
            return rb.get_rules(slug)
        response = container.rules_service.get_rules(slug)
        return response.model_dump(mode="json")
    return _safe(_run)


def _load_rule(slug: str, rule_id: str):
    """Fetch a memory and confirm it is actually a rule."""
    existing = container.memory_repo.get_by_id(slug, rule_id)
    if existing is None or existing.category not in RULE_CATEGORIES:
        raise MemoryNotFoundError(f"Rule not found: {rule_id}")
    return existing


@mcp.tool()
def memory_add_rule(
    rule_type: str,
    title: str,
    content: str,
    project: str | None = None,
    priority: int = 2,
) -> dict:
    """Add a project rule. rule_type is 'mandatory' (always do) or 'forbidden'
    (never do). The rule is enforced in every future session."""
    def _run():
        slug = _resolve(project)
        rb = _remote(slug)
        if rb:
            return rb.add_rule(slug, rule_type, title, content, priority=priority)
        req = StoreMemoryRequest(
            project=slug,
            category=rule_category(rule_type),
            title=title,
            content=content,
            priority=priority,
            source="assistant",
        )
        memory = container.memory_service.store(req)
        return {"status": "ok", "rule": memory.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_update_rule(
    rule_id: str,
    project: str | None = None,
    title: str | None = None,
    content: str | None = None,
) -> dict:
    """Update an existing mandatory or forbidden rule by its id."""
    def _run():
        slug = _resolve(project)
        rb = _remote(slug)
        if rb:
            return rb.update_rule(slug, rule_id, title=title, content=content)
        _load_rule(slug, rule_id)
        req = UpdateMemoryRequest(
            project=slug, memory_id=rule_id, title=title, content=content,
        )
        memory = container.memory_service.update(req)
        return {"status": "ok", "rule": memory.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_delete_rule(
    rule_id: str,
    project: str | None = None,
    hard: bool = False,
) -> dict:
    """Delete a rule by its id. Soft-deletes (archives) unless hard=True."""
    def _run():
        slug = _resolve(project)
        rb = _remote(slug)
        if rb:
            return rb.delete_rule(slug, rule_id, hard=hard)
        _load_rule(slug, rule_id)
        return container.memory_service.delete(slug, rule_id, hard=hard)
    return _safe(_run)


@mcp.tool()
def memory_approve_rule(rule_id: str, project: str | None = None) -> dict:
    """Approve a proposed rule (server mode, admin only) so it becomes enforced.

    In local mode rules are always enforced, so this is only meaningful on a
    shared server where members propose rules for admin review.
    """
    def _run():
        slug = _resolve(project)
        rb = _remote(slug)
        if rb:
            return rb.approve_rule(slug, rule_id)
        _load_rule(slug, rule_id)
        memory = container.memory_service.approve_rule(slug, rule_id)
        return {"status": "ok", "rule": memory.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_revoke_rule(rule_id: str, project: str | None = None) -> dict:
    """Revoke a rule (server mode, admin only): it stops being enforced but is
    kept for audit and can be re-approved later."""
    def _run():
        slug = _resolve(project)
        rb = _remote(slug)
        if rb:
            return rb.revoke_rule(slug, rule_id)
        _load_rule(slug, rule_id)
        memory = container.memory_service.revoke_rule(slug, rule_id)
        return {"status": "ok", "rule": memory.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_add_rule_bulk(
    rule_type: str,
    title: str,
    content: str,
    projects: list[str] | None = None,
    priority: int = 2,
) -> dict:
    """Add one rule to many projects at once.

    rule_type is 'mandatory' or 'forbidden'. projects=None adds it to every
    registered project; otherwise pass a list of project slugs. Lets you push
    a rule to all your projects without doing it one by one.
    """
    def _run():
        category = rule_category(rule_type)
        slugs = projects or [p.slug for p in container.project_service.list_all()]
        results = []
        for slug in slugs:
            try:
                container.project_service.get(slug)
                container.memory_service.store(
                    StoreMemoryRequest(
                        project=slug, category=category, title=title,
                        content=content, priority=priority, source="assistant",
                    )
                )
                results.append({"project": slug, "status": "ok"})
            except Exception as e:  # noqa: BLE001
                results.append({"project": slug, "status": "error", "error": str(e)})
        added = sum(1 for r in results if r["status"] == "ok")
        return {"status": "ok", "added": added, "total": len(slugs), "results": results}
    return _safe(_run)


# ---------- Templates ----------


def _template_by_name(name: str):
    template = container.template_repo.get_by_name(name)
    if template is None:
        raise ValueError(f"Template not found: {name}")
    return template


@mcp.tool()
def memory_list_templates() -> dict:
    """List reusable rule/memory templates that can be applied to new projects."""
    def _run():
        templates = container.template_service.list_templates()
        return {"templates": [t.model_dump(mode="json") for t in templates]}
    return _safe(_run)


@mcp.tool()
def memory_create_template(name: str, description: str | None = None) -> dict:
    """Create a reusable template - a named set of default rules/memories that
    can be applied when creating new projects so they need not be re-typed."""
    def _run():
        template = container.template_service.create(name, description)
        return {"status": "ok", "template": template.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_add_template_rule(
    template: str,
    rule_type: str,
    title: str,
    content: str,
    priority: int = 2,
) -> dict:
    """Add a rule to a template (by template name). rule_type is 'mandatory' or
    'forbidden'."""
    def _run():
        tpl = _template_by_name(template)
        category = rule_category(rule_type)
        item = container.template_service.add_item(
            tpl.id, category.value, title, content, priority,
        )
        return {"status": "ok", "item": item.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_apply_template(template: str, project: str | None = None) -> dict:
    """Apply a template's rules/memories into a project (by template name)."""
    def _run():
        slug = _resolve(project)
        tpl = _template_by_name(template)
        result = container.template_service.apply(slug, tpl.id)
        return {"status": "ok", "template": result["template"], "applied": result["applied"]}
    return _safe(_run)


@mcp.tool()
def memory_import_rules(
    source_project: str,
    memory_ids: list[str],
    project: str | None = None,
    pending: bool = True,
) -> dict:
    """Copy selected rules/memories from another project into this one. Use
    memory_get_rules(source_project) first to get the ids to import.

    Imports arrive PENDING: stored, but kept out of the rule block, search and
    the git snapshot until they are rewritten for this project - another
    project's component names and paths must never silently become rules here.
    memory_session_start returns them with instructions; adapt each one with
    memory_adapt_pending (or drop it with memory_discard_pending). Pass
    pending=False only for text you know is already project-neutral."""
    def _run():
        slug = _resolve(project)
        result = container.memory_service.copy_memories(
            slug, source_project, memory_ids, pending=pending,
        )
        return {
            "status": "ok",
            "imported": result["imported"],
            "skipped": result["skipped"],
            "pending": result["pending"],
            "next_step": (
                "Adapt each import to this project with memory_adapt_pending "
                "before relying on it - memory_pending_list() shows them."
            ) if result["pending"] and result["imported"] else None,
        }
    return _safe(_run)


@mcp.tool()
def memory_pending_list(project: str | None = None) -> dict:
    """List imported memories still waiting to be adapted to this project.

    Each carries `metadata.imported_from` with the original text and its source
    project. None of them are in force until adapted."""
    def _run():
        slug = _resolve(project)
        pending = container.memory_service.list_pending(slug)
        return {
            "project": slug,
            "total": len(pending),
            "instructions": adaptation_brief(slug, pending),
            "pending": [m.model_dump(mode="json") for m in pending],
        }
    return _safe(_run)


@mcp.tool()
def memory_adapt_pending(
    memory_id: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    priority: int | None = None,
    project: str | None = None,
) -> dict:
    """Rewrite a pending import for THIS project and put it into force.

    Pass the adapted title/content: the principle kept, the source project's
    specifics (component names, paths, repo URLs, stack details) replaced with
    this project's own or removed. Ask the user rather than guessing when a rule
    cannot be translated without knowing something about this codebase. Clearing
    pending adds the memory to the rules in force and to the git snapshot."""
    def _run():
        slug = _resolve(project)
        memory = container.memory_service.adapt_pending(
            slug, memory_id, title, content, tags=tags, priority=priority,
        )
        return {"status": "ok", "memory": memory.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_discard_pending(
    memory_id: str, reason: str | None = None, project: str | None = None,
) -> dict:
    """Drop an imported memory that does not belong in this project."""
    def _run():
        slug = _resolve(project)
        return container.memory_service.discard_pending(slug, memory_id, reason)
    return _safe(_run)


# ---------- Tasks ----------
#
# The task list is how the user records a requirement WITHOUT interrupting a
# session: they add it, it waits, and the next session start surfaces it. Every
# tool below therefore treats a task as something QUEUED, never as an
# instruction - see services/task_brief.py for the brief that says so.
#
# Tasks are local-only in Phase 1: unlike memories, they have no counterpart on
# an org server, so these tools do not take the `_remote(slug)` gateway branch.
# When the asoode bridge lands, mirroring happens through an outbox below the
# service, not by routing the tool elsewhere.


@mcp.tool()
def memory_task_add(
    title: str,
    description: str | None = None,
    priority: int = 0,
    labels: list[str] | None = None,
    assignee: str | None = None,
    due_at: str | None = None,
    estimated_minutes: int | None = None,
    parent_id: str | None = None,
    source: str = "user",
    target: str | None = None,
    project: str | None = None,
) -> dict:
    """Record a task: either a requirement parked for later, or work starting now.

    TWO CASES, and confusing them is the common mistake:

    1. PARKING A REQUIREMENT. "Add a task to do X" means add it and CARRY ON with
       what you were doing - adding it is not permission to begin X. That is the
       point of the list: the user can record something mid-session without
       derailing the work in progress.

    2. RECORDING WORK YOU ARE STARTING NOW. When you begin something the user
       asked for, create the task and immediately memory_task_start it. Do NOT
       skip this because "tasks are for parked work" - work that exists only in
       the conversation is lost when the session ends, and on a linked project it
       never reaches the board where the user can see it. Comment on it as you
       go, and memory_task_done it when finished.

    The difference is whether YOU are about to do it, not whether it belongs in
    the list. Both belong in the list.

    source='user' (default) means the user asked for it - use this whenever the
    requirement came from them. source='claude' means you queued it yourself:
    out-of-scope work you noticed and are deliberately not doing now. Say that
    you queued it rather than acting on it.

    `target` names the asoode board this task belongs to, when the project is
    linked to several - a monorepo has one work package per app. Give a link
    label, a work package externalRef, or its id; memory_asoode_links lists
    them. Omit it and the task routes to the project's DEFAULT board, which is
    what keeps single-board projects working unchanged. A wrong name is
    rejected rather than guessed - a task silently landing on the wrong board is
    worse than a failed create.

    Dates are ISO 8601 strings. priority is 0-3.
    """
    def _run():
        slug = _resolve(project)
        req = CreateTaskRequest(
            project=slug,
            title=title,
            description=description,
            priority=priority,
            labels=labels or [],
            assignee=assignee,
            due_at=due_at,
            estimated_minutes=estimated_minutes,
            parent_id=parent_id,
            source=TaskSource(source),
            target=target,
        )
        task = container.task_service.create(req)
        return {
            "status": "ok",
            "task": task.model_dump(mode="json"),
            "note": (
                "Queued, not started. Continue with the current work unless the "
                "user asks for this one."
            ),
        }
    return _safe(_run)


@mcp.tool()
def memory_task_list(
    state: str | None = None,
    source: str | None = None,
    parent_id: str | None = None,
    include_subtasks: bool = False,
    include_done: bool = False,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
    project: str | None = None,
) -> dict:
    """List this project's tasks, what is still open first.

    Finished, cancelled and archived tasks are hidden unless asked for, and so
    are sub-tasks - they belong to their parent. Pass parent_id to list one
    task's sub-tasks, or include_subtasks=True for a flat list. `meta` carries per-task comment and
    sub-task counts, minutes tracked, and whether a clock is running. These are
    queued requirements: report them, do not start them.
    """
    def _run():
        slug = _resolve(project)
        filters = TaskFilter(
            state=TaskState(state) if state else None,
            source=source,
            parent_id=parent_id,
            include_subtasks=include_subtasks,
            include_done=include_done,
            include_archived=include_archived,
        )
        result = container.task_service.list_tasks(slug, filters, limit, offset)
        return {
            "project": slug,
            "total": result.total,
            "open": result.open_count,
            "limit": limit,
            "offset": offset,
            "running": result.running_ids,
            "meta": {tid: m.model_dump(mode="json") for tid, m in result.meta.items()},
            "tasks": [t.model_dump(mode="json") for t in result.tasks],
        }
    return _safe(_run)


@mcp.tool()
def memory_task_get(task_id: str, project: str | None = None) -> dict:
    """One task with its comments, time entries, and minutes spent so far."""
    def _run():
        slug = _resolve(project)
        return container.task_service.detail(slug, task_id).model_dump(mode="json")
    return _safe(_run)


@mcp.tool()
def memory_task_update(
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    state: str | None = None,
    priority: int | None = None,
    assignee: str | None = None,
    labels: list[str] | None = None,
    due_at: str | None = None,
    begin_at: str | None = None,
    end_at: str | None = None,
    estimated_minutes: int | None = None,
    project: str | None = None,
) -> dict:
    """Change a task. Only the fields you pass are touched.

    state is one of: todo, in_progress, done, paused, blocked, cancelled,
    duplicate, incomplete, blocker. Setting it to done stamps done_at; moving it
    back off done clears it. begin_at/end_at are the PLANNED window - actual
    time comes from memory_task_start / memory_task_stop.
    """
    def _run():
        slug = _resolve(project)
        req = UpdateTaskRequest(
            project=slug,
            task_id=task_id,
            title=title,
            description=description,
            state=TaskState(state) if state else None,
            priority=priority,
            assignee=assignee,
            labels=labels,
            due_at=due_at,
            begin_at=begin_at,
            end_at=end_at,
            estimated_minutes=estimated_minutes,
        )
        task, changed = container.task_service.update(req)
        return {
            "status": "ok",
            "task": task.model_dump(mode="json"),
            "changed": changed,
        }
    return _safe(_run)


@mcp.tool()
def memory_task_comment(
    task_id: str,
    body: str,
    kind: str = "note",
    author: str | None = None,
    project: str | None = None,
) -> dict:
    """Add a comment to a task.

    kind says what the comment IS, so it is not read as chatter later:
    'note' (default), 'rule', 'decision', or 'reminder'. Anything that outlives
    the task - a project-wide rule, a decision that shapes future work - belongs
    in memory_add_rule / memory_store as well.
    """
    def _run():
        slug = _resolve(project)
        comment = container.task_service.comment(slug, task_id, body, kind, author)
        return {"status": "ok", "comment": comment.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_task_start(task_id: str, project: str | None = None) -> dict:
    """Start the clock on a task and move it to in_progress.

    Call this when the user has picked the task, not when you notice it. Safe to
    repeat: an already-running task keeps its open time entry rather than
    starting a second one. A finished task is reopened.
    """
    def _run():
        slug = _resolve(project)
        return container.task_service.start(slug, task_id).model_dump(mode="json")
    return _safe(_run)


@mcp.tool()
def memory_task_stop(task_id: str, project: str | None = None) -> dict:
    """Stop the clock on a task.

    The state is deliberately left as it is - stopping the clock says nothing
    about whether the work is paused, blocked or finished. Say which with
    memory_task_update(task_id, state=...) or memory_task_done(task_id).
    """
    def _run():
        slug = _resolve(project)
        return container.task_service.stop(slug, task_id).model_dump(mode="json")
    return _safe(_run)


@mcp.tool()
def memory_task_done(
    task_id: str, note: str | None = None, project: str | None = None,
) -> dict:
    """Mark a task done. Stops a running clock and stamps done_at.

    An optional note is stored as a comment on the task - what was actually
    done, or what is left over.
    """
    def _run():
        slug = _resolve(project)
        return container.task_service.done(slug, task_id, note).model_dump(mode="json")
    return _safe(_run)


@mcp.tool()
def memory_task_archive(task_id: str, project: str | None = None) -> dict:
    """Take a task out of the list without deleting it.

    Archived tasks drop out of the default listing and out of the session brief,
    but stay in the database and can still be listed with include_archived=True.
    """
    def _run():
        slug = _resolve(project)
        task = container.task_service.archive(slug, task_id)
        return {"status": "ok", "task": task.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_task_convert(task_id: str, project: str | None = None) -> dict:
    """Promote a sub-task to a task of its own.

    It leaves its parent's progress and joins the top-level list, where the
    session brief and the claim can see it.
    """
    def _run():
        slug = _resolve(project)
        task = container.task_service.convert_to_task(slug, task_id)
        return {"status": "ok", "task": task.model_dump(mode="json")}
    return _safe(_run)


@mcp.tool()
def memory_task_delete(task_id: str, project: str | None = None) -> dict:
    """Delete a task permanently, with its comments and time entries.

    This does NOT archive - the row is gone. Prefer memory_task_archive, which
    takes a task out of the list but keeps it findable; use this only when the
    user asks for the task to be removed for good. Any sub-tasks are promoted to
    top-level rather than deleted along with it.
    """
    def _run():
        slug = _resolve(project)
        return container.task_service.delete(slug, task_id)
    return _safe(_run)


@mcp.tool()
def memory_task_claim_next(session_id: str, project: str | None = None) -> dict:
    """Take the next available task in this project for this session.

    CALL THIS WHEN YOU HAVE FINISHED WHAT YOU WERE DOING - NEVER IN THE MIDDLE
    OF WORK. Several Claude sessions may be running against this project at
    once, and a task must be picked up by exactly one of them. Nothing routes
    work to you: you ask for it, and you only ask when you are idle. A busy
    session that asks anyway takes work it cannot start, and blocks the session
    that could have.

    Also do not call it speculatively at session start just because the list is
    non-empty - queued tasks are surfaced there to be reported to the user, not
    collected. Claim when the user has pointed you at the list, or when you have
    finished the thing you were asked to do and are picking up the next one.

    Pass the session_id returned by memory_session_start. Returns the claimed
    task, or claimed=false when nothing is available. The claim is held on a
    30-minute lease that renews whenever you touch the task, and is dropped
    automatically by memory_session_end.
    """
    def _run():
        slug = _resolve(project)
        task = container.task_service.claim_next(slug, session_id)
        if task is None:
            return {
                "claimed": False,
                "project": slug,
                "message": "No unclaimed task is waiting.",
            }
        return {
            "claimed": True,
            "project": slug,
            "task": task.model_dump(mode="json"),
            "next_step": (
                "Call memory_task_start to clock on, then memory_task_done when "
                "it is finished. memory_task_release hands it back untouched."
            ),
        }
    return _safe(_run)


@mcp.tool()
def memory_task_release(
    task_id: str, session_id: str | None = None, project: str | None = None,
) -> dict:
    """Give a claimed task back so another session can pick it up.

    Use this when you claimed something you are not going to do after all.
    Passing session_id releases only your own claim, never another session's.
    Finishing or archiving a task releases it for you.
    """
    def _run():
        slug = _resolve(project)
        task = container.task_service.release(slug, task_id, session_id)
        return {"status": "ok", "task": task.model_dump(mode="json")}
    return _safe(_run)


# ---------- Sessions ----------


@mcp.tool()
def memory_session_start(project: str | None = None) -> dict:
    """Start a session. Loads rules, last summary, sprint goals, recent decisions."""
    def _run():
        slug = _resolve(project)
        set_active_project(slug)
        ctx = container.session_service.start(slug)
        return ctx.model_dump(mode="json")
    return _safe(_run)


@mcp.tool()
def memory_session_end(
    session_id: str,
    summary: str,
    project: str | None = None,
    memories_created: int = 0,
    memories_accessed: int = 0,
) -> dict:
    """End a session and store its summary."""
    def _run():
        return container.session_service.end(
            _resolve(project), session_id, summary,
            memories_created, memories_accessed,
        )
    return _safe(_run)


# ---------- Portability ----------


@mcp.tool()
def memory_attach_project(
    project_path: str,
    slug: str | None = None,
    display_name: str | None = None,
    description: str | None = None,
) -> dict:
    """Attach an existing project directory. Auto-activates on success."""
    def _run():
        result = container.portable_service.attach(
            project_path, slug, display_name, description,
        )
        if result.get("status") == "ok":
            project_slug = result.get("project", {}).get("slug")
            if project_slug:
                set_active_project(project_slug)
                result["active"] = True
        return result
    return _safe(_run)


@mcp.tool()
def memory_make_portable(
    project_path: str,
    project: str | None = None,
) -> dict:
    """Move the project's DB into the project directory for git sharing."""
    def _run():
        return container.portable_service.make_portable(_resolve(project), project_path)
    return _safe(_run)


@mcp.tool()
def memory_sync(project_path: str, slug: str | None = None) -> dict:
    """Sync a portable DB after git pull. Auto-activates on success."""
    def _run():
        result = container.portable_service.sync(project_path, slug)
        if result.get("status") == "ok":
            project_slug = result.get("project", {}).get("slug")
            if project_slug:
                set_active_project(project_slug)
        return result
    return _safe(_run)


# ---------- Export / Import ----------


@mcp.tool()
def memory_export(export_path: str, project: str | None = None) -> dict:
    """Export all active memories to human-readable .md files."""
    def _run():
        return container.export_import_service.export(_resolve(project), export_path)
    return _safe(_run)


@mcp.tool()
def memory_import(import_path: str, project: str | None = None) -> dict:
    """Import memories from exported .md files."""
    def _run():
        return container.export_import_service.import_from(_resolve(project), import_path)
    return _safe(_run)


@mcp.tool()
def memory_import_claude_md(
    path: str,
    project: str | None = None,
    stub_rewrite: bool = False,
) -> dict:
    """Import a project's CLAUDE.md into memory as categorized entries.

    `path` is the CLAUDE.md file or the directory containing it. Headings are
    mapped to categories (rules, architecture, decisions, devops, docs...) and
    rule sections are split per bullet. When stub_rewrite=True, CLAUDE.md is
    replaced with a slim pointer at memory MCP (the original is backed up).
    """
    def _run():
        slug = _resolve(project)
        container.project_service.get(slug)  # ensure the project is registered
        return container.claude_md_service.import_file(slug, path, stub_rewrite)
    return _safe(_run)


# ---------- Model Management ----------


@mcp.tool()
def memory_model_info() -> dict:
    """Current embedding model + available presets."""
    return container.model_service.info()


@mcp.tool()
def memory_set_model(
    preset: str,
    project: str | None = None,
    confirm: bool = False,
) -> dict:
    """Switch embedding model between 'english' and 'multilingual' presets."""
    def _run():
        slug = _resolve(project) if project else None
        return container.model_service.set_model(preset, slug, confirm)
    return _safe(_run)


@mcp.tool()
def memory_reembed(project: str | None = None) -> dict:
    """Re-embed all active memories with the current model."""
    def _run():
        return container.model_service.reembed(_resolve(project))
    return _safe(_run)


# ---------- Updates ----------


@mcp.tool()
def memory_check_update() -> dict:
    """Check if a newer version of the Memory MCP server is available.

    Queries GitHub Releases first, falls back to git commit comparison.
    Does NOT modify anything - it only reports. Returns step-by-step
    update instructions when a new version is available.
    """
    return container.update_service.check()


# ---------- Entrypoint ----------


def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
