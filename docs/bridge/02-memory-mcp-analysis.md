# claude-memory-mcp — architecture map for the asoode bridge

> Captured 2026-09-03 by an Opus analysis agent reading the source directly (working tree,
> including then-uncommitted changes). **Purpose: so we never have to re-read this repo to design
> the bridge.** Line numbers are a strong hint — re-verify before editing.
>
> **Updated after Phase 1 of the task module landed.** The audit below describes the repo *before*
> the `tasks` / `task_comments` / `task_time_entries` tables existed; §8 records what changed, and
> line numbers cited elsewhere have shifted in `db/schema.py`, `models.py`, `server.py`,
> `web/routes.py`, `enforcement.py`, `session_service.py` and `App.tsx`. Verify before relying on one.

## 1. Layering

```
MCP tools (server.py)         ──┐
Web JSON API (web/routes.py)  ──┤──►  services/*  ──►  repositories/*  ──►  db/{connection,schema,registry}.py
CLI (sync_cli, rules_cli,       │           │                    │
     users_cli, cli.py)       ──┘           └──► embeddings.py, utils/*
```

**Rules observed everywhere:**

- `server.py:1-8` states the tool contract: *"Each `@mcp.tool()` is a minimal wrapper: 1. Resolve the
  project 2. Build a request model 3. Call the service 4. Return a dict."* **Tools contain no SQL.**
- Services never import `server` or `web`. They take repositories via constructor injection
  (`container.py:22-53`).
- **Repositories own all SQL** (`repositories/memory_repository.py`). Nothing above the repository
  layer writes SQL — except `db/registry.py`, which is the SQLite registry's own module-level function
  API (`get_setting`, `create_user`, …) called directly by `context.py`, `project_service.py`, `routes.py`.
- `container.py` is a module-level singleton (`:57`), imported by `server.py:15`, `web/routes.py:21`,
  `enforcement.py:7`, `folder_import.py:12`. Tests build a fresh `Container()` instead.
- `constants.py:1-8` documents a hard constraint: leaf constants must live **outside** `services/`
  because `services/__init__` imports `memory_service` → `context`, and that cycle *"crashed
  `memory-mcp sync` on import."* **Any new shared constant goes in `constants.py`.**

### Conventions a new feature must follow

- **Pydantic models** for every request/response in `models.py`, grouped
  `# --- Domain Models ---` / `# --- Request Models ---` / `# --- Response Models ---` (`:56`, `:142`, `:197`).
- Tool bodies wrap work in a nested `def _run():` and return `_safe(_run)` (`server.py:108-118`).
- Web handlers are **sync** `fn(params, body, query)` wrapped by `_api(...)`, which runs them in a
  worker thread because DuckDB blocks (`web/routes.py:156-232`, docstring `:1-7`).
- Errors: raise from `exceptions.py`; `_api` maps to 404/401/403/400/500 (`web/routes.py:217-226`).
- Naming: `*_service.py` / `*_repository.py`, classes `XService` / `XRepository`, MCP tools prefixed `memory_`.
- Prose comments explain **why** and what failure the code prevents (see `db/schema.py:128-137`).

### File map

| File | LOC | Role |
|---|---|---|
| `src/memory_mcp/server.py` | 1147 | FastMCP app; **53** registered tools (11 of them `memory_task_*`). Auth wiring `:60-70` |
| `src/memory_mcp/context.py` | 270 | Active-project resolution + per-request user identity |
| `src/memory_mcp/enforcement.py` | 89 | Renders rule blocks for hooks; `rules_digest()` injected into tool responses |
| `src/memory_mcp/models.py` | 380 | All Pydantic models + `MemoryCategory` / `TaskState` / `TaskSource` / `TaskCommentKind` enums |
| `src/memory_mcp/constants.py` | 24 | `PORTABLE_DB_NAME`, `SNAPSHOT_DIRNAME`, `MANIFEST_NAME`, `SYNC_CATEGORIES` |
| `src/memory_mcp/db/schema.py` | 348 | DuckDB per-project schema + migrations + HNSW |
| `src/memory_mcp/db/registry.py` | 437 | **SQLite** global registry: projects, app_settings, templates, users |
| `src/memory_mcp/db/connection.py` | 133 | Connection-per-operation, path cache, lazy init/migrate |
| `src/memory_mcp/web/routes.py` | 1218 | Starlette JSON API + SPA + gateway proxy |
| `src/memory_mcp/sync_cli.py` | 203 | `memory-mcp sync export\|import` — the only file I/O for snapshots |
| `src/memory_mcp/remote_backend.py` | 119 | HTTP client to an org server |
| `src/memory_mcp/daemon.py` | 57 | Starlette app = UI routes + `Mount("/", mcp.http_app("/mcp"))` |

---

## 2. Storage

### 2.1 Two databases

