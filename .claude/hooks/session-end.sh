#!/bin/bash
# Stop hook:
#  1. Export the project's memory to .claude-memory/ so the next git commit
#     and push carries the latest rules/decisions to other devices/people.
#  2. Remind Claude to persist the session summary.
# Silent when the working directory is not a registered memory project.
INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
BASE="${MEMORY_MCP_URL:-http://127.0.0.1:${MEMORY_MCP_DAEMON_PORT:-8765}}"
AUTH=()
[ -n "$MEMORY_MCP_TOKEN" ] && AUTH=(-H "Authorization: Bearer ${MEMORY_MCP_TOKEN}")
MEMORY_MCP_BIN="${MEMORY_MCP_BIN:-$HOME/.claude-memory-mcp/runtime/bin/memory-mcp}"

# Write the git-committable memory snapshot into the project folder. Local only:
# a remote server owns its own storage and is not synced from this machine.
[ -z "$MEMORY_MCP_URL" ] && [ -x "$MEMORY_MCP_BIN" ] && \
  "$MEMORY_MCP_BIN" sync export --cwd "$CWD" 2>/dev/null

curl -s -G --max-time 2 "${AUTH[@]}" "${BASE}/api/hook/rules" \
  --data-urlencode "cwd=${CWD}" --data-urlencode "mode=end" 2>/dev/null
exit 0
