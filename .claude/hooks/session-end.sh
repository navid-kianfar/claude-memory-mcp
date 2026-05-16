#!/bin/bash
# Stop hook: remind Claude to persist the session summary and any new rules /
# decisions before finishing. Silent when the cwd is not a memory project.
INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
PORT="${MEMORY_MCP_DAEMON_PORT:-8765}"
curl -s -G --max-time 2 "http://127.0.0.1:${PORT}/api/hook/rules" \
  --data-urlencode "cwd=${CWD}" --data-urlencode "mode=end" 2>/dev/null
exit 0
