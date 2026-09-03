# Memory MCP Server

Production-grade vector memory MCP server with DuckDB + semantic search.

## Auto-Memory Behavior

When this MCP is active and a project session is running, Claude should AUTOMATICALLY use memory tools during natural conversation — no explicit commands needed:

### Auto-Store (triggered by conversation context)
- **Decision made** → `memory_store(category="decision", ...)`
- **Architecture choice** → `memory_store(category="architecture", ...)`
- **User sets a rule** ("always do X", "never do Y") → `memory_store(category="mandatory_rules"/"forbidden_rules", ...)`
- **User gives feedback** ("don't do that", "keep doing this") → `memory_store(category="feedback", ...)`
- **Sprint/milestone discussed** → `memory_store(category="sprint", ...)`
- **DevOps config decided** → `memory_store(category="devops", ...)`
- **External resource mentioned** → `memory_store(category="reference", ...)`

### Auto-Search (before answering)
- When user asks about a past decision → `memory_search("...")`
- When context from previous sessions is needed → `memory_search("...")`
- When starting work that may have prior context → `memory_search("...")`

### Auto-Rules Check
- Before any significant operation → `memory_get_rules()` to ensure mandatory rules are followed and forbidden patterns are avoided

### Pending imports (rules copied from another project)
- `memory_session_start` returns `pending_adaptations` — rules imported from
  another project that are **not yet in force**. Adapt them before relying on
  any of them: rewrite each for this codebase (dropping the source project's
  component names, paths and stack details), **ask the user** when a rule cannot
  be translated without information only they have, then call
  `memory_adapt_pending(memory_id, title, content)`. Use
  `memory_discard_pending(memory_id, reason)` for rules that do not belong here.

### Tasks (a separate store, not memories)

`tasks` / `task_comments` / `task_time_entries` live in their own DuckDB tables — deliberately **not**
a `MemoryCategory`, so they never enter the committed `.claude-memory/*.json` snapshot.

- **A queued task is a requirement, not an instruction.** `memory_session_start` returns
  `queued_tasks`; surface what is waiting and then leave it alone. Do not start a task because it is
  in the list — the user decides what gets picked up and when.
- "Add a task to do X" means `memory_task_add(title="X")` **and nothing else** — keep doing what you
  were doing. Being able to record a requirement without derailing the session is the entire feature.
- Out-of-scope work you notice → `memory_task_add(..., source="claude")`. Queue it and say so.
- Working one: `memory_task_start` → `memory_task_done`. `memory_task_stop` only stops the clock and
  leaves the state alone; say what happened with `memory_task_update(task_id, state=...)`.
- Notes on a task → `memory_task_comment(task_id, body, kind="note"|"rule"|"decision"|"reminder")`.
  Anything that outlives the task still belongs in `memory_add_rule` / `memory_store`.
- **Several sessions may share a project.** Take a task with `memory_task_claim_next(session_id)`
  ONLY when you have finished what you were doing — never mid-task, and never just because the list
  is non-empty. `memory_session_end` releases whatever you held.

### Multi-part requests

Call `memory_task_plan(request, tasks)` FIRST when a request has two or more
separable deliverables, then work them top-down. One task per deliverable, never
per step (steps use `parent_index`); a question or a single change in several
clauses is not a plan. Every task needs a description — the tool rejects one
without it.

### asoode (the task bridge)

Endpoints default to the hosted service — `app.asoode.com`, `api.asoode.com`,
`socket.asoode.com` — so nothing needs configuring unless this is an on-premise
install (`memory-mcp asoode set-url --api …`, or `MEMORY_MCP_ASOODE_*_URL`).

The PAT is stored **once for the machine**, keyed by server URL, and shared by
every project. Never ask the user for it per project, and never accept it in
chat: `memory-mcp asoode set-pat` prompts without echoing. `memory_asoode_status`
returns a fingerprint, never the token.

**A project links to WORK PACKAGES, never to an asoode project.** asoode has no
route attaching a task to a project — project → work package → list → task is the
only path — so "linked to an asoode project" cannot be represented. One memory
project holds MANY links (a monorepo has one board per app); `memory_task_add`'s
`target` names which, and a task with no target routes to the default link.

- `memory_asoode_attach` links a board that ALREADY EXISTS (the usual case).
- `memory_asoode_link` CREATES a project + board — only when none exists yet.
- `memory_asoode_boards` lists what can be attached.
- `memory_asoode_import` pulls board tasks into the local list.

Mirroring is automatic: every task create/update/completion/comment queues to an
outbox and flushes off-thread, so a local write never waits on the network and an
unreachable asoode is a delay, not a lost edit. `memory_asoode_push` is now only
for full reconciliation. **Still one-way for edits** — importing is explicit, so
never say the two sides are in sync.
- `memory_asoode_push` mirrors local tasks onto it.
- `memory-mcp asoode open <slug>` opens that board already signed in.

**A bound project's board IS the work queue.** `memory_session_start` returns it
with a brief saying to work it one task at a time — take the highest-priority
actionable one, start it, mirror the state, comment as you go, mark it done, next.
That inverts the "surface but never start" contract, which still holds for every
unbound project. Do not auto-start blocked/blocker/paused/cancelled tasks, and
stop to ask when the work needs a decision only the user can make.

Both are **idempotent via `externalRef`** (the project uid and the task id), so
re-running pushes changes rather than duplicating. Both create real objects on the
user's account — ask before the first link, and say what was created afterwards.

**The bridge is one-way today.** Nothing reads asoode back into the local store,
so never tell the user the two are in sync.

### Session Lifecycle
- At conversation start → `memory_session_start()`
- At conversation end → `memory_session_end(session_id, summary)`

## Development

```bash
uv sync --all-extras
uv run pytest -v
```

## Tech Stack
- Python + FastMCP
- DuckDB + VSS (HNSW vector search, cosine similarity)
- sentence-transformers/all-MiniLM-L6-v2 (local embeddings, 384 dimensions)
- Pydantic v2
