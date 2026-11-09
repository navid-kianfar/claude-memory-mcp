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

# ZCode may not carry every field on stdin; it injects these as environment
# variables instead (its docs list ${CLAUDE_PROJECT_DIR} / ${CLAUDE_SESSION_ID}
# template variables as "also injected as environment variables"). An empty
# stdin-derived value falls back to them; a real one wins.
CWD="${CWD:-${CLAUDE_PROJECT_DIR:-${ZCODE_PROJECT_DIR:-}}}"
SID="${SID:-${CLAUDE_SESSION_ID:-}}"

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

# A daemon that is down makes every call below fail silently, and silence reads
# exactly like "nothing to report" - a session then works for hours storing
# nothing. Say so once, at the start, where it can still be acted on.
if curl -s -o /dev/null --max-time 3 "${AUTH[@]}" "${BASE}/api/health" 2>/dev/null; then
  # Claim this folder for the project its committed .claude-memory/manifest.json
  # names, then pull that snapshot into the central store. Runs BEFORE
  # auto-register: claiming re-binds a moved or renamed folder to its existing
  # project, and auto-register would otherwise register a duplicate first.
  # Local only: a remote server (MEMORY_MCP_URL set) has no local DB to sync
  # into. Both calls are silenced: nothing but the envelope below may reach
  # stdout - a stray line in it fails ZCode's strict output validation and
  # costs the whole injection.
  [ -z "$MEMORY_MCP_URL" ] && [ -x "$MEMORY_MCP_BIN" ] && \
    "$MEMORY_MCP_BIN" sync import --cwd "$CWD" >/dev/null 2>&1

  # Register this folder as a project (if it is a git repo and still not
  # known), so it appears in the management UI even before it has any rules.
  curl -s -G --max-time 3 "${AUTH[@]}" "${IDENT[@]}" "${BASE}/api/hook/auto-register" \
    --data-urlencode "cwd=${CWD}" >/dev/null 2>&1

  CONTEXT=$(curl -s -G --max-time 2 "${AUTH[@]}" "${IDENT[@]}" "${BASE}/api/hook/rules" \
    --data-urlencode "cwd=${CWD}" --data-urlencode "mode=intro" 2>/dev/null)
else
  CONTEXT="[Memory MCP] The memory daemon is not answering at ${BASE}, so its tools are unavailable and nothing this session learns will be stored. Tell the user before recording rules or decisions; restart it with: launchctl kickstart -k gui/\$(id -u)/com.claude-memory-mcp.daemon"
fi

# ---- the output contract ----
# Claude Code injects a context hook's plain stdout into the model. ZCode does
# NOT: its hook runner parses stdout as JSON against a STRICT schema and
# discards whatever fails validation - so every session-start here was silently
# dropped in ZCode, and nothing loaded unless the user asked. The envelope
# below is the one shape BOTH clients accept (Claude Code injects
# hookSpecificOutput.additionalContext for SessionStart too), and ZCode
# validates it hard: exactly these keys - any extra key fails validation - and
# a hookEventName that does not name the firing event raises. Without python3
# the envelope cannot be built; the raw text then still reaches Claude Code,
# and ZCode carries on exactly as before this fix.
# The update banner is for the PERSON: Claude Code shows a top-level
# `systemMessage` to the user and never to the model, so the notice cannot be
# left unsaid. ZCode rejects any extra top-level key, so it is sent ONLY where
# setup marked the install as Claude Code's (MEMORY_MCP_HARNESS=claude in the
# hook command). Its own short timeout: a slow answer costs the banner only.
BANNER=""
if [ "$MEMORY_MCP_HARNESS" = "claude" ]; then
  BANNER=$(curl -s -G --max-time 2 "${AUTH[@]}" "${IDENT[@]}" \
    "${BASE}/api/hook/update-banner" 2>/dev/null)
fi
if [ -n "$CONTEXT" ] || [ -n "$BANNER" ]; then
  if ENVELOPE=$(printf '%s' "$CONTEXT" | BANNER="$BANNER" python3 -c '
import json, os, sys
context = sys.stdin.read()
out = {}
if context:
    out["hookSpecificOutput"] = {
        "hookEventName": "SessionStart",
        "additionalContext": context,
    }
if os.environ.get("BANNER"):
    out["systemMessage"] = os.environ["BANNER"]
print(json.dumps(out))' 2>/dev/null) && [ -n "$ENVELOPE" ]; then
    printf '%s\n' "$ENVELOPE"
  elif [ -n "$CONTEXT" ]; then
    printf '%s\n' "$CONTEXT"
  fi
fi
exit 0
