#!/bin/bash
# SessionStart hook: nudge Claude to call memory_session_start at the start of
# a conversation, so rules / last summary / sprint goals / decisions are loaded.
# Silent when the working directory is not a registered memory project.
INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
PORT="${MEMORY_MCP_DAEMON_PORT:-8765}"
curl -s -G --max-time 2 "http://127.0.0.1:${PORT}/api/hook/rules" \
  --data-urlencode "cwd=${CWD}" --data-urlencode "mode=intro" 2>/dev/null
exit 0
