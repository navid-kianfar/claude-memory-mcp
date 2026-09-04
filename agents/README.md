# The agent team

Five specialised agents that work on projects using this server's memory and task board:
`pm`, `design`, `frontend`, `backend`, `e2e`. The design is in
[`docs/bridge/06-agent-team.md`](../docs/bridge/06-agent-team.md).

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

3. Restart Claude Code so it re-reads the directory.

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

Frontmatter carries `name`, `description`, `model`, `tools`, and `isolation`
(`isolation: worktree` gives the agent its own git worktree, auto-cleaned if unchanged).

Two things every definition must say, both consequences of how subagents work:

- **Load your own context.** A subagent receives a prompt, not the transcript. Nothing the
  main session learned reaches it unless the agent fetches it with `memory_get_rules` and
  `memory_search`.
- **Write handoffs down.** Agents share no conversation, so an unwritten handoff is a lost
  one. *If the next agent needs it, comment on the task; if next month needs it, store a
  memory.*
