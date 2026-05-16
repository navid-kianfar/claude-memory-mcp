#!/bin/bash
# UserPromptSubmit hook: inject the current project's binding rules into context
# on every turn, so rules survive context compaction and never get forgotten.
#
# Talks to the running memory-mcp daemon. Prints nothing when the working
# directory is not a registered memory project, so it is safe to install
# globally without adding noise to unrelated repositories.
INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
PORT="${MEMORY_MCP_DAEMON_PORT:-8765}"
curl -s -G --max-time 2 "http://127.0.0.1:${PORT}/api/hook/rules" \
  --data-urlencode "cwd=${CWD}" --data-urlencode "mode=rules" 2>/dev/null
exit 0
