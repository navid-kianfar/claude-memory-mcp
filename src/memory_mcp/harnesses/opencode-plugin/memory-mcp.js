// memory-mcp lifecycle bridge for OpenCode.
//
// OpenCode has no shell-hook surface like Claude Code's; it loads plugins from
// ~/.config/opencode/plugins/ (and .opencode/plugins/ per project), whose
// exported async functions return a hooks object. This file is INSTALLED by
// `memory-mcp-setup` (the OpenCode adapter in src/memory_mcp/harnesses/
// opencode.py) - edit the source in the repo, not this copy.
//
// What it wires, onto the SAME daemon endpoints the Claude hooks use:
// - session.created -> session-start.sh  (snapshot import + folder claim -
//   the SessionStart equivalent)
// - session.idle    -> session-end.sh    (snapshot export + update apply -
//   the Stop equivalent)
//
// The scripts themselves are harness-neutral: they read a JSON payload on
// stdin (cwd is what matters; the daemon resolves the project from it), print
// plain text that OpenCode logs, and FAIL OPEN - a daemon that is down, a
// non-memory project, anything at all degrades to silence, never to a blocked
// session. No per-turn rules line exists in OpenCode (no UserPromptSubmit
// equivalent); rules reach the model through the MCP server's instructions
// and memory_session_start.

const HOOKS_DIR = `${process.env.HOME}/.claude-memory-mcp/hooks`;

export const MemoryMcpPlugin = async ({ directory, $ }) => {
  const run = async (script) => {
    if (!directory) return;
    try {
      const payload = JSON.stringify({ cwd: directory });
      await $`echo ${payload} | bash ${HOOKS_DIR}/${script}`;
    } catch {
      // Never raise into the app: a failed hook is a missed sync, not a
      // broken session. The scripts log their own failures to sync.log.
    }
  };

  return {
    event: async ({ event }) => {
      if (event.type === "session.created") {
        await run("session-start.sh");
      } else if (event.type === "session.idle") {
        await run("session-end.sh");
      }
    },
  };
};
