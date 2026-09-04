#!/bin/bash
# Stop hook: keep the local installation matching the source.
#
# TWO JOBS, in order:
#   1. An APPROVED remote update -> git pull --ff-only, then reinstall.
#   2. Local source changes since the last install -> reinstall.
#
# WHY THIS RUNS HERE AND NOT IN THE DAEMON. Installing reloads the launchd
# daemon, which drops every live MCP connection - so it must happen when no tool
# call is in flight, and the end of a turn is exactly that moment. The daemon
# also cannot reach a repo under a TCC-protected folder like ~/Desktop, so it
# detects and this applies.
#
# Runs detached so it never blocks the end of a turn. Logs to
# ~/.claude-memory-mcp/auto-update.log
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

STATE="$HOME/.claude-memory-mcp"
MARKER="$STATE/.last-auto-install"
LOCK="$STATE/.auto-install.lock"
LOG="$STATE/auto-update.log"
BASE="${MEMORY_MCP_URL:-http://127.0.0.1:${MEMORY_MCP_DAEMON_PORT:-8765}}"
mkdir -p "$STATE" 2>/dev/null

# Ask the daemon two things at once: is an update approved, and where is the
# source repo. The second matters because this script is INSTALLED to
# ~/.claude-memory-mcp/hooks/ - resolving its own path finds no repository, so
# an earlier version of this hook could only ever have been a no-op.
# Answer is "<apply|no> [repo path]"; plain text so bash needs no parser.
ANSWER=$(curl -s --max-time 2 "${BASE}/api/hook/update" 2>/dev/null)
APPROVED="${ANSWER%% *}"
REPO="${ANSWER#* }"
[ "$REPO" = "$ANSWER" ] && REPO=""

# Fall back to our own location, which is right when running from the repo.
if [ -z "$REPO" ] || [ ! -d "$REPO" ]; then
  REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)"
fi
[ -z "$REPO" ] && exit 0

# This runs on every Stop in every project. Get out cheaply when it is not ours.
[ -f "$REPO/pyproject.toml" ] || exit 0
grep -q 'name = "memory-mcp"' "$REPO/pyproject.toml" 2>/dev/null || exit 0

# What changed locally since the last install? (first run => treat as changed)
if [ -f "$MARKER" ]; then
  SRC_CHANGED=$(find "$REPO/src" -type f -newer "$MARKER" 2>/dev/null | head -1)
  FE_CHANGED=$(find "$REPO/frontend/src" -type f -newer "$MARKER" 2>/dev/null | head -1)
else
  SRC_CHANGED="first-run"
  FE_CHANGED="first-run"
fi

# Nothing approved and nothing changed: the common case, and it costs one curl.
[ "$APPROVED" != "apply" ] && [ -z "$SRC_CHANGED" ] && [ -z "$FE_CHANGED" ] && exit 0

# Clear a stale lock left by a crashed run (older than 15 minutes).
[ -d "$LOCK" ] && find "$LOCK" -maxdepth 0 -mmin +15 -exec rmdir {} \; 2>/dev/null
# Single-flight: two installs would race on the runtime directory.
mkdir "$LOCK" 2>/dev/null || exit 0

nohup bash -c "
  echo '=== auto-update '\"\$(date)\"' ==='
  cd '$REPO' || exit 0

  if [ '$APPROVED' = 'apply' ]; then
    # FAST-FORWARD ONLY, and never over local work. This repo is where the user
    # develops; silently discarding or merging their changes to install an
    # update would be unforgivable, so anything unexpected aborts and says why.
    if [ -n \"\$(git status --porcelain 2>/dev/null)\" ]; then
      echo 'SKIPPED: working tree is dirty - commit or stash, then approve again'
    else
      BRANCH=\$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
      echo \"pulling --ff-only on \$BRANCH...\"
      if git pull --ff-only 2>&1; then
        echo 'pulled'
        curl -s -X POST --max-time 2 '${BASE}/api/hook/update-done' >/dev/null 2>&1
        FE_CHANGED=changed
        SRC_CHANGED=changed
      else
        echo 'SKIPPED: not a fast-forward (diverged or no upstream) - resolve by hand'
      fi
    fi
  fi

  if [ -n '$FE_CHANGED' ]; then echo 'rebuilding frontend...'; ( cd frontend && npm run build ); fi
  uv run memory-mcp update
  touch '$MARKER'
  rmdir '$LOCK' 2>/dev/null
  echo '=== done '\"\$(date)\"' ==='
" >> "$LOG" 2>&1 &

exit 0
