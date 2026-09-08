#!/bin/bash
# PreToolUse hook: refuse a mutating tool call when the project's bound board
# has no task in progress.
#
# WHY THIS EXISTS. Every other hook here only prints into the model's context,
# and a rule that says "put the work on the board first" was followed roughly
# 70% of the time. Text can remind; it cannot require. This one can, because
# PreToolUse is the only hook whose exit status decides whether the tool runs.
#
# IT FAILS OPEN, ALWAYS. Not a memory project, not bound to a board, daemon
# unreachable, curl missing, malformed answer, anything unexpected: allow. A
# gate that stops someone editing a file because a board is down is a far worse
# bug than the one it fixes, so the ONLY path to a block is the daemon
# explicitly answering allow=false.
INPUT=$(cat)

# The user's own off switch, so this never has to be argued with.
[ -n "$MEMORY_MCP_NO_GATE" ] && exit 0

CWD=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)
[ -z "$CWD" ] && exit 0

BASE="${MEMORY_MCP_URL:-http://127.0.0.1:${MEMORY_MCP_DAEMON_PORT:-8765}}"
AUTH=()
[ -n "$MEMORY_MCP_TOKEN" ] && AUTH=(-H "Authorization: Bearer ${MEMORY_MCP_TOKEN}")

# Short timeout: this runs before every edit, so it must never be felt. A
# timeout is an allow, which is why 2 seconds is safe to insist on.
ANSWER=$(curl -s -G --max-time 2 "${AUTH[@]}" "${BASE}/api/hook/gate" \
  --data-urlencode "cwd=${CWD}" 2>/dev/null)
[ -z "$ANSWER" ] && exit 0

REASON=$(printf '%s' "$ANSWER" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)          # unreadable answer -> allow
if d.get('allow', True):
    sys.exit(0)
print(d.get('reason') or 'No task is in progress for this project.')
" 2>/dev/null)

# Nothing printed means the decision was allow (or we could not read it).
[ -z "$REASON" ] && exit 0

# Exit 2 is the blocking status: the tool call does not run and stderr is fed
# back to the model, which is why the reason has to name the tool to call next.
printf '%s\n' "$REASON" >&2
exit 2