**Per-project DuckDB.** `~/.claude-memory-mcp/projects/<slug>.duckdb` (`config.py:44,86-87`;
`db/connection.py:37`). Overridable per project via `projects.db_path`, which is how "portable" mode
moves it to `<project_folder>/.memory-mcp.duckdb`. Resolved paths are cached per process;
**`invalidate_path_cache(slug)` must be called after any `db_path` change** (`db/connection.py:40-58`).

**There is no project dimension inside a DuckDB file — the slug *is* the file.** Every repository
method takes `project: str` first and uses it only to pick the file (`connect(project)`).

**Global SQLite registry.** `~/.claude-memory-mcp/registry.db` (`config.py:90-92`), holding everything
that is not per-project memory (`registry.py:1-11`):

- `projects(slug PK, project_uid, display_name, description, created_at, last_accessed, db_path,
  project_path, owner, backend DEFAULT 'local', remote_url)` + unique partial index on `project_uid` (`:25-37, :121-124`)
- `app_settings(key PK, value)` — active project, embedding model, **and remote credentials under key
  `cred:<url>`** (`:245-252`)
- `templates` / `template_items` (`:42-56`)
- `users(id, username UNIQUE, display_name, role, token_hash, session_hash, active, created_at, last_login)` (`:57-69`)

Schema applied via `executescript(_SCHEMA)` on **every** connection open, plus `_ensure_columns()` for
post-hoc `ALTER TABLE`s (`:92-94, :101-125`). **This is the pattern a new registry table must follow.**
`PRAGMA foreign_keys = ON` (`:90`).

### 2.2 DuckDB schema (v5, `db/schema.py`)

**`memories`** (`:19-43`) — PK `id VARCHAR`. Column order is load-bearing (`memory_repository.py:14-22`):

```
id, category, title, content, summary, tags VARCHAR[], metadata JSON, embedding FLOAT[384],
status('active'), priority INT 0, source, related_ids VARCHAR[], entities VARCHAR[],
access_count INT 0, expires_at, created_at, updated_at, created_by,
approval_status('approved'), approved_by, approved_at, pending BOOLEAN FALSE
```

**`provenance`** (`:48-57`) — `id INTEGER PK DEFAULT nextval('seq_provenance_id')`, `memory_id`,
`operation`, `details JSON`, `actor`, `created_at`. **No FK** to `memories`.

