#!/bin/bash
# UserPromptSubmit hook: inject the current project's binding rules into context
# on every turn, so rules survive context compaction and never get forgotten.
#
# Talks to the running memory-mcp daemon. Prints nothing when the working
# directory is not a registered memory project, so it is safe to install
# globally without adding noise to unrelated repositories.
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
# The attachments POST body, built here so no field is ever escaped by hand.
body = {k: (d.get(k) if isinstance(d.get(k), str) else "")
        for k in ("session_id", "cwd", "transcript_path", "agent_id", "agent_type")}
body["event"] = "UserPromptSubmit"
print("ATTACH_BODY=" + shlex.quote(json.dumps(body)))
' 2>/dev/null)"
# Default to the local daemon; MEMORY_MCP_URL points at a remote server and
# MEMORY_MCP_TOKEN (if set) authenticates to it. With neither set this is the
# original localhost request, byte-for-byte.
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
curl -s -G --max-time 2 "${AUTH[@]}" "${IDENT[@]}" "${BASE}/api/hook/rules" \
  --data-urlencode "cwd=${CWD}" --data-urlencode "mode=rules" 2>/dev/null

# Files the user attached in the compose box. They reach the model as inline
# base64 inside the transcript and nowhere else, so the DAEMON reads the
# transcript, parks what the user attached, and answers with the notices this
# session has not seen yet (nothing binds to a task until the session asks).
# A second call, after the rules and on its own timeout, so a slow scan can
# never cost the rules injection. Skipped without a transcript to read.
if [ -n "$TRANSCRIPT" ] && [ -n "$ATTACH_BODY" ]; then
  NOTICES=$(printf '%s' "$ATTACH_BODY" | curl -s --max-time 3 -X POST "${AUTH[@]}" \
    -H 'Content-Type: application/json' --data @- \
    "${BASE}/api/hook/attachments" 2>/dev/null)
  # On its own line: the rules body does not end in a newline.
  [ -n "$NOTICES" ] && printf '\n%s\n' "$NOTICES"
fi
exit 0
