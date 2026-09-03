# Tasks + asoode bridge — documentation set

Captured 2026-09-03 from a three-agent audit of both codebases, so neither repo has to be re-read to
work on this. **Where a doc and the code disagree, the code wins** — correct the doc as you go.

| File | What it holds |
|---|---|
| [`01-asoode-analysis.md`](01-asoode-analysis.md) | Full map of `asoode`: the existing MCP server's 20 tools, the Prisma data model, every task endpoint, the RabbitMQ→Socket.IO event pipeline with payloads, PAT auth, ports, and a verified gap list |
| [`02-memory-mcp-analysis.md`](02-memory-mcp-analysis.md) | Full map of this repo: layering rules, DuckDB + SQLite registry schemas, migration contract, project identity, the **pending/adaptation subsystem**, all three sync mechanisms, the remote gateway, the daemon/UI, and every extension seam |
| [`03-claude-ui-surfaces.md`](03-claude-ui-surfaces.md) | Whether asoode can be shown inside Claude. Answer: the Browser pane + `.claude/launch.json`; **plugins have no UI extension point** |
| [`04-design-decisions.md`](04-design-decisions.md) | What we decided and why — local-first tasks, optional configurable asoode, offline outbox, live socket inbound, and the DuckDB-vs-JSON answer |
| [`05-session-prompt.md`](05-session-prompt.md) | The session-opening prompt for **this** repo's work (Phase 1: the standalone task store) |
| [`06-agent-team.md`](06-agent-team.md) | **Phase 3** — a standing team of specialised agents (pm / design / frontend / backend / e2e) sharing the memory and the task board |

The asoode-side work lives in that repo:

- `../../../asoode/docs/claude-bridge-backlog.md` — prioritised changes with file:line change sites
- `../../../asoode/docs/claude-bridge-prompt.md` — the session-opening prompt

## Sequence

**Phase 1** — the standalone task store in memory-mcp, working with no asoode at all.
**Phase 2** — the optional asoode bridge (outbox out, live socket in). Gated on asoode's P0 items.
**Phase 3** — the agent team, which depends on Phase 1 for shared state and handoffs.

## The short version

- memory-mcp gets a **real, complete task store** in per-project DuckDB. It must work fully with no
  asoode at all.
- asoode integration is **per-project, opt-in, and fully configurable** (on-premise: base URL, socket
  URL, project, work package). Never auto-bound. PAT stored in `app_settings['cred:<url>']`, never in
  the committed snapshot.
- Writes go through an **outbox**; connection loss is a normal state. Drain on reconnect, then reconcile.
- Inbound arrives over a **live Socket.IO subscription** from an asyncio task in the daemon lifespan —
  which makes asoode's socket authentication a hard blocker.
- **Tasks never enter the `.claude-memory/` JSON snapshot.** That is what keeps the "JSON grows to
  100MB" scenario from ever happening.
- One asoode PAT must authenticate the backend, the socket, **and** the frontend panel — so Claude's
  Browser pane can sign in to the board without a password.
