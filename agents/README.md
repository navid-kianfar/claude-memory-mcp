# The agent team

Eight specialised agents that work on projects using this server's memory and task board:
`pm`, `backend`, `frontend`, `designer`, `test`, `reviewer`, `devops`, `docs`. The design and
its verification status are in [`docs/bridge/06-agent-team.md`](../docs/bridge/06-agent-team.md).

**The main session is the lead.** It orchestrates directly rather than dispatching `pm` to do
it, because a subagent's output is never shown to the user and cannot be redirected once it is
running. The brief that says so is injected by the `UserPromptSubmit` hook
(`enforcement.agent_team_intro` / `agent_team_line`), and `pm` is excluded from the roster it
advertises. The `pm.md` file still exists, for a planning job worth doing in isolated context.

## This folder is the source of truth

Claude Code reads agent definitions from `~/.claude/agents/`. **That copy is a build
artefact — do not edit it.** An agent edited in place there has no history, no review, and no
way to tell why a prompt changed or whether the change helped. Editing here instead is what
makes the team maintainable and its prompts improvable.

```
agents/<name>.md   →   ~/.claude/agents/<name>.md
```

`setup_agents()` in [`src/memory_mcp/setup.py`](../src/memory_mcp/setup.py) does the copy,
following the same pattern the hook scripts already use.

## Editing an agent

1. Edit `agents/<name>.md`.
2. Reinstall:

   ```bash
   uv run memory-mcp-setup
   ```

3. Restart Claude Code so it re-reads the directory. Agents installed mid-session are not
   dispatchable until then.

Installed copies are **overwritten** on every run, exactly as the hook scripts are. Anything
you changed under `~/.claude/agents/` is lost — which is the point: the edit belongs here.

## Adding and retiring

Add an agent by dropping a new `<name>.md` in this folder and re-running setup. Retire one by
deleting it here — setup removes its installed copy too.

Retirement only ever deletes a file **this installer previously wrote**. The list of installed
names is kept in `~/.claude-memory-mcp/agents-installed.json`, so an agent you hand-wrote
directly in `~/.claude/agents/` is never touched.

`README.md` is documentation, not an agent, and is not installed.

## What goes in a definition

Frontmatter carries `name`, `description`, `model`, `effort`, `color`, and optionally `tools`,
`disallowedTools`, `skills` and `isolation`.

- **`model`** — every agent here pins `claude-opus-5` by its full id rather than the `opus`
  alias, so a new Opus cannot silently change how the team behaves.
- **`effort`** — `low` | `medium` | `high` | `xhigh` | `max`. `max` for pm, designer, test and
  reviewer; `xhigh` for backend, frontend and devops; `high` for docs, which runs often and
  writes prose rather than reasoning deeply.
- **`disallowedTools`, not `tools`** — prefer the denylist. An allowlist risks filtering out
  the inherited MCP tools an agent needs, whereas a denylist leaves them intact. `reviewer` is
  the one real use: it is denied `Edit`/`Write` because a reviewer who can fix its own findings
  stops reviewing.
- **`isolation: worktree`** — gives the agent its own git worktree. `frontend`, `backend` and
  `test` have it so they can run at once. **Its work does not arrive on its own:** the changes
  stay in the worktree until someone merges them deliberately.
- **`skills`** — mostly *absent* on purpose. It preloads a skill's full content at startup on
  **every** dispatch, and most jobs need one or two, so `designer` names `/design` in its body
  and loads on demand instead. Only `reviewer` preloads, because it uses `code-review` and
  `security-review` every run.

`description` is the only part your session pays for continuously — it is what the router sees
in the agent list, and the body is not loaded until the agent is actually dispatched. Keep it
to one specific sentence.

## Three things every definition must say

All three are consequences of how subagents actually behave, each verified rather than assumed:

- **Load your own context.** A subagent receives a prompt, not the transcript. Nothing the
  main session learned reaches it unless the agent fetches it with `memory_get_rules` and
  `memory_search` — **and `memory_task_list` / `memory_task_get`, because `memory_search` does
  not search tasks.** Memories and tasks are separate stores; a search for a topic will not
  find the task describing it.
- **Write handoffs down.** Agents share no conversation, so an unwritten handoff is a lost
  one. *If the next agent needs it, comment on the task; if next month needs it, store a
  memory.* This works: `pm` posted a plan as a task comment and `frontend` picked it up from
  the board with nothing relayed through the session.
- **Mind the tokens.** A dispatch costs ~60k at the floor, measured. Say so, and say what the
  agent should read rather than letting it survey the repo.

## One more thing worth knowing

Subagents inherit the memory MCP server **in full, including its write surface** — 66 tools,
plus the server's own instructions. An agent's `tools` list restrains its filesystem, not the
board: any dispatched agent can write memories, tasks and push to asoode.
