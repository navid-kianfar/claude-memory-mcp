#!/bin/bash
# Codex notify wrapper: the session-end bookkeeping, on Codex's terms.
#
# Codex runs the command in its config's `notify` key when a turn completes
# and APPENDS the JSON payload as the LAST argument. Setup writes:
#
#     notify = ["<this script>", "memory-mcp", <the user's own notify...>]
#
# so Codex calls: this-script memory-mcp [user-command...] '<json>'.
#
# The only thing this daemon needs at a turn boundary is what the Claude Stop
# hook does: export the project's memory snapshot so the next commit carries
# it, and apply an approved update at a moment when no MCP call is in flight.
# That is exactly session-end.sh's job, so this wrapper is a thin bridge.
#
#   1. Payload: the last argument when it is a JSON object; otherwise stdin
#      (bounded wait - an open, silent stdin must not hang a turn); otherwise
#      nothing, and the cwd falls back to $PWD.
#   2. A chained user command (Codex has ONE notify slot; setup keeps what the
#      user had there) runs first, with the payload appended exactly as Codex
#      would have passed it.
#   3. session-end.sh, the SAME installed script the Claude harness uses.
#
# FAILS OPEN, ALWAYS: no payload, no daemon, no python3, a failing chained
# command - exit 0. A missed export is a missed sync; a failed notify must
# never break a Codex turn.

MARKER="memory-mcp"
STDIN_WAIT_SECONDS=2

ARGS=("$@")
if [ "${#ARGS[@]}" -gt 0 ] && [ "${ARGS[0]}" = "$MARKER" ]; then
  ARGS=("${ARGS[@]:1}")
fi

PAYLOAD=""
FROM_ARGV=""
COUNT=${#ARGS[@]}
if [ "$COUNT" -gt 0 ]; then
  LAST="${ARGS[$((COUNT - 1))]}"
  case "$LAST" in
    \{*)
      PAYLOAD="$LAST"
      FROM_ARGV=1
      ARGS=("${ARGS[@]:0:$((COUNT - 1))}")
      ;;
  esac
fi
if [ -z "$FROM_ARGV" ] && [ ! -t 0 ]; then
  IFS= read -r -d '' -t "$STDIN_WAIT_SECONDS" PAYLOAD
fi

if [ "${#ARGS[@]}" -gt 0 ]; then
  if [ -n "$FROM_ARGV" ]; then
    "${ARGS[@]}" "$PAYLOAD" </dev/null
  else
    printf '%s' "$PAYLOAD" | "${ARGS[@]}"
  fi
fi

CWD=$(MEMORY_MCP_NOTIFY_PAYLOAD="$PAYLOAD" python3 -c '
import json, os
try:
    d = json.loads(os.environ.get("MEMORY_MCP_NOTIFY_PAYLOAD", ""))
except Exception:
    d = {}
cwd = ""
if isinstance(d, dict):
    for key in ("cwd", "cwd_path", "working_directory", "directory"):
        value = d.get(key)
        if isinstance(value, str) and value:
            cwd = value
            break
print(cwd)
' 2>/dev/null)

[ -z "$CWD" ] && CWD="$PWD"
[ -d "$CWD" ] || exit 0

END="$HOME/.claude-memory-mcp/hooks/session-end.sh"
[ -f "$END" ] || exit 0

printf '{"cwd": %s}\n' "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$CWD" 2>/dev/null)" \
  | bash "$END" >/dev/null 2>&1
exit 0
