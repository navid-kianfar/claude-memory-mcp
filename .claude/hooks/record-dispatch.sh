#!/bin/bash
# PreToolUse hook on the Agent tool: tell the daemon a subagent is being
# dispatched, so the session has a ledger of who it handed work to.
#
# WHY A SECOND PreToolUse SCRIPT. require-task.sh matches Edit|Write|NotebookEdit
# and must not run on anything else; this one matches Agent|Task. Claude Code
# creates one hook group per command, so two scripts on one event with different
# matchers is the supported shape.
#
# WHY PreToolUse AND NOT SubagentStart. PreToolUse is the oldest hook event and
# certainly exists on the installed CLI, `tool_input.subagent_type` is documented
# for the Agent tool, and it is the only event that fires BEFORE the agent runs -
# which is what lets it put a question in front of a dispatch to the wrong agent.
#
# IT ASKS - AND REFUSES EXACTLY ONE THING. The user's decision (2026-09-13,
# offered deny / ask / text-only): a generic `backend`/`frontend` dispatch where
# the repo's own specialist applies becomes a permission prompt naming that
# specialist, and so does a third agent OF A KIND while two of that kind are still
# running (2026-09-14, after one session ran 15+ agents at once). The one refusal is a
# SUBAGENT dispatching: only the lead starts agents. Every answer goes out as JSON
# on exit 0; this script never exits 2.
#
# IT FAILS OPEN, ALWAYS, like every other hook here: daemon down, curl missing,
# unreadable answer, no session id - exit 0, silently.
INPUT=$(cat)

# Parse the payload ONCE, and let Python build the JSON body too. Two things
# come back: CWD, so a dispatch in an unrelated repo costs no round trip, and
# PAYLOAD, the exact body to POST. Building JSON in bash would mean escaping a
# free-text description by hand, which is the one thing worth not doing here.
#
# Both values are shlex.quote'd on the Python side, so the eval cannot be
# injected into by a payload. A malformed payload, a missing python3, anything at
# all: both stay empty and the script exits 0, because a hook that fails must
# still allow the work.
#
# agent_id/agent_type are documented hook fields that were NOT verified on the
# installed CLI (2.1.236). Sent when present, empty when not; the daemon reads
# "absent" as unknown, never as "this is the lead".
eval "$(printf '%s' "$INPUT" | python3 -c '
import sys, json, shlex
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


# An Agent call with no subagent_type is the default agent; the ledger records
# what actually ran, so it records that name.
body = {
    "session_id": s(d.get("session_id")),
    "cwd": s(d.get("cwd")),
    "subagent_type": s(ti.get("subagent_type")) or "general-purpose",
    "description": s(ti.get("description")),
    "tool_use_id": s(d.get("tool_use_id")),
    "agent_id": s(d.get("agent_id")),
    "agent_type": s(d.get("agent_type")),
    "event": "PreToolUse",
}
print("CWD=" + shlex.quote(body["cwd"]))
print("PAYLOAD=" + shlex.quote(json.dumps(body)))
' 2>/dev/null)"
[ -z "$CWD" ] && exit 0

BASE="${MEMORY_MCP_URL:-http://127.0.0.1:${MEMORY_MCP_DAEMON_PORT:-8765}}"
AUTH=()
[ -n "$MEMORY_MCP_TOKEN" ] && AUTH=(-H "Authorization: Bearer ${MEMORY_MCP_TOKEN}")

# JSON, not a query string: the description is free text a person wrote, and a
# long dispatch brief does not belong in a URL.
ANSWER=$(printf '%s' "$PAYLOAD" | curl -s --max-time 1 "${AUTH[@]}" \
  -H 'Content-Type: application/json' --data @- \
  "${BASE}/api/hook/dispatch" 2>/dev/null)
[ -z "$ANSWER" ] && exit 0

# The daemon answers `{}`, `{"decision": "ask", "reason": ..., "context": ...}`,
# or `{"decision": "deny", "reason": ...}` - the last only for a dispatch made
# from inside a subagent. Anything else is not a decision this script acts on.
# The installed copy of this script is refreshed by a full `memory-mcp-setup`,
# never by the auto-updater.
#
# The output is Claude Code's documented PreToolUse decision (hooks reference,
# "PreToolUse decision control"): `hookSpecificOutput.permissionDecision: "ask"`
# prompts the user; for "ask", `permissionDecisionReason` is shown to the USER and
# not to Claude; `additionalContext` is added to CLAUDE's context beside the tool
# result (PreToolUse support since CLI 2.1.9). JSON is read only on exit 0, so the
# exit code stays 0. Python builds it, so quotes in a reason cannot break it.
DECISION=$(printf '%s' "$ANSWER" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)          # unreadable answer -> no decision
if not isinstance(d, dict) or d.get("decision") not in ("ask", "deny"):
    sys.exit(0)


def s(value):
    return value.strip() if isinstance(value, str) else ""


decision = d["decision"]
out = {
    "hookEventName": "PreToolUse",
    "permissionDecision": decision,
    "permissionDecisionReason": (
        s(d.get("reason")) or "Memory MCP asks you to confirm this dispatch."
    ),
}
if s(d.get("context")):
    out["additionalContext"] = s(d.get("context"))
# First line: the decision, so bash can tell a prompt from a refusal without
# parsing JSON. Second line: the output Claude Code reads.
print(decision)
print(json.dumps({"hookSpecificOutput": out}))
' 2>/dev/null)

[ -z "$DECISION" ] && exit 0
KIND=$(printf '%s\n' "$DECISION" | head -n 1)
OUTPUT=$(printf '%s\n' "$DECISION" | sed -n '2p')
[ -z "$OUTPUT" ] && exit 0

# The user's own off switch, the same one the gate honours - but it is checked
# HERE, not at the top, so turning the prompt off still leaves the ledger filling.
# A dispatch record is what makes "you never handed this to the specialist who
# owns it" sayable, and that advice is worth having even with nothing asked.
#
# It silences PROMPTS only. A deny is the "only the lead dispatches" guard, not a
# delegation preference, and switching prompts off must not reopen the swarm.
[ -n "$MEMORY_MCP_NO_GATE" ] && [ "$KIND" = "ask" ] && exit 0

printf '%s\n' "$OUTPUT"
exit 0