**`sessions`** (`:136-147`) — `id PK`, `started_at`, `ended_at`, `summary`, `memories_created`,
`memories_accessed`, `metadata JSON`, `last_seen_at` (v5: the claim's heartbeat).

**`schema_version`** (`:148-153`) — `version INTEGER PK`, `applied_at`.

**`tasks` / `task_comments` / `task_time_entries`** (v5, `_TASK_DDL` + `create_task_tables()`,
`:15-89`) — the task store. See §8.

**Indexes** (`:77-85`): `idx_memories_{category,status,approval,pending,created,expires}`,
`idx_provenance_{memory,op}`. **No foreign keys anywhere in the DuckDB schema.**

### 2.3 VSS / HNSW

- `install_vss()` runs `INSTALL vss; LOAD vss; SET hnsw_enable_experimental_persistence = true;`
  (`:8-12`), called on every connection open (`db/connection.py:94-97`) and at the top of
  `run_migrations` (`schema.py:184`).
- `create_hnsw_index()` drops+recreates `idx_memories_embedding USING HNSW (embedding) WITH (metric='cosine')`,
  **only if ≥1 row has an embedding**, and swallows all exceptions — *"HNSW index is optional — search
  works without it (brute force)"* (`:205-242`).
- ⚠️ It is only called on **fresh DB creation** (`db/connection.py:75-76`) and in `make_portable`
  (`portable_service.py:104`). **It is not re-run after migrations**, so long-lived DBs likely run
  brute-force `array_cosine_distance` (`memory_repository.py:310`).

### 2.4 Migrations

Hand-written, linear, idempotent Python functions — no framework. `CURRENT_SCHEMA_VERSION = 5` (`:5`).
`migrate_v1_to_v2` (`:174`), `v2_to_v3` (`:207`), `v3_to_v4` (`:240`), `v4_to_v5` (`:261`).

`run_migrations(conn)` (`:285-322`): ensure `schema_version` exists, read `get_schema_version()`
(missing table ⇒ **v1**, `:274-282`), apply each step in sequence, return the new version.

**Every migration must:** wrap each `ALTER TABLE ADD COLUMN` in `try/except: pass` because *"DuckDB's
ADD COLUMN cannot be guarded with IF NOT EXISTS"* (`:132-136`), and **never rely on the column DEFAULT
for backfill** — an explicit `UPDATE` guarantees it (`:146-151, :167-170`).

Applied automatically on first open per process by `_ensure_initialized` (`db/connection.py:61-81`):
`is_new ⇒ create_schema + create_hnsw_index`, else `run_migrations`.

**Test contract (`tests/test_migrations.py`)** — a new migration must add a test in this exact shape:
build a v1-shaped DB (`:8-22`), assert version detection (`:25`), new columns/tables appear (`:35`),
**existing rows preserved** (`:83`), **idempotent** (`:95`), new flags backfill to the safe value (`:107-120`).
v5 follows it in `test_migration_adds_v5_task_tables` and
`test_migration_adds_last_seen_at_backfilled_from_started_at`, plus
`test_fresh_schema_matches_migrated_schema`, which asserts `create_schema` and the migration path
produce identical task tables — the drift a two-place DDL invites.

### 2.5 ⚠️ Measured sizes — decisive for the "DuckDB file vs JSON" question

| Project | DuckDB file | rows | JSON snapshot | ratio |
|---|---|---|---|---|
| `claude-memory-mcp` | **5.26 MB** | **1** | 8 KB | **~650×** |
| `asoode` | 9.71 MB | 35 | 128 KB | ~76× |
| `kalagh` | 22.29 MB | 109 | 996 KB | ~22× |

Total `~/.claude-memory-mcp/projects/`: **~420 MB across 32 project files.** Registry SQLite: 78 KB.

**A DuckDB file with one row is 5.26 MB.** Block allocation + the persisted HNSW index dominate
completely; file size barely tracks row count, and **files never shrink**. Per-memory JSON cost is
**1–3.5 KB**. Embeddings (`FLOAT[384]` = 1536 B/row, `all-MiniLM-L6-v2`) are excluded from JSON and
**regenerated on import** (~14k sentences/sec) — that is why the snapshot is small.

---

## 3. Project identity

Three layers, in priority order:

1. **`project_uid`** (UUID4, `registry.py:79-81`) — the stable identity, written into the committed
   `<folder>/.claude-memory/manifest.json` as `project_id` (`sync_cli.py:125-130`). Survives move,
   rename, and a teammate's clone.
2. **`slug`** — filename-safe key, `slugify()` (`utils/text.py:6-12`), validated by `validate_slug`.
   Collisions get `-2`, `-3`, … via `_free_slug` (`project_service.py:155-163`).
3. **`project_path`** — the bound source folder.

**Resolution from a cwd** (`context.py:168-202`) — `detect_project_from_cwd(cwd)`:
1. walk up ≤10 levels for `.claude-memory/manifest.json` → read `project_id` → `get_by_uid()`
2. walk up ≤10 levels for `.memory-mcp.duckdb`
3. `_slug_from_path()` — bound `project_path` exact-or-ancestor → `db_path` substring → foldername == slug
   (`:229-263`). `__global__` is explicitly excluded.

`resolve_project(project, cwd)` = explicit > active > cwd-detected (`:266-270`).
`server._resolve()` disables CWD detection in server mode (`server.py:83`).

**Active project:** local mode = process global `_active_project` + persisted
`app_settings['active_project']` (`context.py:98-134`). Server mode = per-user key
`active_project:<user_id>` (`registry.py:432-437`).

**Claim / rebind:** `ProjectService.claim_folder(cwd, project_uid, slug_hint, display_name)`
(`project_service.py:84-153`) returns `matched|rebound|adopted|created|unclaimed`. Exposed at
`POST /api/hook/claim` (`web/routes.py:280-310`), called by `sync_cli._claim()` from the SessionStart hook.
Covered by `tests/test_project_identity.py` (move, rename, adopt, collision, manifest detection, corrupt-manifest fallback).

**State at capture: 32 registered projects, all `backend='local'`, including one already named `asoode`
bound to `/Users/aslan_nejad/Desktop/DEV/achasoft/asoode` (uid `802b848f-…`).**
There is **no** existing "one project → many external targets" relation anywhere.

### Project tools

| Tool | Where | Does |
|---|---|---|
| `memory_init_project(slug, display_name, description, set_active, project_path)` | `server.py:153-175` → `project_service.py:17-44` | Register in SQLite, assign uid, create DuckDB by opening a connection |
| `memory_link_folder(path, project)` | `server.py:193-205` → `:165-169` | Set `project_path` ⇒ enables git-synced `.claude-memory/` |
| `memory_attach_project(project_path, slug, …)` | `server.py:780-798` → `portable_service.py:25-73` | Attach an existing `.memory-mcp.duckdb`, or create; auto-activates |
| `memory_use(project)` | `server.py:143-147` | Set active project |
| `memory_load_from_folder(path)` | `server.py:178-190` → `folder_import.py:30-69` | Name from `package.json`/folder, attach, import `CLAUDE.md`, activate |
| `memory_list_projects` / `memory_project_info` / `memory_rename_project` | `:224`, `:231`, `:208` | |

---

## 4. Memory records, rules, and the pending subsystem

### 4.1 `Memory` (`models.py:59-88`)

11 categories (`:15-26`): `decision, session, sprint, project_plan, architecture, devops,
mandatory_rules, forbidden_rules, developer_docs, feedback, reference`.

- `status`: `active` | `archived` (soft delete, `memory_repository.py:360-365`) | `expired`
  (set lazily during `list()`, `:241-245`)
- `metadata: dict|None` — **JSON column, the extension point** used by the pending flow
- `embedding` — 384 floats, `all-MiniLM-L6-v2`, normalized (`embeddings.py:27-31`).
  **Stripped from every API/snapshot response** (`web/routes.py:60-64`, `sync_service.py:34-45`)
- `expires_at` — TTL by category × priority; rules and `priority >= 2` never expire (`utils/extraction.py:71-109`)
- `priority: 0-3`; rules forced to `>= 2` on store (`memory_service.py:68-70`)

### 4.2 Rules

**Not a separate table** — memories in the two `*_rules` categories, fetched with a dedicated
**no-LIMIT** query so the rule set is never a top-N subset (`memory_repository.py:132-164`), cached
60 s (`services/rules_service.py:11-41`), invalidated by every rule write (`memory_service.py:111-113,
:177-178, :236-237`).

Rule lifecycle: `approval_status ∈ {approved, proposed, revoked}` + `approved_by/at`, consulted only
for rules and **enforced only in server mode** (`rules_service.py:55-58`). Org-wide rules live in the
reserved `__global__` project and are prepended to every project's block (`models.py:31-38`).

**Provenance:** every mutation writes a row — `create, access, update, approve, revoke, soft_delete,
hard_delete, adapt, discard_import`.

**Token budgeting:** `SearchRequest.token_budget` → `SearchService._build_budgeted` (`:64-91`) returns
**every** hit as a lightweight `index` entry but only fills `details` with full `content` while the
running total fits; sets `has_more`. Estimate = `len(text) // 4`. Relevance = `0.7 similarity +
0.15 recency + 0.15 access_count`, 3× oversample. **Rules bypass budgeting entirely.**

### 4.3 ⭐ The pending / adaptation subsystem

**A complete, working "inbound item needing triage" pipeline.** Schema v4. This is the direct
precedent for anything arriving from asoode.

**Mechanism:** one boolean column, `memories.pending` (`schema.py:41`, migration `:159-177`).
A pending row is stored but **inert** — excluded from:

- rules (`memory_repository.py:151` via `NOT_PENDING`, defined `:26`)
- vector search (`:312`)
- recent/active category reads (`:188, :204`)
- default listing (`MemoryFilter.pending = False` default, `models.py:187`; SQL `:231-235`)
- the git snapshot (`all_for_categories(..., include_pending=False)`, `:261-280`)

**Visible only through** `pending_memories()` / `count_pending()` (`:282-300`), and `MemoryFilter(pending=True)`.

**Pipeline:**

1. **Ingest** — `MemoryService.copy_memories(target, source, ids, pending=True)` (`memory_service.py:243-294`).
   Stores the original verbatim in `metadata["imported_from"] = {project, memory_id, title, content}`
   (`:269-278`), `source="imported"`.
2. **Announce** — `SessionService.start()` returns `pending_adaptations` + `pending_instructions` in
   `SessionContext` (`session_service.py:56, :73-74`; `models.py:243-246`). The session-start hook text
   also counts them (`enforcement.py:39-45`).
3. **Brief** — `services/adaptation.py` builds a 5-step agent instruction: read origin → rewrite for
   this project → **ask the user rather than guess** → `memory_adapt_pending` → or `memory_discard_pending`
   (`:11-33`, entry `adaptation_brief(project, pending)` `:36-44`).
4. **Resolve** — `adapt_pending()` re-summarizes, re-extracts entities, **re-embeds**, sets
   `source="adapted"`, clears `pending`, provenance op `adapt` (`:305-362`).
   `discard_pending()` soft-deletes + op `discard_import` (`:364-378`).

**Surfaces:**
- MCP: `memory_import_rules` (`server.py:658-689`, `pending=True` by default), `memory_pending_list`
  (`:692-707`), `memory_adapt_pending` (`:710-732`), `memory_discard_pending` (`:735-743`)
- HTTP: `GET /api/projects/{slug}/pending`, `POST …/pending/{mid}/adapt`, `DELETE …/pending/{mid}`
  (`web/routes.py:874-900`)
- UI: `frontend/src/components/PendingTab.tsx` — origin panel showing the original verbatim (`:148-158`),
  editable title/content drafts, Apply/Discard. **Tab label carries the count** so invisible items
  announce themselves (`App.tsx:606-612`)
- Types: `ImportOrigin`, `PendingAdaptationsResponse` (`frontend/src/types.ts:283-295`);
  client `listPendingAdaptations/adaptPending/discardPending` (`frontend/src/lib/api.ts:430-456`)
- Tests: `tests/test_pending_imports.py` — `TestPendingIsInert` (5 invisibility assertions),
  `TestPendingIsVisible`, `TestAdapting`, `TestDiscarding`

---

## 5. Sync & portability

### 5.1 Three mechanisms that do not know about each other

**(1) JSON snapshot** — `<project_path>/.claude-memory/` (`constants.py:16`):
- `<category>.json` for each of the **10** `SYNC_CATEGORIES` (all but `session`, `constants.py:24`),
  written only when non-empty; an empty category's file is **deleted** (`sync_cli.py:112-120`)
- `manifest.json`: `{version:1, project_id, slug, categories:[…], exported_at}` (`:125-130`) —
  **this file is the project's portable identity**
- entries = `Memory.model_dump(mode="json")` minus `embedding`, `access_count`, `pending`
  (`sync_service.py:34-45`); written `indent=2, sort_keys=True` **for stable diffs**

**Who writes/reads: only `sync_cli.py`.** ⚠️ It runs in **Claude Code's process, not the launchd
daemon**, because *"unlike the launchd daemon, it can reach project folders on the Desktop"*
(`sync_cli.py:1-9`) — **a macOS TCC constraint**. It talks to the daemon over plain HTTP on
`127.0.0.1:8765` with `urllib` (`:26-34`) and does the file I/O itself. `SyncService` is pure in-memory.

Triggered by shell hooks: `.claude/hooks/session-start.sh:19-20` runs `sync import`;
`session-end.sh:16-17` runs `sync export`. **Both skipped when `MEMORY_MCP_URL` is set.**

**(2) Portable DuckDB** — `memory_make_portable` = `shutil.copy2` the .duckdb into the folder, move the
original to `backups/`, update `db_path`, suggest gitignoring `*.duckdb.wal` (`portable_service.py:75-125`).

**(3) Markdown export** — `memory_export` → `<path>/.memory/<category>/*.md` + `MEMORY_INDEX.md` +
`README.md`, capped at 500 memories (`export_import_service.py:32-67`). `memory_import` parses them back.

Note `memory_sync(project_path, slug)` is **not** the JSON snapshot sync — it registers the folder and
points `db_path` at an existing `.memory-mcp.duckdb` (`portable_service.py:127-161`).

### 5.2 Snapshot merge invariants — `SyncService.apply_snapshot` (`sync_service.py:77-113`)

Commit `031eee3 "Fix: project memory sync must never delete or revert rules"` exists because these were
once violated. **Copy these invariants for any new sync path:**

1. **Never delete.** A memory present locally but absent from the snapshot is kept and counted as
   `kept_local_only` (`:112`). *"a stale snapshot … must not be able to destroy rules"* (`:83-87`).
2. **Last-write-wins by timestamp, one direction only.** Update **only if
   `snapshot.updated_at > db.updated_at`** strictly (`_snapshot_newer`, `:119-135`); a missing snapshot
   timestamp returns `False` (never overwrite).
3. **Per-category quarantine.** A `<category>.json` that fails to parse (e.g. unresolved git conflict
   markers) is dropped from `reconcile` and that whole category is skipped (`sync_cli.py:146-155`).

Difference detection compares 13 fields incl. approval fields (`_SYNC_FIELDS`, `:27-31`).
Insert re-embeds from scratch (`:138`); update re-embeds only if title/content changed (`:175-178`).
Returns `{added, updated, kept_local_only}`.

Wire: `GET /api/projects/{slug}/sync-export` → `{"categories": {...}}` (`web/routes.py:758-762`);
`POST /api/projects/{slug}/sync-import` with `{"categories":{...}, "reconcile":[...]}` (`:765-774`).
**Neither is `remote_aware`** — snapshot sync is local-only by design.

### 5.3 Remote / gateway — the precedent to copy for asoode

**(A) Server mode** (`MEMORY_MCP_MODE=server`, `config.py:59-63, :75-83`):
- Bearer tokens `mmcp_<43 urlsafe>`, stored **SHA-256 hashed only** in `registry.users.token_hash`
  (`registry.py:263-269, :287-314`). Verified by `auth.RegistryTokenVerifier` (`auth.py:16-34`),
  wired at `server.py:65-70`.
- UI sessions: username+token → HttpOnly/Secure/SameSite=strict cookie `mmcp_session`
  (`web/routes.py:69, :354-383`), plus CSRF header `X-Requested-With: memory-mcp` required on
  cookie-authed writes (`:73-75, :200-209`; sent by the client on every request, `api.ts:64-67`).
- Per-request identity in a **contextvar** (`context.py:45-95`), per-user active project,
  rule approval governance, admin-only routes.

**(B) Hybrid gateway** — per-project backend routing:
- `bind_backend(slug, 'remote'|'local', remote_url, token)` (`project_service.py:57-82`) —
  **always explicit**, via `POST /api/projects/{slug}/bind` or `memory-mcp bind <slug> --remote <url>
  --token T` (`cli.py:65-86`). *"projects default to 'local' and are never auto-bound to remote, so
  private projects can't leak"* (`:66-67`). **Adopt this rule verbatim for asoode links.**
- Persisted as `projects.backend` + `projects.remote_url` (`registry.py:35-36`).
- **Token storage:** `app_settings['cred:<url>']`, **keyed by server URL, not by project**, and
  deliberately *"never in the committable .claude-memory snapshot, so a private project's credentials
  never travel with a repo"* (`registry.py:238-252`). **Reuse this verbatim for the asoode PAT.**
- **Wire protocol: plain HTTP + JSON, `Authorization: Bearer <token>`.** Two clients:
  - `RemoteBackend` (httpx **sync**, 20 s, used by MCP tools) — `POST/GET/PUT/DELETE
    /api/projects/{slug}/memories[/{id}]`, `/rules[/{id}][/approve|/revoke]` (`remote_backend.py:30-114`).
    Failures raise `RemoteError`.
  - `_proxy_to_remote` (httpx **async**, used by the web API) — forwards method, path, query and body
    **verbatim**, returns the remote status/body; unreachable ⇒ 502 (`web/routes.py:112-140`).
- **Routing decision:** `server._remote(slug)` for tools (`:93-105`), `_api(..., remote_aware=True)` for
  routes — checked **before any local work**, and *"Not gated on server_mode — a local daemon gateways
  too"* (`web/routes.py:181-186`).
- **No merge strategy for remote — there is no merge.** Remote-bound projects are *served from* the org
  server; their data never touches local storage. Each tool is either/or:
  `rb = _remote(slug); if rb: return rb.store(...)`. Remote branches are **lossy**: `memory_search`
  degrades to keyword `list()` (`server.py:298-300`), `memory_recall` requires an id, `related_ids` dropped.
- Client-only install: `memory-mcp setup --client --url … --token …` writes an MCP entry with a Bearer
  header and env-prefixed hooks, **no local daemon/DB** (`setup.py:335-370`).
- Tests: `tests/test_gateway.py` asserts only the routing decision;
  *"End-to-end forwarding is covered by the manual two-daemon check."*

---

## 6. Daemon & UI

**One process, one port.** `daemon.build_app()` =
`Starlette(routes=[*build_routes(), Mount("/", mcp.http_app("/mcp"))], lifespan=mcp_app.lifespan)`
(`daemon.py:20-24`). Default **`127.0.0.1:8765`** (`config.py:55-57`; 8765 because *"98765 is not
usable"*). UI at `/`, MCP at `/mcp/`.

**Started by** `uv run memory-mcp-setup` (`pyproject.toml:34` → `setup.py:295-332`): installs a
self-contained venv at `~/.claude-memory-mcp/runtime`, copies `frontend/dist` → `~/.claude-memory-mcp/ui`,
writes+loads a **launchd** agent `com.claude-memory-mcp.daemon` with `RunAtLoad`/`KeepAlive` and
`MEMORY_MCP_UI_DIR` set (`setup.py:83-152`). Also `memory-mcp serve`, Docker, Homebrew.
Auto-reinstall on source change: `.claude/hooks/auto-update-install.sh` → `memory-mcp update`.

**Frontend served from** `MEMORY_MCP_UI_DIR` if set, else repo-relative `frontend/dist`
(`web/routes.py:39-47`). `/` returns `dist/index.html` or a "UI not built" placeholder;
`/assets` is a `StaticFiles` mount **added only if the directory exists** (`:1079-1081`).
Dev mode proxies `/api` and `/mcp` to `127.0.0.1:8765` (`frontend/vite.config.ts:13-18`).

**React app** (React 18 + Tailwind + lucide, **no router**): `App.tsx` is a single stateful shell.
Sidebar views `projects | templates | users | org-rules | moderation`; per project, 5 tabs:
`memories | rules | tasks | pending | sessions` (`App.tsx:57, :623-646`, rendered `:805-880`).
**Adding a tab = 3 edits** (`TabValue`, `tabs[]`, render block).
Dialogs: `NewProjectDialog`, `ImportRulesDialog`/`ImportRulesPanel` (checkbox picker over another
project's or a template's items — **the closest analogue to a "pick asoode targets" picker**),
`LinkFolderDialog`, `MemoryEditorDialog`, `BulkAddRuleDialog`, `⌘K CommandPalette`.

**Embedding an external app in this UI?** Structurally cheap, but: (a) no CSP header is set anywhere,
so an iframe is not blocked *by this app* — asoode would need to permit framing
(`X-Frame-Options`/`frame-ancestors`) from `http://claude-memory-mcp:8765`; (b) the SPA is served over
**plain HTTP on a custom hostname** (`setup.py:157-167`), so any postMessage handshake must pin origins;
(c) `_proxy_to_remote` (`web/routes.py:112-140`) could be generalized into a **same-origin proxy** for
an asoode API, avoiding CORS entirely; (d) no cross-origin cookie story — asoode auth needs its own
token stored like remote creds.

### ⚠️ There is NO scheduler, NO background task runner, NO webhook receiver anywhere in `src/`

`grep webhook|integration|task` returns only an unrelated comment at `context.py:44`. The only periodic
triggers are the three Claude Code shell hooks (`.claude/hooks/*.sh`) and launchd `KeepAlive`.

**A poll loop or socket subscription needs a new mechanism** — either a `sync_cli`-style command invoked
from `session-start.sh`, or an **asyncio task started in `daemon.build_app()`'s lifespan** (`daemon.py:22-24`).

---

## 7. Extension seams

### 7.1 Walk one existing trio end to end (copy this exactly): Templates

1. **Schema** — `registry.py:42-56`: `templates` + `template_items` added to `_SCHEMA`
   (SQLite global — correct for anything that is not per-project memory).
2. **Repository** — `repositories/template_repository.py`: `TemplateRepository` with
   `create/list_all/get/update/delete/add_item/...`, all inside `with registry_conn() as conn:`,
   plus `TemplateNotFoundError(MemoryMCPError)` (`:11-13`), re-exported from `repositories/__init__.py:7-9`.
3. **Model** — `models.py:107-121`: `TemplateItem`, `Template`.
4. **Service** — `services/template_service.py`: constructor takes `(TemplateRepository, MemoryService)`
   (`:16-18`); validates input (`:49-52`); `apply()` **composes `MemoryService.store()`** rather than
   writing SQL (`:74-107`).
5. **Wiring** — `container.py:28` (repo), `:52` (service); export in `services/__init__.py:13,28`.
6. **MCP tools** — `server.py:601-655`: a private `_template_by_name()` helper, then
   `memory_list_templates`, `memory_create_template`, `memory_add_template_rule`,
   `memory_apply_template`, each `def _run(): … ; return _safe(_run)`.
7. **HTTP** — `web/routes.py:777-848` handlers + `Route(...)` entries at `:1070-1077`.
8. **Frontend** — `api.ts:291-363` client methods, `types.ts` interfaces, `TemplatesView.tsx` + dialogs,
   sidebar view in `App.tsx`.
9. **Tests** — `tests/services/test_template_service.py`.

### 7.2 Where a bridge plugs in

| Seam | Where | Notes |
|---|---|---|
| **Link table (project → many targets)** | `db/registry.py:24-70` `_SCHEMA` + `_ensure_columns` (`:101-125`) | Global SQLite is the right home: it already holds `projects`, credentials, templates. `PRAGMA foreign_keys=ON` already set |
| **Credentials** | `registry.get_credential/set_credential` (`:238-252`) | Already URL-keyed, already excluded from the committed snapshot. **Reuse verbatim for the asoode PAT** |
| **Repository** | new `repositories/*_repository.py`, export in `repositories/__init__.py` | Follow `TemplateRepository` (registry_conn) or `MemoryRepository` (`connect(project)`) depending on where the table lives |
| **Service** | new `services/*_service.py`, export in `services/__init__.py`, wire in `container.py:33-53` | Constructor-inject repos. **Shared constants go in `constants.py`, not here** (`constants.py:1-8`) |
| **Outbound HTTP client** | mirror `remote_backend.py:20-114` | `_req(method, path, params, json)`, Bearer header, `RemoteError`, `for_project()` factory |
| **Inbound triage** | `MemoryService.copy_memories` (`memory_service.py:243-294`) is the template | Store `pending=True` + origin in `metadata`. The row is then **automatically** invisible to rules/search/snapshot and **automatically** surfaced at session start and in the Pending tab. **Zero new invisibility plumbing needed** |
| **Triage brief** | `services/adaptation.py:36-44` | Pure function of `(project, pending_list)` — extend to branch on origin type, or add a sibling `task_brief()` |
| **New category** | `models.py:15-26` + TTL entry in `utils/extraction.py:72-84` | ⚠️ `SYNC_CATEGORIES` is **derived automatically** (`constants.py:24`) — a new category **will** start appearing in `.claude-memory/`. Exclude it explicitly if it shouldn't be committed |
| **Migration** | `db/schema.py`: bump `CURRENT_SCHEMA_VERSION` (`:5`), add `migrate_v4_to_v5`, chain in `run_migrations` (`:181-208`), add to `create_schema` | Follow `migrate_v3_to_v4` (`:159-177`) exactly. Add a test mirroring `tests/test_migrations.py:107-120` |
| **MCP tools** | `server.py`, new section | Follow `memory_pending_list`/`memory_adapt_pending` (`:692-743`). Remember the `rb = _remote(slug)` branch for remote-bound projects |
| **HTTP routes** | `web/routes.py` handlers + `build_routes()` (`:1015-1082`) | Use `_api(fn)`; `remote_aware=True` only for project-scoped data |
| **Frontend tab** | `App.tsx:55` / `:603-614` / `:778-840` + new component | `PendingTab.tsx` is the closest component to copy; `ImportRulesPanel.tsx` is the model for a multi-select target picker |
| **Background loop** | ⚠️ **does not exist** | New mechanism required: a `sync_cli`-style command from `session-start.sh`, or an asyncio task in `daemon.build_app()`'s lifespan (`daemon.py:22-24`) |

### 7.3 Constraints to design around

- ⚠️ **The launchd daemon cannot read Desktop/Documents folders (macOS TCC)** — that is why
  `sync_cli.py` exists and runs in Claude's process (`sync_cli.py:1-9`, `setup.py:13-15`).
  **Anything touching project folders must follow the same split; anything that only talks HTTP can
  live in the daemon.**
- `MEMORY_COLUMNS` order is load-bearing; **new memory columns append at the end** and
  `MEMORY_COLUMN_COUNT` must be bumped (`memory_repository.py:14-22`).
- `httpx` is **already** a dependency (`pyproject.toml:9-18`) — no new dep needed for an HTTP bridge.
- **Failures in hook-facing paths must be swallowed and logged, never surfaced** (`sync_cli.py:174-189`
  writes tracebacks to `~/.claude-memory-mcp/sync.log`; the comment records that a silent circular
  import *"killed every export and import for weeks without a trace"*).


---

## 8. Phase 1 of the task module (added after this audit)

The audit above says memory-mcp has **no task concept**. That is no longer true; §7.2's "where a
bridge plugs in" is now partly built. What exists, and what still does not:

**Schema v5** (`db/schema.py`) — `tasks`, `task_comments`, `task_time_entries`, created by
`create_task_tables()` which `create_schema` and `migrate_v4_to_v5` both call, plus an `ALTER` adding
`sessions.last_seen_at` backfilled from `started_at`. Tasks are **separate tables, never a
`MemoryCategory`**, so `SYNC_CATEGORIES` cannot pick them up and the `.claude-memory/` snapshot stays
small — asserted by `TestTasksStayOutOfTheSnapshot` in `tests/services/test_task_service.py`.

**Layers** — `repositories/task_repository.py` (all SQL), `services/task_service.py` (constructor-
injected, wired in `container.py`), `services/task_brief.py` (a pure `(project, tasks) -> str | None`,
sibling of `adaptation.py`), models in `models.py` under the existing Domain/Request/Response grouping.
Every task mutation writes a `provenance` row (`task_create`, `task_update`, `task_comment`,
`task_start`, `task_stop`, `task_done`, `task_archive`, `task_claim`, `task_release`) — the table has
no FK and `memory_id` is just an entity id, so task rows live alongside memory rows.

**MCP tools** — `memory_task_add/list/get/update/comment/start/stop/done/archive`, plus
`memory_task_convert` (promote a sub-task), `memory_task_delete` (permanent, unlike archive) and
`memory_task_claim_next` / `memory_task_release`. **Deliberately not `_remote()`-gated:** tasks have no
counterpart on an org server, so a remote-bound project keeps its task list locally rather than
proxying to a route that does not exist there.

**HTTP** — `/api/projects/{slug}/tasks[...]` in `web/routes.py`, `_api`-wrapped and sync, **not**
`remote_aware`, for the same reason. `TaskNotFoundError` is mapped to 404 alongside the others.
Beyond CRUD: `/tasks/reorder` (manual order), `/tasks/{tid}/convert`, `/tasks/{tid}/activity`
(the provenance trail), `/tasks/{tid}/release` (the operator escape hatch for a stuck claim).

**Sub-tasks** — `parent_id` with one level of nesting in practice. The top-level list hides children
(`TaskFilter.include_subtasks=False` by default) and shows their progress on the parent instead; the
claim never offers a sub-task; deleting a parent **promotes** its children rather than cascading.

**UI** — `TasksTab.tsx` composes `TaskListView.tsx` (asoode's list mode: a group per state with a
colour-coded pill, the NAME/ASSIGNEE/DUE DATE/PRIORITY column grid, inline add per group, drag to
reorder) and `TaskDialog.tsx` (asoode's task modal: breadcrumb header, quick-properties bar,
in-place title/description editing, sub-tasks with a ⋯ menu for rename/convert/delete,
comments + activity tabs, and a 280px sidebar carrying assignee, labels, priority, dates, the
stopwatch and the claim). Shared presentation constants live in `lib/tasks.ts` - the state colours are
asoode's, so the two apps read the same way side by side.

**Session** — `SessionContext.queued_tasks` + `task_instructions`; `SessionService.end()` releases the
ending session's claims; `enforcement.format_intro` carries the open count.

**The multi-session claim** — `claimed_by` / `claimed_at` / `lease_expires_at` on `tasks`, taken by a
conditional `UPDATE` whose **rowcount is the answer** (`TaskRepository.claim`), serialized by a
per-project `threading.Lock` in `TaskService`. One daemon makes that lock sufficient; a multi-daemon
server mode would have to move the claim to the SQLite registry. The lease is checked **lazily on the
next claim**, so there is no sweeper thread.

**Still not built, and still shaped as §7.2 describes:** `project_links` (shape recorded in a comment
at the top of `db/registry.py`), `task_sync` / `task_outbox` (shape recorded above `_TASK_DDL` in
`db/schema.py`), the asoode HTTP client, and the inbound socket subscription (shape recorded in
`daemon.build_app`'s docstring). `triage` exists on `tasks` and nothing sets it. **§6's warning still
holds: there is no background-task mechanism anywhere in `src/`.**
