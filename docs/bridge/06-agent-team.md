# Phase 3 — the agent team

> Status: **idea, agreed for later.** Not scheduled until Phase 1 (the task module) ships.
> Sequence stays: **Phase 1 tasks → Phase 2 asoode bridge → Phase 3 agent team.**

## The idea

A standing team of specialised agents that work on my projects using the shared memory and the task
board: a **project manager**, a **design/UI/UX** agent, a **frontend** agent, a **backend** agent, and
an **e2e test** agent.

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
  YAML frontmatter carrying at least `name`, `description`, `model`, `tools`, and `isolation`.
  ⚠️ **Verify the exact frontmatter schema against the current docs before writing the files** — the
  specs below record *intent*, not confirmed syntax.
- **Subagents inherit the session's MCP servers.** So each agent can call `memory_*` and asoode's tools
  directly. This is the "using its memory and project-management skills" requirement, already solved.
- **`isolation: worktree`** gives an agent its own git worktree, auto-cleaned if unchanged. Frontend and
  backend agents can work the same repo in parallel without colliding.
  (asoode already has a `.claude/worktrees` directory, so this path is proven here.)
- **Skills** are available to agents. Seven design skills are already installed at `~/.claude/skills/`:
  `banner-design`, `brand`, `design`, `design-system`, `slides`, `ui-styling`, `ui-ux-pro-max`.
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

Gets the seven installed design skills. Works from `ui-ux-pro-max` for interface decisions and
`design-system` for token architecture. Writes specs and tokens, not application code — the frontend
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

## Orchestration — who dispatches

**The main session dispatches. The `pm` agent does not.**

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

Because agents share no conversation, every handoff must be written down. Two channels, both already
being built:

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

- Does `pm` write tasks directly, or propose them for approval? (Leaning: propose, using the same
  `triage` flag the inbound asoode tasks use — one triage surface, not two.)
- Per-project agent overrides, or one user-level team? (Leaning: user-level team, project-level
  `CLAUDE.md` supplies the project specifics.)
- Should `design` and `frontend` be one agent? They hand off constantly. Worth trying as two first,
  merging if the handoff cost dominates.
