# Claude Memory MCP

**Persistent, searchable, per-project memory for Claude Code.**

[![CI](https://github.com/navid-kianfar/claude-memory-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/navid-kianfar/claude-memory-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker Hub](https://img.shields.io/badge/docker%20hub-kianfar%2Fclaude--memory--mcp-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/kianfar/claude-memory-mcp)

Claude forgets everything between sessions. You re-explain the same decisions,
rules get missed, and context is lost when the window fills up. Claude Memory
MCP gives each of your projects its own brain — decisions, rules, architecture
notes, and sprint goals stored locally in a vector database, retrieved by
meaning, and automatically loaded every time you start a session.

![Claude Memory MCP management UI](screenshots/memory-browser.png)

---

## What you get

- **Per-project memory** — each project has an isolated DuckDB database; memory
  never leaks between projects.
- **Semantic search** — ask "what database did we pick?" and it finds the
  Postgres decision even if you never typed "Postgres".
- **Rule enforcement** — mandatory/forbidden rules are re-injected into Claude's
  context every turn (via hooks) so they survive context compaction and stop
  being forgotten.
- **A management UI** — a React app to browse, search, and edit every project's
  memories, rules, sessions, and history, with a `Cmd+K` command palette.
- **One shared daemon** — a single background process serves the MCP endpoint
  and the UI; the embedding model loads once, and there are no database lock
  conflicts between clients.
- **Templates** — define a set of default rules once, then seed every new
  project from it (pick exactly which rules with checkboxes) instead of
  re-typing them. New projects can also import selected rules from any
  existing project — those arrive **pending**, and are rewritten for the
  project they land in before they take effect.
- **A task list** — drop a requirement into the project's list at any moment
  without interrupting whatever Claude is doing. Claude surfaces what is waiting
  at the start of a session and starts nothing unless you ask. Tasks have
  states, priorities, labels, due dates, sub-tasks, comments and a stopwatch,
  and live in their own tables — never in the committed memory snapshot.
- **CLAUDE.md import** — convert an existing `CLAUDE.md` into structured memory.
- **Portable & team-shareable** — move a project's database into the repo,
  commit it, and teammates get the same memory after `git pull`. A project's
  identity is committed with it, so moving or renaming its folder never
  produces a duplicate project.

## How it works

```mermaid
flowchart LR
  subgraph clients[Claude Code]
    CLI[Terminal CLI]
    APP[Desktop app]
  end
  UI[Management UI<br/>React + command palette]
  subgraph daemon[memory-mcp daemon · port 8765]
    MCP[/MCP endpoint  /mcp/]
    API[/JSON API  /api/]
    EMB[Embedding model<br/>loaded once]
  end
  DB[(Per-project<br/>DuckDB + vector index)]

  CLI -->|HTTP| MCP
  APP -->|HTTP| MCP
  UI -->|HTTP| API
  MCP --> DB
  API --> DB
  MCP --- EMB
```

Both Claude Code clients and the UI connect to the **same daemon**, which is the
sole owner of the DuckDB files. A `UserPromptSubmit` hook asks the daemon for
the current project's rules and injects them into context on every turn.

## Quick start — Docker (recommended)

```bash
docker run -d --name memory-mcp \
  -p 8765:8765 \
  -v memory-mcp-data:/data \
  kianfar/claude-memory-mcp:latest
```

Or with Compose:

```bash
docker compose up -d
```

Then:

- **Management UI** — open <http://localhost:8765/>
- **Connect Claude Code** — register the MCP server:

  ```bash
  claude mcp add --transport http memory http://localhost:8765/mcp
  ```

## Quick start — Homebrew

```bash
brew tap navid-kianfar/tap
brew install claude-memory-mcp
brew services start claude-memory-mcp        # runs the daemon in the background
```

Then `claude mcp add --transport http memory http://localhost:8765/mcp`. See
[packaging/homebrew/](packaging/homebrew/) for tap setup details.

## Quick start — from source

Requires [`uv`](https://docs.astral.sh/uv/) and (for the UI) Node 20+.

```bash
git clone https://github.com/navid-kianfar/claude-memory-mcp.git
cd claude-memory-mcp
./install.sh
```

`install.sh` installs dependencies, builds the UI, downloads the embedding
model, installs a launchd agent so the daemon auto-starts, points Claude Code at
the daemon, and installs the rule-enforcement hooks. It prints a one-time
`sudo` command to add a `claude-memory-mcp` entry to `/etc/hosts` so the UI URL
resolves — after that the UI is at <http://claude-memory-mcp:8765/>.

## Screenshots

Once the daemon is running, the management UI is at
<http://localhost:8765/> — browse, search, and edit every project's memories,
rules, and sessions.

**Templates** — define a baseline rule set once, then reuse it for every new
project:

![Templates view](screenshots/templates.png)

**Seed a new project** — on creation, import exactly the rules you want (with
checkboxes) from a template or from another existing project:

![Importing rules into a new project](screenshots/new-project-seed.png)

## Using it

Inside Claude Code:

```text
memory_init_project("my-app", "My App")   # create a project
memory_session_start("my-app")            # loads rules + context
```

From then on Claude stores decisions, rules, and sprint notes automatically and
recalls them with semantic search. At the start of each session it loads the
project's rules, last summary, and recent decisions.

### Rule enforcement

Rules you set (`mandatory_rules` / `forbidden_rules`) are enforced three ways:

1. **Hook injection** — a `UserPromptSubmit` hook injects the actual rule text
   into context every turn, so rules survive context compaction.
2. **Server instructions** — the MCP server tells Claude to load and honor rules.
3. **Tool responses** — search/store responses carry a compact rules reminder.

Hooks are silent in directories that are not registered memory projects, so
they can be installed globally without noise.

Rules imported from another project are the one exception: they are **not**
enforced until they have been adapted to the project they were imported into
(see [Importing rules from another project](#importing-rules-from-another-project)).

### Tasks

A task is a **queued requirement, not an instruction**. That is the whole point:
you can record something mid-session without derailing the work in progress.

```text
memory_task_add("Rewrite the CSV exporter")   # queued; Claude keeps doing what it was doing
memory_task_list()                            # what is waiting, open work first
memory_task_start(task_id)                    # clock on, moves it to in_progress
memory_task_done(task_id, note="shipped")     # closes it and stops the clock
```

`memory_session_start` returns the open tasks together with a brief telling
Claude to **report them and start none of them** unless you ask. Work Claude
notices but is not doing goes in with `source="claude"`.

When several Claude sessions share one project, a task is taken by claiming it:
`memory_task_claim_next(session_id)` — which a session calls only when it has
finished what it was doing, never mid-task. The claim is a conditional UPDATE
whose rowcount decides the winner, held on a 30-minute lease that renews as the
task is worked on, and released by `memory_session_end`.

The **Tasks** tab in the management UI is a full list view: tasks grouped by
state, drag to reorder, inline add per group, and a task dialog with in-place
editing, sub-tasks, comments, an activity trail and time tracking.

### Importing an existing CLAUDE.md

```text
memory_import_claude_md("/path/to/project")            # import into memory
memory_import_claude_md("/path/to/project", stub_rewrite=True)  # + slim the file
```

Headings are mapped to categories (rules, architecture, decisions, devops,
docs); rule sections are split per bullet. With `stub_rewrite`, `CLAUDE.md` is
replaced by a short pointer at memory MCP (the original is backed up).

## The management UI

A React single-page app served by the daemon at `/`:

- Browse, search, create, edit, and archive memories in every category
- Manage mandatory/forbidden rules
- Work the task list: grouped by state, drag to reorder, and a task dialog with
  sub-tasks, comments, activity and a stopwatch
- Inspect sessions and per-memory provenance/history
- Switch and set the active project
- `Cmd+K` command palette for fast navigation and actions

## MCP tools

All 66 tools:

| Area | Tools |
|------|-------|
| Projects | `memory_init_project`, `memory_load_from_folder`, `memory_link_folder`, `memory_list_projects`, `memory_project_info`, `memory_rename_project`, `memory_use` |
| Memories | `memory_store`, `memory_search`, `memory_recall`, `memory_update`, `memory_delete`, `memory_list` |
| Rules | `memory_get_rules`, `memory_add_rule`, `memory_add_rule_bulk`, `memory_update_rule`, `memory_delete_rule` |
| Governance (server mode) | `memory_approve_rule`, `memory_revoke_rule` |
| Templates | `memory_list_templates`, `memory_create_template`, `memory_add_template_rule`, `memory_apply_template`, `memory_import_rules` |
| Imported rules | `memory_pending_list`, `memory_adapt_pending`, `memory_discard_pending` |
| Tasks | `memory_task_add`, `memory_task_list`, `memory_task_get`, `memory_task_update`, `memory_task_comment`, `memory_task_start`, `memory_task_stop`, `memory_task_done`, `memory_task_archive`, `memory_task_convert`, `memory_task_delete` |
| Task claims (multi-session) | `memory_task_claim_next`, `memory_task_release` |
| Planning | `memory_task_plan` |
| Sessions | `memory_session_start`, `memory_session_end` |
| Portability | `memory_attach_project`, `memory_make_portable`, `memory_sync` |
| Import/Export | `memory_export`, `memory_import`, `memory_import_claude_md` |
| Model | `memory_model_info`, `memory_set_model`, `memory_reembed` |
| asoode bridge | `memory_asoode_status`, `memory_asoode_boards`, `memory_asoode_attach`, `memory_asoode_link`, `memory_asoode_import`, `memory_asoode_reconcile`, `memory_asoode_push`, `memory_asoode_links` |
| Attachments | `memory_task_attach`, `memory_task_attachments` |
| Misc | `memory_provenance`, `memory_version`, `memory_check_update` |

## Configuration

Environment variables (prefix `MEMORY_MCP_`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `MEMORY_MCP_DATA_DIR` | `~/.claude-memory-mcp` | Where databases are stored |
| `MEMORY_MCP_DAEMON_HOST` | `127.0.0.1` | Daemon bind address (`0.0.0.0` in Docker) |
| `MEMORY_MCP_DAEMON_PORT` | `8765` | Daemon port |
| `MEMORY_MCP_DAEMON_HOSTNAME` | `claude-memory-mcp` | Hostname used in the UI URL |
| `MEMORY_MCP_ASOODE_API_URL` | `https://api.asoode.com` | asoode REST base (on-premise override) |
| `MEMORY_MCP_ASOODE_APP_URL` | `https://app.asoode.com` | asoode web app, for links |
| `MEMORY_MCP_ASOODE_SOCKET_URL` | `https://socket.asoode.com` | asoode realtime origin |

## asoode integration

Mirror a project's task list onto an [asoode](https://app.asoode.com) board. The
endpoints above default to the hosted service, so on-premise is the only case
that needs configuring.

The access token is stored **once per machine**, not per project — every project
that talks to the same asoode reuses it:

```bash
memory-mcp asoode set-pat                     # prompts; the input is not echoed
memory-mcp asoode check                       # prove it reaches the server
memory-mcp asoode boards                      # list boards you can attach to
memory-mcp asoode attach <slug> --ref <ref>   # link an EXISTING board
memory-mcp asoode link <slug>                 # CREATE a project + board
memory-mcp asoode import <slug>               # pull board tasks into the local list
memory-mcp asoode push <slug>                 # full reconciliation (rarely needed)
memory-mcp asoode open <slug>                 # open that board already signed in
```

These commands ask the **running daemon** to do the work rather than opening the
project database themselves. DuckDB allows one writer per file across processes
and the daemon holds that lock, so doing it in-process used to fail — and fail
*late*, after the remote calls had already gone out, leaving tasks on the board
that the local store had no record of. If no daemon is running they fall back to
direct access, and any other path that hits the lock now says so and offers both
ways out instead of raising a DuckDB `IOException`.

### One project, many boards

**A project links to work packages, never to an asoode project.** asoode has no
route attaching a task to a project — `project → work package → list → task` is
the only path — so a monorepo links to *one board per app*:

```bash
memory-mcp asoode attach myrepo --ref myrepo:backend                 # the default
memory-mcp asoode attach myrepo --ref myrepo:frontend --not-default
```

`memory_task_add(title=..., target="myrepo:frontend")` names the board a task
belongs to; a task with no `target` routes to the **default** link. A wrong name
fails the create rather than landing on the wrong board.

Use `attach` when the boards already exist, which is the normal case — `link`
*creates* one, so running it on a set-up workspace adds a duplicate beside the
real boards.

### Mirroring is automatic

Every task create, update, completion and comment queues to an outbox and flushes
**off-thread**, so a local write never waits on the network. If asoode is
unreachable the write still succeeds and the row is retried on the next flush —
an unreachable server is a delay, not a lost edit.

`import` is the other direction, for tasks created in asoode by a person. It is
**import-only**: a remote change overwrites the local title and state, and local
edits are not merged back. The two sides are never "in sync" — don't describe
them that way.

`open` uses asoode's `/auth/token` deep link, which carries the token in the URL
**fragment** — never sent to the server, never in an access log, never leaked
through a `Referer`, and stripped from the address bar on arrival. The link is
handed straight to the browser and printed redacted; no HTTP route returns or
redirects to it, which would put the token in a response body or a `Location`
header.

### Multi-part requests become tasks before they are worked

`memory_task_plan(request, tasks)` records a request with several separable
deliverables as an ordered set of tasks — the user's wording kept verbatim on
each one — then they get worked top-down. If the session ends after the first,
the rest are still in the queue rather than only in the transcript.

The boundary is one task per **deliverable**, not per step: a question or a single
change described in several clauses is not a plan. Fewer than 2 tasks is rejected
(that is `memory_task_add`), more than 20 is over the cap, and a task with no
description is refused — over-decomposition buries a board in rows nobody would
plan around.

### Other platforms

The bridge is provider-agnostic: `TaskProvider` (a Protocol), a registry that
resolves a link's `provider` column, per-(provider, account) credentials, and a
conformance suite any implementation must pass. asoode is the only provider
shipped, because an integration written from published docs and tested against a
fake written in this repo is not a verified integration.

### Binding a project makes its board the work queue

A bound project's `memory_session_start` returns the board's open tasks and a
brief telling the agent to **work** them one at a time — start, mirror the state,
comment as it goes, mark done, next. An unbound project keeps the opposite
contract: its queued tasks are surfaced and never started, so parking a
requirement mid-session cannot derail the session.

The binding is the whole opt-in — nothing is configured per project. If the board
cannot be reached, the session still starts and is told to work the local list,
which is the same queue mirrored.

The token lives in the local registry (`~/.claude-memory-mcp/registry.db`), never
in the committed `.claude-memory/` snapshot, and is never printed back — `status`
shows only a `prefix…last4` fingerprint. Pass `--api-url` to store one for a
second server.

`link` and `push` are idempotent: the board carries the project's stable uid as
its `externalRef` and each task carries its local id, so asoode returns the
existing row instead of creating a duplicate. Re-running pushes changes.

On-premise:

```bash
memory-mcp asoode set-url --api https://api.asoode.internal
```

The sibling `app.`/`socket.` URLs are derived when the host looks like
`api.<domain>`; otherwise pass `--app` and `--socket` too. `reset-url` returns to
the hosted defaults.

### Both directions

**Out:** every task create, update, completion, comment, time entry and
attachment queues to an outbox and flushes off-thread, so a local write never
waits on the network. An unreachable asoode is a delay, not a lost edit — rows
are retried, and abandoned after five attempts so a poison row cannot loop
forever repeating a side effect.

**In:** the daemon holds a Socket.IO subscription, so a task added on the board
reaches the session within seconds. It is an optimisation, not a correctness
requirement — `reconcile` also runs after every mirror, so a dropped socket
degrades to polling. asoode replays nothing, so every connect (the first one
included — the daemon was deaf before it started) also reconciles each linked
project once, floored at five minutes so a flapping socket cannot spend its life
sweeping. `GET /api/asoode/socket` reports connection, events, reconciles,
catch-ups and suppressed echoes.

Note the socket needs a **ticket**, not the PAT: asoode's gateway keeps no
database and verifies signed JWTs only, so the PAT is exchanged at
`POST /account/socket-token` on each connect. A raw PAT is accepted and then
dropped with `transport error`.

**Our own writes are not reacted to.** asoode broadcasts every change to every
member and deliberately does not exclude the actor — and it drops the actor id
before the client sees it, so the payload cannot be used to tell. The writer
therefore records what it wrote and the listener consults it: a push of 29 tasks
now produces 29 suppressed echoes and **zero** board reads, where it used to
cost seven.

**Inbound only creates, never overwrites.** A task that exists on both sides is
left alone, because resolving a two-sided edit needs a conflict policy that has
not been decided. `memory_asoode_import` is the explicit path that does overwrite.

### Evidence on a task

`memory_task_attach(task_id, path)` copies a file into the task store and mirrors
it to the remote task — a screenshot proving a fix, a failing log, a generated
report. Content-addressed, so the same file on two tasks is one blob; sent once,
because no platform gives an attachment an idempotency key.

## Team / multi-device memory (git sync)

Bind a project to its source folder and its memory travels with the code
through git — across your devices and teammates:

```text
memory_link_folder("/path/to/project")   # bind an existing project to its folder
```

You can also set the folder when creating a project — the New Project dialog
has a **Project folder** field, and `memory_load_from_folder` binds it
automatically.

Once bound, the project's rules and decisions are mirrored to a committable
**`.claude-memory/`** snapshot in the project folder — one JSON file per
category, diff- and merge-friendly (no binary database, no embeddings). A
`git push` carries the latest memory; a teammate's `git pull` plus their next
session imports it back. The export runs at the end of each turn and the
import at session start (both via hooks), and the central database stays the
daemon's fast working copy.

Import is **safe by design**: it only adds new entries and applies edits that
are strictly newer — it never deletes, and never reverts a more recent local
change. Removing a rule is always explicit. Each project's memory is separate —
sharing one never exposes the others.

The snapshot's `manifest.json` carries a **`project_id`** — the project's stable
identity. Because it is committed with the code, moving or renaming the project
folder re-binds the existing project instead of registering a duplicate, and a
teammate's clone resolves to the same project on their machine.

**If memory is not reaching the snapshot**, look at
`~/.claude-memory-mcp/sync.log`: the hooks discard the sync command's stderr so
it can never disturb a Claude turn, so every failure writes a dated traceback
there instead. Export also warns when `.claude-memory/` is gitignored, since
an ignored snapshot never reaches your teammates.

## Importing rules from another project

Rules written for one project carry that project's specifics — its component
names, its paths, its stack. Copied verbatim into another project they read as
authoritative and quietly steer the agent wrong. So `memory_import_rules` (and
the UI's Import dialog) brings them in as **pending**:

- stored and visible in the **Pending** tab, but **not in force** — kept out of
  the injected rule block, out of search, out of session context, and out of the
  git snapshot;
- surfaced at the next `memory_session_start` with a brief telling the agent to
  rewrite each one for *this* codebase, and to **ask you** rather than guess when
  a rule cannot be translated without knowing something only you know;
- activated by `memory_adapt_pending(memory_id, title, content)` — which clears
  the flag, puts the rule in force from that moment on, and lets it sync — or
  dropped with `memory_discard_pending(memory_id, reason)`.

Pass `pending=False` to import text you already know is project-neutral.

## Architecture

- **Python + FastMCP** — the MCP server and HTTP daemon (Starlette + uvicorn)
- **DuckDB + VSS** — per-project memory storage with an HNSW cosine vector index
- **SQLite** — the local registry (project list + app settings); stdlib, no extra dependency
- **sentence-transformers** — local embeddings (`all-MiniLM-L6-v2`, 384-dim;
  a 50+ language multilingual preset is also available)
- **Layered design** — repositories → services → container → tool/HTTP layer
- **React + Vite + Tailwind** — the management UI, with hand-built
  shadcn-style components

Tasks live in their own DuckDB tables (`tasks`, `task_comments`,
`task_time_entries`) rather than as a memory category — which is what keeps them
out of the git-committed `.claude-memory/` snapshot however long the list grows.

Existing databases are migrated automatically on open, so older project
databases keep working after upgrades.

## Development

```bash
uv sync --all-extras
uv run pytest -v          # backend tests

cd frontend
npm install
npm run dev               # UI dev server (proxies the API to the daemon)
npm run build             # production build into frontend/dist
```

Run the daemon directly:

```bash
uv run memory-mcp serve
```

## Releasing

The Docker image is published only for **tagged releases** — never on ordinary
commits. Cut a release with the helper script:

```bash
./scripts/release.sh           # patch bump (0.6.0 -> 0.6.1)
./scripts/release.sh minor     # 0.6.0 -> 0.7.0
./scripts/release.sh 1.2.3     # explicit version
```

It runs the tests, bumps the version in `pyproject.toml` and the package,
commits, creates a `vX.Y.Z` tag, and pushes. The tag push triggers the workflow
that builds and publishes the multi-arch image to Docker Hub.

## License

MIT — see [LICENSE](LICENSE).
