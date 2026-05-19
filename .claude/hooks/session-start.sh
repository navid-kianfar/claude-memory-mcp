#!/bin/bash
# SessionStart hook:
#  1. Import the project's .claude-memory/ snapshot into the central store
#     (picks up rules/decisions a git pull brought in).
#  2. Nudge Claude to call memory_session_start so rules/context are loaded.
# Silent when the working directory is not a registered memory project.
INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
PORT="${MEMORY_MCP_DAEMON_PORT:-8765}"
MEMORY_MCP_BIN="${MEMORY_MCP_BIN:-$HOME/.claude-memory-mcp/runtime/bin/memory-mcp}"

# Register this folder as a project (if it is a git repo and not registered),
# so it appears in the management UI even before it has any rules.
curl -s -G --max-time 3 "http://127.0.0.1:${PORT}/api/hook/auto-register" \
  --data-urlencode "cwd=${CWD}" 2>/dev/null

# Pull any git-synced project memory into the central store.
[ -x "$MEMORY_MCP_BIN" ] && "$MEMORY_MCP_BIN" sync import --cwd "$CWD" 2>/dev/null

curl -s -G --max-time 2 "http://127.0.0.1:${PORT}/api/hook/rules" \
  --data-urlencode "cwd=${CWD}" --data-urlencode "mode=intro" 2>/dev/null
exit 0
