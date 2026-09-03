# Tasks + asoode bridge — design decisions

> Status: **brainstorm, agreed direction.** Nothing implemented yet.
> Read `01-asoode-analysis.md`, `02-memory-mcp-analysis.md`, `03-claude-ui-surfaces.md` for the
> underlying facts. This file records what we decided and why.

## 1. The problem

Two needs that look like one:

- **Capture** — *"many times I just want to add to the claude tasks list and I do not want to confuse
  it or interrupt it, I just want it to know I have a requirement/task."* Instant, local, must never
  fail, must not cause Claude to context-switch.
- **Management** — create tasks with descriptions and assignees, change status while working, add
  comments (a rule, decision, reminder), track time start/stop. Shared, rich, visible off this Mac.

asoode already does the second one completely. memory-mcp does neither today — it has **no task concept
at all**.

## 2. Decisions

### D1 — memory-mcp owns a real, complete task store; asoode is optional

**memory-mcp must work fully without asoode.** Some users won't run it. A missing or unreachable asoode
must never degrade memory-mcp beyond "mirroring is paused".

So this is **not** a thin ledger. The local `tasks` tables are a working task store: create, describe,
assign, change state, comment, track time — all functional offline.

### D2 — asoode integration is per-project, opt-in, and fully configurable

asoode is **on-premise**. Base URL, socket URL, project id, work-package id, credentials are all
per-link configuration. Nothing is hardcoded to `localhost:3000`.

**Copy the gateway's rule verbatim** (`project_service.py:66-67`): a project is **never auto-bound**.
Linking is always an explicit action, so a private project can't leak to someone's server.

**Copy the credential storage verbatim** (`registry.py:238-252`): the PAT goes in
`app_settings['cred:<base_url>']`, keyed by URL not by project, and **never** enters the committable
`.claude-memory/` snapshot.

### D3 — offline-first with an outbox

Every local mutation that needs mirroring appends to a `task_outbox` row and returns immediately. A
flusher drains the outbox, in order per task, whenever asoode is reachable. Connection loss is a normal
state, not an error. On reconnect: drain outbox, then reconcile inbound.

### D4 — inbound via live socket subscription, with a reconcile poll as the safety net

Chosen over polling. **This makes asoode's socket auth a hard blocker, not a nice-to-have** — today the
gateway trusts an unverified `?userId=` query param (`01-asoode-analysis.md` §4.3), and it mis-targets
recipients so project-level members receive nothing (§4.2).

The subscription lives in an **asyncio task in `daemon.build_app()`'s lifespan** (`daemon.py:22-24`) —
no such mechanism exists today, it must be built. It only talks HTTP/WS, so the macOS TCC constraint
that forces `sync_cli` into Claude's process does **not** apply to it.

A reconcile poll runs on every (re)connect to catch anything missed while disconnected, since socket
delivery is not durable.

### D5 — several sessions, one claim: pull, don't push

Multiple Claude Code sessions run against one project at once. A task must be picked up by exactly one,
and never by a busy one.

**There is no fan-out to deduplicate.** memory-mcp is one launchd daemon on `127.0.0.1:8765` and every
session is an MCP *client* of it; the port bind plus `KeepAlive` make a second daemon impossible. The
socket subscription lives in the daemon's lifespan, so one event produces one row.

**Idleness is self-declared.** The daemon cannot push to a session — sessions only speak when they call
a tool. So sessions *pull*: inbound tasks land unclaimed and inert, and a session claims one at
`memory_session_start` or via `memory_task_claim_next` when it has finished what it was doing. A busy
session never asks, so it never gets work. No heartbeat protocol, and nothing is ever interrupted —
which was the original requirement anyway.

**The claim** is `claimed_by` / `claimed_at` / `lease_expires_at` on `tasks`, taken with a conditional
`UPDATE … WHERE claimed_by IS NULL OR lease_expires_at < now()`; rowcount is the answer. DuckDB is
single-writer and this repo opens a connection per operation, so serialize it with a per-project
`threading.Lock` — sufficient because there is exactly one daemon. If server mode ever runs several
daemons, the claim table moves to the SQLite registry.

**Crash recovery:** lease TTL checked lazily on the next claim, `last_seen_at` refreshed by any tool
call (free — every call already hits the daemon), and `memory_session_end` releasing that session's claims.

⚠️ **Known limit:** two machines means two daemons and two local rows; local claims can't see each
other. Cross-machine exclusion requires a server-side claim in asoode (a bot assignee, or a
`claimed:<host>` label). Deferred to Phase 2+.

### D6 — tasks live in DuckDB and never enter the JSON snapshot

This kills the *"JSON files could become 100MB+"* worry at the root.

Measured (`02-memory-mcp-analysis.md` §2.5): per-memory JSON is 1–3.5 KB; a task row is far smaller
(~200–400 B without the description). **10,000 tasks ≈ 2–4 MB in DuckDB.** The 100 MB scenario only
existed because memories round-trip through `.claude-memory/*.json` — tasks won't.

⚠️ **`SYNC_CATEGORIES` is derived automatically** (`constants.py:24`). If tasks ever become a memory
*category*, they will start being written to the snapshot. **They must be separate tables, not a
category** — or explicitly excluded.

### D7 — DuckDB vs JSON files: the question was framed wrong

Neither. **Don't move files at all.**

