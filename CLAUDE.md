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

Mirroring is automatic in both directions. Out: every create/update/completion/
comment/time-entry/attachment queues to an outbox and flushes off-thread, so a
local write never waits on the network. In: a Socket.IO subscription reconciles
within seconds of a board change, backed by a poll so a dropped socket only costs
promptness. Every connect also sweeps each linked project once, because asoode
replays nothing that happened while the socket was down.

The subscriber ignores the echo of our OWN writes. asoode broadcasts to every
member without excluding the actor, and drops the actor id before the client
sees it, so the writer records what it wrote (`services/echo_log.py`) and the
listener consults that. Do not "fix" this with a time window — it would drop a
genuine concurrent change that lands in the same window.

`memory-mcp asoode` write commands go through the daemon's HTTP API when one is
running: DuckDB is single-writer per file and the daemon holds the lock, so
doing it in-process fails AFTER the remote calls have already happened. Adding a
CLI write means adding its route too.

**Inbound only CREATES.** A task on both sides is never overwritten — that needs
a conflict policy nobody has decided. `memory_asoode_import` is the explicit path
that does overwrite, so do not describe the two sides as "in sync".

`memory_task_attach(task_id, path)` puts evidence on a task — a screenshot of a
working fix, a failing log — and mirrors it to the board.
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

## The agent team

Eight agents share this server's memory and task board: `pm`, `backend`, `frontend`,
`designer`, `test`, `reviewer`, `devops`, `docs`. All pinned to `claude-opus-5`; effort is
`max` for pm/designer/test/reviewer, `xhigh` for backend/frontend/devops, `high` for docs.
Design and verification status: `docs/bridge/06-agent-team.md`.

**Every session starts as `pm`.** `setup_default_agent()` writes `agent: pm` to
`~/.claude/settings.json`, so the session inherits pm's prompt, tools, model and effort and
behaves as the orchestrator. Remove the `"agent"` key from that file to undo it. Note the
mechanism: the MCP **server** cannot choose a session's agent — the **installer** sets it, and
it reaches every project because it writes the same global files the MCP registration uses.

**pm keeps Edit/Write on purpose.** A strictly read-only pm was considered and rejected: a
session that cannot edit must dispatch a subagent for even a one-line change, and the measured
floor for a dispatch is ~60k tokens. pm fans out to protect its own *context* — surveying a
large codebase — not because it is forbidden to work. Do not "restore" a no-edit constraint
without re-reading that trade-off.

**`reviewer` cannot edit**, via `disallowedTools`. That one is load-bearing: a reviewer that can
fix its own findings stops reviewing. `disallowedTools` is used rather than a `tools:` allowlist
because a denylist leaves inherited MCP tools intact.

**`agents/` is the source of truth.** Claude Code reads `~/.claude/agents/`; `setup_agents()`
copies them there on every `memory-mcp-setup` and on the auto-update path. **Never edit the
installed copy** — it is overwritten. Edit `agents/<name>.md`, re-run setup, restart Claude Code.
Retiring an agent removes its installed copy, but only ever a file this installer wrote
(`~/.claude-memory-mcp/agents-installed.json` is the manifest). See `agents/README.md`.

**Routing.** A task carries a `role` (schema v12). `memory_task_claim_next(role=...)` offers an
agent its own role's work plus unroled work, never another role's. A task with NO role is
claimable by anyone — that keeps every task predating the column visible, so do not backfill
roles to "tidy up".

**Test credentials** live in `.claude/test-credentials.json`, gitignored, with a committed
`.example`. Never in an agent definition — `agents/` is version-controlled and installs to a
shared directory.

**Two verified facts that shape every definition:** subagents inherit the memory MCP server in
full, including its write surface (an agent's `tools` list restrains its filesystem, not the
board); and `memory_search` does NOT search tasks, so an agent must read `memory_task_list` /
`memory_task_get` or it misses the entire queue.

Agents installed mid-session are not dispatchable until Claude Code restarts.

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
