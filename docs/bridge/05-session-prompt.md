# Session prompt — memory-mcp side (tasks module)

> **Phase 1 is done.** This prompt is kept as the record of what was asked for;
> `02-memory-mcp-analysis.md` §8 records what actually landed, including the parts
> that grew in the building (asoode-style list mode and task dialog, sub-tasks,
> manual reordering, convert/delete). Phase 2 — the asoode bridge itself — is
> still unbuilt, and its seams are marked in the code.

Paste this to open the memory-mcp work session.

---

I'm adding a **task concept** to this project. Today it has none. The driving need: I want to drop a
requirement into a list without interrupting whatever Claude is working on, and I want Claude to pick
those up at the start of a session. Later, an optional bridge mirrors tasks to `asoode`, my
project-management app.

**Read `docs/bridge/` first** — especially `04-design-decisions.md` (what we agreed and why) and
`02-memory-mcp-analysis.md` (a full audit of this repo: layering, schemas, migration contract, the
pending subsystem, sync mechanisms, extension seams, with file:line citations). `01-asoode-analysis.md`
covers the other side. Where a doc and the code disagree, **the code wins** — the audit is from
2026-09-03 and line numbers may have drifted; correct the docs as you go.

## This session: Phase 1 only — the standalone task store

**Build no asoode integration in this session.** It must work with zero asoode, because some users
won't run one, and because the asoode side has P0 blockers (socket auth, `externalRef`) being worked in
a separate session. Design the seams for the bridge, but don't wire it.

### 1 · Schema (v5 migration)

New tables in the **per-project DuckDB**:

```
tasks(id, title, description, state, priority, assignee, labels[],
      due_at, begin_at, end_at, estimated_minutes, parent_id, position,
      source, triage,
      claimed_by, claimed_at, lease_expires_at,
      created_at, updated_at, done_at, archived_at)
task_comments(id, task_id, body, kind, author, created_at)
task_time_entries(id, task_id, begin_at, end_at, manual)   -- `end` is a DuckDB reserved word
```

- `state` vocabulary is **asoode's, verbatim**, so the later mapping is lossless:
  `todo in_progress done paused blocked cancelled duplicate incomplete blocker`.
- `source` ∈ `user | claude` (add `asoode` in Phase 2).
- `kind` on comments distinguishes a note from a rule/decision/reminder.
- Leave `triage` in the schema now (Phase 2 uses it for inbound items) but nothing sets it yet.
- `claimed_by` / `claimed_at` / `lease_expires_at` implement the multi-session claim (see §1a). They
  are cheap now and awkward to retrofit after v5 ships, so add them even though Phase 1 barely uses them.

The migration must also **`ALTER` the existing `sessions` table** to add `last_seen_at`, backfilled
explicitly from `started_at`. Every tool call already reaches the daemon, so stamping this is a free
heartbeat.

⚠️ **These must be separate tables, never a `MemoryCategory`.** `SYNC_CATEGORIES` is derived
automatically from the category enum (`constants.py:24`), so a new category would immediately start
writing tasks into the committed `.claude-memory/*.json` snapshot. Keeping tasks out of the snapshot is
the whole reason the "JSON grows to 100MB" problem can't happen.

Follow the migration contract exactly (`migrate_v3_to_v4`, `db/schema.py:240-258`, is the model): bump
`CURRENT_SCHEMA_VERSION`, add `migrate_v4_to_v5`, chain it in `run_migrations`, add the tables to
`create_schema` too, wrap each DDL in `try/except` (DuckDB has no `ADD COLUMN IF NOT EXISTS`), and
never rely on a column DEFAULT for backfill. Add a test in the shape of
`tests/test_migrations.py:107-120` — version detection, tables appear, **existing rows preserved**,
**idempotent**.

### 1a · The multi-session claim

I run **several Claude Code sessions against one project at the same time.** A task must be picked up
by exactly one of them, and never by a session that is busy.

The architecture already solves most of this: memory-mcp is **one launchd daemon bound to
`127.0.0.1:8765`**, and every session is an MCP *client* of it. A second daemon can't even start — the
port bind and launchd `KeepAlive` enforce it. So there is one writer, and no fan-out to deduplicate.

**The rule: pull, don't push.** The daemon cannot push to a session — sessions only speak when they
call a tool. So never route work *to* a session; let a session ask *for* work. Only the session knows
whether it is mid-task, which makes "idle" definitionally correct with no heartbeat machinery: a busy
session simply doesn't ask.

Build:

- `memory_task_claim_next` — the session asks for one unclaimed task. Claude calls this **when it has
  finished what it was doing**, never in the middle of work. Its description must say exactly that.
- `memory_task_release` — give a claim back.
- The claim is a conditional update, and the rowcount is the answer:

  ```sql
  UPDATE tasks SET claimed_by = ?, claimed_at = now(), lease_expires_at = now() + <ttl>
  WHERE id = ? AND (claimed_by IS NULL OR lease_expires_at < now())
  ```

  1 = you got it, 0 = someone else did.

⚠️ **DuckDB is single-writer and this repo opens a connection per operation** (`db/connection.py`), so
that read-modify-write needs serializing. There is exactly one daemon process and `_api` already runs
handlers in a worker thread pool, so a **per-project `threading.Lock` in the service is a complete
fix**. Don't reach for anything heavier. (If `MEMORY_MCP_MODE=server` ever runs multiple daemons, the
claim table moves to the SQLite registry, where cross-process `UPDATE … WHERE` is genuinely atomic.
Leave a comment saying so.)

**Crash recovery**, all cheap:

