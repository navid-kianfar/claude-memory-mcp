#!/bin/bash
# SubagentStop hook: tell the daemon one of this session's agents finished.
#
# WHY IT EXISTS. record-dispatch.sh counts agents IN at PreToolUse; this counts
# them OUT. The difference is "how many are running right now", which is what
# lets the dispatch hook put a prompt in front of a third concurrent agent
# (2026-09-14: one session ran 15+ agents at once and hit the usage limit).
#
# WHY A SEPARATE SCRIPT. Hook matchers are installed per script (setup.py
# HOOK_MATCHERS). record-dispatch.sh carries `Agent|Task`, which on SubagentStop
# would be matched against the AGENT TYPE ("python", "reviewer") and never fire.
# This one takes no matcher, so it fires for every agent.
#
# IT GATES NOTHING AND FAILS OPEN: daemon down, curl or python3 missing, a
# malformed payload - exit 0, silently. A missed stop only makes the running
# count high for up to an hour (registry.RUNNING_WINDOW_MINUTES).
INPUT=$(cat)

# One python3 call parses the payload and builds the JSON body; the values come
# back shlex.quote'd, so the eval cannot be injected into by a payload.
eval "$(printf '%s' "$INPUT" | python3 -c '
import sys, json, shlex
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}


def s(value):
    return value if isinstance(value, str) else ""


body = {
    "session_id": s(d.get("session_id")),
    "cwd": s(d.get("cwd")),
    "agent_id": s(d.get("agent_id")),
    "agent_type": s(d.get("agent_type")),
    "event": "SubagentStop",
}
print("SID=" + shlex.quote(body["session_id"]))
print("PAYLOAD=" + shlex.quote(json.dumps(body)))
' 2>/dev/null)"
[ -z "$SID" ] && exit 0

BASE="${MEMORY_MCP_URL:-http://127.0.0.1:${MEMORY_MCP_DAEMON_PORT:-8765}}"
AUTH=()
[ -n "$MEMORY_MCP_TOKEN" ] && AUTH=(-H "Authorization: Bearer ${MEMORY_MCP_TOKEN}")

printf '%s' "$PAYLOAD" | curl -s --max-time 1 "${AUTH[@]}" \
  -H 'Content-Type: application/json' --data @- \
  "${BASE}/api/hook/dispatch" >/dev/null 2>&1
exit 0
