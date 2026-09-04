# Phase 3 — the agent team

> Status: **built, 2026-09-04, and not yet dispatched.** EIGHT definitions live in
> [`agents/`](../../agents/) and `memory-mcp-setup` installs them to `~/.claude/agents/`;
> role-aware claiming is in the task store at schema v12. What remains is a real dispatch —
> see *What was verified* below for exactly which assumptions are now checked and which are
> still open.
>
> Sequence was: **Phase 1 tasks → Phase 2 asoode bridge → Phase 3 agent team.** Phases 1 and 2
> shipped, which is what unblocked this one.

## The idea

A standing team of specialised agents that work on my projects using the shared memory and the task
board. The user specified this on 2026-09-04 and it supersedes the five-agent sketch below:
**pm, backend, frontend, designer, test, reviewer, devops, docs** — each framed as a 20+ year
senior practitioner, all on `claude-opus-5`, with token economy as an explicit constraint in
every definition.

`reviewer`, `devops` and `docs` were added because five agents all certifying their own work is
the gap that this project's own history keeps punishing. `design` became `designer` and `e2e`
became `test` (it covers e2e, integration, unit and preview testing).

**pm orchestrates and has full tools.** Its fan-out exists to keep a whole codebase out of its
own context — explorers survey, one digester folds their findings into a summary, pm consumes
the summary — not to restrict what it may do. Verified: a subagent CAN spawn subagents.

## Why it is Phase 3, not Phase 1

Subagents have two properties that decide everything about this design:

- **They are stateless per invocation.** Each `Agent` call starts with a fresh context. `SendMessage`
  can continue one *within* a session, but nothing survives across sessions on its own.
- **They share no conversation.** An agent receives a prompt, not the transcript. Nothing the main
  session learned reaches them unless it is written down.

So a team built today would be five agents that forget everything between invocations and re-derive the
same conclusions in parallel. The fix is not a better prompt — it is shared, durable state:

| Need | Provided by |
|---|---|
| Shared long-term knowledge (decisions, rules, architecture) | **memory MCP** — already exists |
| Shared work queue and handoff channel | **the task module** — Phase 1 |
| Knowing what to work on without being asked | **session brief + socket inbound** — Phase 1 / Phase 2 |

That is the whole dependency. The five agent files themselves are roughly an hour of work.

## What is actually supported

Confirmed capabilities (see `03-claude-ui-surfaces.md` for the UI side of the same question):

- **Subagents** are markdown files in `.claude/agents/` (project) or `~/.claude/agents/` (user), with
  YAML frontmatter carrying `name`, `description`, `model`, `tools`, `color` and `isolation`.
  ✅ **Confirmed 2026-09-04** against the installed plugin agents — the earlier warning that this
  was intent rather than syntax is resolved.
- **Subagents inherit the session's MCP servers.** ✅ **Verified 2026-09-04 by probe**, not assumed:
  all 66 `mcp__memory__*` tools are available to a subagent immediately, with no `ToolSearch` and no
  extra configuration, and the server's own instructions are injected verbatim too.
- **`isolation: worktree`** gives an agent its own git worktree, auto-cleaned if unchanged. Frontend and
  backend agents can work the same repo in parallel without colliding.
  (asoode already has a `.claude/worktrees` directory, so this path is proven here.)
- **Skills** are available to agents. Six design skills are installed at `~/.claude/skills/`:
  `design` (the comprehensive entry point the `designer` agent uses), `design-system`,
  `ui-styling`, `brand`, `slides`, `banner-design`. They are invoked on demand rather than
  preloaded via `skills:`, which would pull each one's full content in on every dispatch.
- **Plugins** can bundle agents + skills + hooks + `.mcp.json` and be distributed via a marketplace, so
  the team follows into every project.

## The five agents

| Agent | Model | Tools | Isolation | Owns |
|---|---|---|---|---|
| `pm` | opus | `memory_*`, asoode MCP, Read, Grep, Glob — **no Edit/Write** | none | Breaking work down, writing tasks, updating state, keeping the board honest |
| `design` | opus | design skills, Read, Write, Browser pane | none | Design tokens, component specs, UI/UX review |
| `frontend` | opus | Edit, Write, Bash, Read, Grep, Browser pane | **worktree** | UI implementation, verified in the browser |
| `backend` | opus | Edit, Write, Bash, Read, Grep — no browser | **worktree** | APIs, services, schema, migrations |
| `e2e` | sonnet | Bash, Browser pane, Read | **worktree** | Running and extending the e2e suite |

### `pm`

Reads the board and the memory, produces a plan, writes tasks. **Deliberately has no Edit or Write** —
it plans, it does not code. That constraint is what keeps it from quietly becoming a sixth
implementation agent.

Inputs: `memory_search`, `memory_get_rules`, the asoode board via `list_board` / `my_tasks`.
Outputs: tasks (with description and assignee), state changes, and a `decision` memory when it makes a
call worth remembering.

### `design`

Works from `/design` — the comprehensive design skill — and `design-system` for token architecture. Writes specs and tokens, not application code — the frontend
agent implements them.

### `frontend` / `backend`

The two that genuinely need `isolation: worktree`, because they are the two most likely to run at the
same time on the same repo. Each verifies its own work: the frontend agent through the Browser pane,
the backend agent through tests.

In a monorepo like asoode, the `match_paths` routing on `project_links` (see `04-design-decisions.md`)
already maps `apps/backend/**` and `apps/frontend/**` to different boards — the same split the agents
work along.

### `e2e`

