#!/bin/bash
# Codex notify wrapper: the session-end bookkeeping, on Codex's terms.
#
# Codex invokes the program named in its config's `notify` key when a turn
# completes, with a JSON payload on stdin. The only thing this daemon needs at
# a turn boundary is what the Claude Stop hook does: export the project's
# memory snapshot so the next commit carries it, and apply an approved update
# at a moment when no MCP call is in flight. That is exactly session-end.sh's
# job, so this wrapper is a thin bridge: read the payload for a cwd, fall back
# to the working directory Codex ran in, and hand it to the SAME installed
# script the Claude harness uses.
#
# FAILS OPEN, ALWAYS: no payload, no daemon, no python3 - exit 0, silently. A
# missed export is a missed sync; a failed notify must never break a Codex
# turn.

INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
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

printf '{"cwd": %s}\n' "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$CWD")" \
  | bash "$END" >/dev/null 2>&1
exit 0