| Option | Verdict |
|---|---|
| Ship a DuckDB file as the exchange format | **No.** 5.26 MB floor for a *single row*; binary, undiffable, unmergeable, guaranteed git conflict on concurrent edit; carries embeddings that are cheap to regenerate |
| Ship JSON files as the exchange format | Only for the existing git-committed memory snapshot. 22–650× smaller, `sort_keys=True` for stable diffs, per-category quarantine |
| **Move JSON over HTTP incrementally** | **Yes, for the bridge.** DuckDB stays the local store; the wire is small JSON payloads per task operation |

A file-based exchange would only be right for an offline, committed-to-git task manifest — and then it
should be JSON, per-target, sorted, exactly like `.claude-memory/`.

### D8 — the embedded window is `.claude/launch.json` + the Browser pane

Not a plugin. **Plugins have no UI extension point of any kind** (`03-claude-ui-surfaces.md` §2).
Config goes in the **asoode** repo, since that's the folder the session opens. Remember: a localhost
`url` must be origin-only and its port must equal the entry's `port` field (which defaults to 3000).

An iframe tab inside memory-mcp's own UI is technically possible but not worth it — prefer deep links
from the Tasks tab into asoode, opened in the pane.

---

## 3. Sketch of the model

> Shapes, not final DDL. Follow the migration rules in `02-memory-mcp-analysis.md` §2.4 and the
> `_ensure_columns` pattern in §2.1.

### Per-project DuckDB (new tables, schema v5)

```
tasks(
  id, title, description, state, priority, assignee, labels[],
  due_at, begin_at, end_at, estimated_minutes, parent_id, position,
  source,            -- 'user' | 'claude' | 'asoode'
  triage,            -- inbound items awaiting a decision (mirrors memories.pending semantics)
  claimed_by,        -- session id holding this task; NULL = free   (D5)
  claimed_at, lease_expires_at,
  created_at, updated_at, done_at, archived_at
)
task_comments(id, task_id, body, kind, author, created_at, remote_id)
task_time_entries(id, task_id, begin, end, manual, remote_id)
task_sync(task_id, link_id, remote_task_id, last_pushed_hash, remote_updated_at, sync_state)
task_outbox(id, task_id, link_id, op, payload, created_at, attempts, last_error)

-- existing table, altered:
sessions( … , last_seen_at )   -- free heartbeat; every tool call already hits the daemon   (D5)
```

**State vocabulary = asoode's, verbatim**, to avoid a lossy mapping:
`todo(1) in_progress(2) done(3) paused(4) blocked(5) cancelled(6) duplicate(7) incomplete(8) blocker(9)`.

### Global SQLite registry (new table)

```
project_links(
  id, slug REFERENCES projects(slug),
  provider DEFAULT 'asoode',
  base_url, socket_url,                 -- on-premise: fully configurable
  remote_project_id, remote_work_package_id,
  label,                                -- e.g. "backend", "frontend"
  is_default, default_list_id, default_assignee_id,
  state_list_map,                       -- JSON: state name -> asoode listId
  match_paths,                          -- JSON array of repo subpaths
  active, created_at
)
UNIQUE(slug, remote_work_package_id)
```

`match_paths` is what answers *"one claude project → multiple asoode work packages"*: in a monorepo
like asoode itself, `apps/backend/**` routes to the backend board, `apps/frontend/**` to the frontend
board, so tasks don't all pile onto one list.

Credentials are **not** in this table — they stay in `app_settings['cred:<base_url>']`.

### Surfaces to add

| Layer | What | Pattern to copy |
|---|---|---|
| Repository | `task_repository.py` (DuckDB), `link_repository.py` (registry) | `memory_repository.py` / `template_repository.py` |
| Service | `task_service.py`, `bridge_service.py` (outbox + reconcile), `asoode_client.py` | `remote_backend.py:20-114` for the HTTP client |
| MCP tools | `memory_task_add/list/update/comment/start/stop/link_target/...` | `memory_pending_*` (`server.py:692-743`) |
| HTTP | `/api/projects/{slug}/tasks[...]`, `/links[...]` | `_api(fn)`, `build_routes()` |
| UI | a **Tasks** tab | `PendingTab.tsx`; target picker modelled on `ImportRulesPanel.tsx` |
| Background | socket subscriber + outbox flusher | ⚠️ **new** — asyncio task in `daemon.py` lifespan |
| Session | `SessionContext.queued_tasks` + a task brief | `pending_adaptations` + `services/adaptation.py:36-44` |

**Capture semantics:** a task added by the user carries `source='user'` and the session brief must say
these are **queued, not instructions** — Claude surfaces them and does not start work unless asked.
That is what makes "add without interrupting Claude" work; it is a prompt-contract, not a storage problem.

---

## 4. Open questions

- Does a local task with no link ever get mirrored retroactively when a link is added later, or only
  new tasks? (Leaning: offer a one-time backfill, opt-in.)
- Conflict policy when both sides changed a task since `remote_updated_at`. asoode has **no optimistic
  concurrency** (no version/etag). Leaning: last-write-wins with a provenance row recording the
  clobber, matching `SyncService`'s "never delete" spirit.
- Do comments sync both ways, or outbound-only in v1?
- Multi-machine: two laptops both linked to the same work package will both mirror. The `externalRef`
  unique index (see the asoode backlog) is what makes that safe.
