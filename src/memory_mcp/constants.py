"""Leaf constants shared across layers.

These live outside `services` on purpose. `context` and the `sync` CLI need the
filenames below, and importing them from a service pulls in the whole
`memory_mcp.services` package - which imports `memory_service`, which imports
`context`. That cycle crashed `memory-mcp sync` on import. Keeping the names in
a dependency-free module makes the cycle impossible to reintroduce.
"""

from memory_mcp.models import MemoryCategory

# Portable per-project DuckDB file, committed alongside a project's source.
PORTABLE_DB_NAME = ".memory-mcp.duckdb"

# Git-committable snapshot directory written by `memory-mcp sync export`.
SNAPSHOT_DIRNAME = ".claude-memory"

# Snapshot manifest. Carries `project_id`, the project's stable identity: it is
# committed with the repo, so the project survives a move, a rename, and a
# teammate's clone.
MANIFEST_NAME = "manifest.json"

# Categories carried by the snapshot; `session` is device-local and excluded.
SYNC_CATEGORIES = [c.value for c in MemoryCategory if c.value != "session"]

# asoode's hosted service - the right answer for ~90% of installs, so these are
# defaults rather than required configuration. An on-premise site overrides them
# (MEMORY_MCP_ASOODE_*_URL, or the Integrations screen); nothing reads these
# names directly except `asoode.get_endpoints`, which applies that precedence.
ASOODE_DEFAULT_APP_URL = "https://app.asoode.com"
ASOODE_DEFAULT_API_URL = "https://api.asoode.com"
ASOODE_DEFAULT_SOCKET_URL = "https://socket.asoode.com"