- lease TTL (start at 30 minutes), checked **lazily on the next claim attempt** — no sweeper thread
- refresh the lease on any tool call from that session, via the `last_seen_at` stamp
- `memory_session_end` releases every claim that session holds

**Known limit, don't try to solve it now:** two Macs means two daemons, two socket subscriptions, and
two local rows — local claims can't see each other. Cross-machine exclusion needs the claim to live
server-side in asoode (assign to a bot member, or a `claimed:<host>` label). That is Phase 2+.

### 2 · Repository + service + models

- `repositories/task_repository.py` — **all SQL lives here**, nothing above it writes SQL. Follow
  `memory_repository.py`; export from `repositories/__init__.py`.
- `services/task_service.py` — constructor-injected repos, wired in `container.py`, exported from
  `services/__init__.py`.
- Pydantic models in `models.py`, in the existing
  `# --- Domain ---` / `# --- Request ---` / `# --- Response ---` grouping.
- Any shared leaf constant goes in `constants.py`, **not** in `services/` — `constants.py:1-8` records
  that a `services/__init__` import cycle "crashed `memory-mcp sync` on import".
- Write a `provenance` row for task mutations, matching how `MemoryService` does it.

### 3 · MCP tools

Follow the contract in `server.py:1-8` — resolve project, build request model, call service, return a
dict; body in a nested `def _run():` returning `_safe(_run)`. Model them on `memory_pending_*`
(`server.py:706-762`).

`memory_task_add`, `memory_task_list`, `memory_task_get`, `memory_task_update` (title/description/
state/priority/assignee/dates/estimate), `memory_task_comment`, `memory_task_start`, `memory_task_stop`
(time entries), `memory_task_done`, `memory_task_archive`, plus `memory_task_claim_next` and
`memory_task_release` from §1a.

**Capture semantics matter most here.** A task with `source='user'` is a **queued requirement, not an
instruction**. `memory_task_add`'s description must say so explicitly, and the session brief must
present these as "here's what's waiting" — Claude surfaces them and does **not** start work unless I
ask. That prompt-contract is the entire feature; get the wording right.

### 4 · Session integration

Extend `SessionService.start()` to include queued tasks in `SessionContext`, alongside the existing
`pending_adaptations`. Add a task brief the way `services/adaptation.py:36-44` does — a pure function
of `(project, tasks)`. Update the session-start hook text in `enforcement.py:32-54` to carry the count.

### 5 · Web API + UI

- Routes under `/api/projects/{slug}/tasks[...]` in `web/routes.py`, handlers wrapped by `_api(fn)`,
  registered in `build_routes()`. Keep them **sync** — `_api` runs them in a worker thread because
  DuckDB blocks.
- A **Tasks** tab in the React UI. Adding a tab is 3 edits in `App.tsx` (`TabValue`, `tabs[]`, render
  block). `PendingTab.tsx` is the closest component to copy, and its "tab label carries the count"
  trick is exactly right for queued tasks.
- Client methods in `frontend/src/lib/api.ts`, types in `frontend/src/types.ts`.

### 6 · Tests

`tests/services/test_task_service.py` in the shape of `tests/services/test_template_service.py`, plus
the migration test. Cover: create/update/state transitions, comments, time entries, and — importantly —
that **tasks never appear in `all_for_categories()` / the sync snapshot**.

Also test the claim: **two concurrent claims on one task, exactly one wins**; an expired lease is
reclaimable; `memory_session_end` releases what that session held.

## Design the seams, don't build them

Leave these obviously-shaped for Phase 2, but empty:

- `project_links` in the **SQLite registry** (`db/registry.py` `_SCHEMA` + `_ensure_columns`) —
  one memory project → many asoode targets, with `base_url`, `socket_url`, `remote_project_id`,
  `remote_work_package_id`, `state_list_map`, and a `match_paths` JSON column that routes a monorepo's
  subpaths to different boards.
- Credentials will reuse `registry.get_credential/set_credential`
  (`app_settings['cred:<url>']`, `registry.py:264-281`) — URL-keyed, never in the committed snapshot.
- `task_sync` and `task_outbox` tables (outbox-based offline mirroring).
- Phase 2's inbound is a **live Socket.IO subscription** in an asyncio task in `daemon.build_app()`'s
  lifespan (`daemon.py:20-42`). ⚠️ **No background-task mechanism exists anywhere in `src/` today** —
  that is net-new, and it will need a socket client dependency (`httpx` is already a dep; a Socket.IO
  client is not).

## Repo constraints — read these before writing code

- **Layering is strict:** tools/routes/CLI → services → repositories → db. Services never import
  `server` or `web`. Repositories own all SQL.
- **macOS TCC:** the launchd daemon cannot read Desktop/Documents. That's why `sync_cli.py` runs in
  Claude's process (`sync_cli.py:1-9`). Anything touching project folders must follow that split;
  anything that only talks HTTP can live in the daemon.
- **Failures in hook-facing paths must be swallowed and logged, never surfaced** (`sync_cli.py:174-189`
  — a silent circular import once "killed every export and import for weeks without a trace").
- `create_hnsw_index()` is only called on fresh DB creation, **not after migrations**
  (`db/connection.py:74-78`) — don't assume the index exists.
- **Mandatory project rule:** after any bug fix, rebuild the frontend if it changed
  (`cd frontend && npm run build`), run `uv run memory-mcp-setup` to reinstall the runtime/UI and reload
  the launchd daemon, and verify it's healthy. A fix isn't done until the local install is updated.

## How to work

Show me the schema and the MCP tool signatures before you write the implementation — those are the two
things that are expensive to change later. Then build it in the order above.
