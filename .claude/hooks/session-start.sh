#!/bin/bash
# SessionStart hook:
#  1. Import the project's .claude-memory/ snapshot into the central store
#     (picks up rules/decisions a git pull brought in).
#  2. Nudge Claude to call memory_session_start so rules/context are loaded.
# Silent when the working directory is not a registered memory project.
INPUT=$(cat)
# Parse the payload ONCE into shell variables. Until now each script pulled out
# `cwd` and threw the rest away, which is why the daemon could not tell which
# session dispatched an agent or edited a file.
#
# Every value is shlex.quote'd on the Python side, so the eval cannot be injected
# into by a payload. A malformed payload, a missing python3, anything at all:
# every variable stays empty and the script carries on, because a hook that
# fails must still exit 0.
#
# agent_id/agent_type are documented hook fields that were NOT verified on the
# installed CLI (2.1.236). Forwarded when present, empty when not; the daemon
# reads "absent" as unknown, never as "this is the lead".
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
pairs = (("CWD", d.get("cwd")), ("SID", d.get("session_id")),
         ("AGENT_ID", d.get("agent_id")), ("AGENT_TYPE", d.get("agent_type")),
         ("TRANSCRIPT", d.get("transcript_path")), ("TOOL", d.get("tool_name")),
         ("FILE_PATH", ti.get("file_path")), ("SUBAGENT", ti.get("subagent_type")),
         ("DESC", ti.get("description")), ("TOOL_USE_ID", d.get("tool_use_id")))
for k, v in pairs:
    print(k + "=" + shlex.quote(v if isinstance(v, str) else ""))
' 2>/dev/null)"
BASE="${MEMORY_MCP_URL:-http://127.0.0.1:${MEMORY_MCP_DAEMON_PORT:-8765}}"
AUTH=()
[ -n "$MEMORY_MCP_TOKEN" ] && AUTH=(-H "Authorization: Bearer ${MEMORY_MCP_TOKEN}")

# The identity every hook forwards, in one array so adding a field is one edit
# rather than five.
IDENT=(
  --data-urlencode "session_id=${SID}"
  --data-urlencode "agent_id=${AGENT_ID}"
  --data-urlencode "agent_type=${AGENT_TYPE}"
  --data-urlencode "transcript_path=${TRANSCRIPT}"
)
MEMORY_MCP_BIN="${MEMORY_MCP_BIN:-$HOME/.claude-memory-mcp/runtime/bin/memory-mcp}"

# Claim this folder for the project its committed .claude-memory/manifest.json
# names, then pull that snapshot into the central store. Runs BEFORE
# auto-register: claiming re-binds a moved or renamed folder to its existing
# project, and auto-register would otherwise register a duplicate first.
# Local only: a remote server (MEMORY_MCP_URL set) has no local DB to sync into.
[ -z "$MEMORY_MCP_URL" ] && [ -x "$MEMORY_MCP_BIN" ] && \
  "$MEMORY_MCP_BIN" sync import --cwd "$CWD" 2>/dev/null

# Register this folder as a project (if it is a git repo and still not known),
# so it appears in the management UI even before it has any rules.
curl -s -G --max-time 3 "${AUTH[@]}" "${IDENT[@]}" "${BASE}/api/hook/auto-register" \
  --data-urlencode "cwd=${CWD}" 2>/dev/null

curl -s -G --max-time 2 "${AUTH[@]}" "${IDENT[@]}" "${BASE}/api/hook/rules" \
  --data-urlencode "cwd=${CWD}" --data-urlencode "mode=intro" 2>/dev/null
exit 0
