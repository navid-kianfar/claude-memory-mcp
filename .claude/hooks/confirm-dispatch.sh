#!/bin/bash
# PostToolUse hook on the dispatch tool (Claude/ZCode `Agent`, DeepSeek Harness
# `subagent`/`subagent_fork`): tell the daemon a dispatch actually ran.
#
# WHY IT EXISTS. record-dispatch.sh records each dispatch at PreToolUse, which
# fires BEFORE the user answers a permission prompt. When it asked, its row is
# only an attempt: a user who answers No leaves a row for an agent that never
# ran, and that row used to clear the delegation escalation. PostToolUse fires
# only when the tool ran, so this confirms the attempt; the daemon ignores it
# for a dispatch that was never prompted (enforcement.confirm_dispatch).
#
# If this hook never fires (an older client, a failed call), a prompted
# dispatch simply stays unconfirmed and the escalation keeps naming the agent -
# the safe direction for an advisory line.
#
# IT GATES NOTHING AND FAILS OPEN: daemon down, curl or python3 missing, a
# malformed payload - exit 0, silently, with no output.
INPUT=$(cat)

# One python3 call parses the payload and builds the JSON body; the values come
# back shlex.quote'd, so the eval cannot be injected into by a payload. The body
# carries the same fields record-dispatch.sh sends, so the daemon derives the
# same role from it (enforcement.dispatch_role).
eval "$(printf '%s' "$INPUT" | python3 -c '
import os, sys, json, shlex
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
ti = d.get("tool_input")
if not isinstance(ti, dict):
    ti = {}


def s(value):
    return value if isinstance(value, str) else ""


name = s(ti.get("name"))
tool = s(d.get("tool_name"))
unnamed_default = not name and tool != "subagent_fork"
body = {
    "session_id": s(d.get("session_id")),
    "cwd": s(d.get("cwd")),
    "subagent_type": s(ti.get("subagent_type"))
    or ("general-purpose" if unnamed_default else ""),
    "name": name,
    "tool_name": tool,
    "description": s(ti.get("description")),
    "tool_use_id": s(d.get("tool_use_id")),
    "agent_id": s(d.get("agent_id")),
    "agent_type": s(d.get("agent_type")),
    "harness": os.environ.get("MEMORY_MCP_HARNESS", ""),
    "event": "PostToolUse",
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
