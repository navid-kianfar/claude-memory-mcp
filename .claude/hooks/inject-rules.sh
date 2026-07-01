#!/bin/bash
# UserPromptSubmit hook: inject the current project's binding rules into context
# on every turn, so rules survive context compaction and never get forgotten.
#
# Talks to the running memory-mcp daemon. Prints nothing when the working
# directory is not a registered memory project, so it is safe to install
# globally without adding noise to unrelated repositories.
INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
# Default to the local daemon; MEMORY_MCP_URL points at a remote server and
# MEMORY_MCP_TOKEN (if set) authenticates to it. With neither set this is the
# original localhost request, byte-for-byte.
BASE="${MEMORY_MCP_URL:-http://127.0.0.1:${MEMORY_MCP_DAEMON_PORT:-8765}}"
AUTH=()
[ -n "$MEMORY_MCP_TOKEN" ] && AUTH=(-H "Authorization: Bearer ${MEMORY_MCP_TOKEN}")
curl -s -G --max-time 2 "${AUTH[@]}" "${BASE}/api/hook/rules" \
  --data-urlencode "cwd=${CWD}" --data-urlencode "mode=rules" 2>/dev/null
exit 0
