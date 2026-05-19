#!/bin/bash
# Stop hook (project-scoped to claude-memory-mcp).
#
# When this repo's source has changed since the last install, rebuild and
# reinstall the local daemon in the background, so the running installation
# always reflects the latest code. No-op when nothing changed; runs detached
# so it never blocks the end of a turn. Logs to ~/.claude-memory-mcp/auto-update.log
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)"
[ -z "$REPO" ] && exit 0
STATE="$HOME/.claude-memory-mcp"
MARKER="$STATE/.last-auto-install"
LOCK="$STATE/.auto-install.lock"
LOG="$STATE/auto-update.log"
mkdir -p "$STATE" 2>/dev/null

# What changed since the last install? (first run => treat as changed)
if [ -f "$MARKER" ]; then
  SRC_CHANGED=$(find "$REPO/src" -type f -newer "$MARKER" 2>/dev/null | head -1)
  FE_CHANGED=$(find "$REPO/frontend/src" -type f -newer "$MARKER" 2>/dev/null | head -1)
else
  SRC_CHANGED="first-run"
  FE_CHANGED="first-run"
fi
[ -z "$SRC_CHANGED" ] && [ -z "$FE_CHANGED" ] && exit 0

# Clear a stale lock left by a crashed run (older than 15 minutes).
[ -d "$LOCK" ] && find "$LOCK" -maxdepth 0 -mmin +15 -exec rmdir {} \; 2>/dev/null
# Single-flight: bail out if an update is already running.
mkdir "$LOCK" 2>/dev/null || exit 0

nohup bash -c "
  echo '=== auto-update '\"\$(date)\"' ==='
  cd '$REPO' || exit 0
  if [ -n '$FE_CHANGED' ]; then echo 'rebuilding frontend...'; ( cd frontend && npm run build ); fi
  uv run memory-mcp update
  touch '$MARKER'
  rmdir '$LOCK' 2>/dev/null
  echo '=== done '\"\$(date)\"' ==='
" >> "$LOG" 2>&1 &
disown 2>/dev/null

exit 0