`asoode/E2E-TEST-PLAN.md` is already the brief for this one. Sonnet rather than opus: running suites
and reporting failures does not need the larger model, and this agent will run most often.

## What was verified, and what is still open

Probed 2026-09-04 with a real subagent. Recorded here because the rest of this document was
written from assumption, and it is now possible to say which parts survived contact.

**Confirmed:**

- Subagents inherit the memory MCP server in full — 66 tools, immediately, plus the server's
  own instructions. This was the load-bearing assumption of the whole design.
- The frontmatter schema (see above).

**Found, and it changed the definitions:**

- `memory_search` does **not** search tasks. A probe searched "agent team" and got zero
  results while a task by that exact name sat in the queue — memories and tasks are separate
  stores. Every definition originally said "load context with `memory_get_rules` and
  `memory_search`", which would have silently missed the entire task queue. All five now read
  the queue explicitly.

**Found, and not yet acted on:**

- Subagents inherit the full **write** surface: `memory_store`, `memory_task_add`,
  `memory_task_done`, `memory_asoode_push`, with no additional gate. The `pm` agent's "no
  Edit/Write/Bash" restricts the filesystem, not the board. Worth a decision before the team
  runs unattended.

**Still open, all needing a restart to test:**

- Whether an explicit `tools:` allowlist filters inherited MCP tools. The probe used
  `general-purpose`, which has `tools: *`, so it cannot answer this. Every definition names its
  `mcp__memory__*` tools explicitly meanwhile, which fails safe either way.
- Whether `isolation: worktree` behaves as assumed.
- Whether a handoff survives between two agents.

**One operational fact, learned the hard way:** Claude Code enumerates `~/.claude/agents/` at
**session start**. Agents installed mid-session are not dispatchable until a restart —
`Agent(subagent_type="pm")` returns *"Agent type 'pm' not found"*.

**Cost baseline:** one subagent doing three read-only lookups cost 61,742 tokens and ~50
seconds. That is the floor for delegating anything, and the concrete argument behind
"default to one agent" below.

## Orchestration — who dispatches

**The main session IS the lead. It does not dispatch a pm agent to be one.**

Settled 2026-09-04 after measuring. The orchestration brief is injected by the
`UserPromptSubmit` hook, so the session holding the conversation is the one delegating, and
`pm` is excluded from the roster that brief advertises. `agent: pm` in settings.json was tried
first and retired: some clients ignore it silently, and alongside the hook it double-injects.

Agents can nest in principle, but it degrades quickly and subagent results are not shown to the user —
the parent has to relay them, so a two-level tree loses information at every hop. Options, in order of
preference:

1. **Main session as orchestrator** — read the board, delegate to one or more agents, relay results.
   Simple, interactive, interruptible.
2. **A `Workflow` script** — deterministic fan-out with real parallelism and phases. Must be explicitly
   opted into and is token-expensive; right for a big batch, wrong for everyday work.
3. **Scheduled runs** (`CronCreate`, scheduled tasks, `/loop`) — for standing jobs like a nightly e2e
   pass. This is the closest thing to "the team works while I'm away", and it is the only autonomous
   trigger that exists.

## Handoff protocol

Work is routed by the `role` column on a task (schema v12): `memory_task_claim_next(role=...)`
offers an agent its own role's work plus unroled work, and never another role's. A task with no
role stays claimable by anyone, which is what keeps every pre-existing task visible.

Because agents share no conversation, every handoff must be written down. Two channels, both already
built:

- **A task comment** for anything tied to one piece of work — "API shape decided, see below",
  "blocked on the migration". The `kind` field on `task_comments` (Phase 1) distinguishes a note from a
  decision or a rule.
- **A memory** for anything that outlives the task — a `decision`, an `architecture` note, a rule.
  Long-lived, searchable, and loaded into every future session automatically.

Rule of thumb to encode in each agent's definition: *if the next agent needs it, comment on the task;
if next month needs it, store a memory.*

## Packaging

Ship the team as a **plugin**: `agents/`, the design `skills/`, `hooks/hooks.json`, and `.mcp.json`
pointing at both MCP servers. One install per machine, and the team is available in every project.

⚠️ `.claude/launch.json` is **not** a plugin component (`03-claude-ui-surfaces.md` §2) — Browser pane
config stays per-repo and committed.

## Honest limits

- **No agent watches for work.** Nothing polls asoode for a new assignment. Autonomy comes from
  scheduled runs, or from the session brief telling Claude what is queued at the start of a session.
- **Subagent output is not shown to the user** — the parent relays it. Deep trees lose fidelity.
- **Cost scales with the team.** Five opus agents on one task is a lot of tokens for what is often one
  agent's job. Default to delegating to *one* agent; fan out only when the work is genuinely parallel.
- **Worktree isolation is per-agent, not per-task.** Two invocations of `backend` in the same session
  may or may not share a worktree — verify before relying on it.

## Open questions

- ~~Does `pm` write tasks directly, or propose them for approval?~~ **Settled: propose**, using the
  same `triage` flag the inbound asoode tasks use — one triage surface, not two.
- ~~Per-project agent overrides, or one user-level team?~~ **Settled: one user-level team**, which is
  why `agents/` sits at the repo root and installs to `~/.claude/agents/` rather than living in
  `.claude/agents/`. Project-level `CLAUDE.md` supplies the specifics.
- Should a subagent be allowed to write to the board at all? Raised by the probe above; nobody has
  decided.
- Should `design` and `frontend` be one agent? They hand off constantly. Worth trying as two first,
  merging if the handoff cost dominates.
