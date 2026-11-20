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
pairs = (("CWD", d.get("cwd")), ("SID", d.get("session_id")),
         ("AGENT_ID", d.get("agent_id")), ("AGENT_TYPE", d.get("agent_type")),
         ("TRANSCRIPT", d.get("transcript_path")), ("TOOL", d.get("tool_name")),
         ("FILE_PATH", ti.get("file_path")), ("SUBAGENT", ti.get("subagent_type")),
         ("DESC", ti.get("description")), ("TOOL_USE_ID", d.get("tool_use_id")))
for k, v in pairs:
    print(k + "=" + shlex.quote(v if isinstance(v, str) else ""))
# Every file the call writes. Edit/Write name one in file_path. DeepSeek
# Harness apply_patch sends the patch text as `input`; its targets are the
# Add/Update/Delete/Move header lines, relative to the session cwd. No path
# found sends an empty one, and the gate behaves as it always has.
paths = []
if isinstance(ti.get("file_path"), str) and ti["file_path"]:
    paths.append(ti["file_path"])
patch = ti.get("input")
cwd = d.get("cwd") if isinstance(d.get("cwd"), str) else ""
if d.get("tool_name") == "apply_patch" and isinstance(patch, str):
    heads = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")
    for line in patch.splitlines():
        for head in heads:
            target = line[len(head):].strip() if line.startswith(head) else ""
            if target:
                paths.append(target if os.path.isabs(target) or not cwd else os.path.join(cwd, target))
args = []
for path in paths or [""]:
    args += ["--data-urlencode", "file_path=" + path]
# Which of them do not exist yet. Only this script can tell - it runs where the
# file is - and the markdown nudge fires on a NEW progress/plan/notes file.
for path in paths:
    probe = path if os.path.isabs(path) or not cwd else os.path.join(cwd, path)
    if not os.path.exists(probe):
        args += ["--data-urlencode", "new_file=" + path]
print("FILE_ARGS=(" + " ".join(shlex.quote(a) for a in args) + ")")
' 2>/dev/null)"
[ -z "$CWD" ] && exit 0


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
  --data-urlencode "harness=${MEMORY_MCP_HARNESS:-}"
)

# Short timeout: this runs before every edit, so it must never be felt. A
# timeout is an allow, which is why 2 seconds is safe to insist on.
# tool_name and file_path ride along: the gate is the only hook that runs
# before every Edit/Write, so it is also the edit ledger and the only place
# that can tell an edit inside the project from one in /tmp.
ANSWER=$(curl -s -G --max-time 2 "${AUTH[@]}" "${IDENT[@]}" "${BASE}/api/hook/gate" \
  --data-urlencode "cwd=${CWD}" \
  --data-urlencode "tool_name=${TOOL}" \
  "${FILE_ARGS[@]}" 2>/dev/null)
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

# An allow may carry a NUDGE: the write parks working state in a .md file or
# CLAUDE.md, and the daemon points at memory_stage instead. Context only - it
# never blocks. The envelope is the one every harness here accepts (Claude
# Code: PreToolUse additionalContext since CLI 2.1.9; ZCode validates exactly
# these keys and drops anything else; the dsh bridge speaks Claude's format),
# so nothing but these two keys may be added. A deny already carries the nudge
# inside its reason, so this runs on allow only.
if [ -z "$REASON" ]; then
  printf '%s' "$ANSWER" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
nudge = d.get("nudge") if isinstance(d, dict) else None
if isinstance(nudge, str) and nudge.strip():
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": nudge.strip(),
    }}))
' 2>/dev/null
  exit 0
fi

# Exit 2 is the blocking status: the tool call does not run and stderr is fed
# back to the model, which is why the reason has to name the tool to call next.
printf '%s\n' "$REASON" >&2
exit 2
