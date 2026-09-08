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
#
# It stays JSON on purpose, even though the memories themselves moved into
# DuckDB. `context.detect_project_from_cwd` reads it on every project detection
# in the daemon; making that hot path open a DuckDB file (and take its lock)
# to answer "which project is this folder?" would be a real regression. It is
# ~300 bytes, does not grow, and conflicts visibly line by line.
MANIFEST_NAME = "manifest.json"

# The memory snapshot itself: one DuckDB file inside SNAPSHOT_DIRNAME, replacing
# the per-category JSON that was rewritten in full every session (kalagh reached
# 1.1M of it). Binary to git, which is why the merge driver below exists.
SNAPSHOT_DB_NAME = "memory.duckdb"

# Bumped when the snapshot's own tables change. A snapshot stamped NEWER than
# the reader supports is refused with a message, never half-imported.
SNAPSHOT_SCHEMA_VERSION = 1

# DuckDB's default 256KB blocks give a ~525KB floor for an empty database, which
# would make the committed snapshot bigger than the JSON it replaces. 16KB blocks
# (the minimum) put a real project's snapshot in the tens of KB. Only applied at
# CREATE; readers take the block size from the file header.
SNAPSHOT_BLOCK_SIZE = 16384

# The git merge driver that reconciles two snapshot databases with SQL.
# `memory-mcp-setup` registers the driver machine-wide under this name, and every
# export writes SNAPSHOT_DIRNAME/.gitattributes pointing the snapshot at it - so
# a clone that has memory-mcp installed merges, and one that does not gets an
# ordinary binary conflict instead of a silent overwrite.
MERGE_DRIVER_NAME = "claude-memory-snapshot"
GITATTRIBUTES_NAME = ".gitattributes"

# Categories carried by the snapshot; `session` is device-local and excluded.
SYNC_CATEGORIES = [c.value for c in MemoryCategory if c.value != "session"]

# asoode's hosted service - the right answer for ~90% of installs, so these are
# defaults rather than required configuration. An on-premise site overrides them
# (MEMORY_MCP_ASOODE_*_URL, or the Integrations screen); nothing reads these
# names directly except `asoode.get_endpoints`, which applies that precedence.
ASOODE_DEFAULT_APP_URL = "https://app.asoode.com"
ASOODE_DEFAULT_API_URL = "https://api.asoode.com"
ASOODE_DEFAULT_SOCKET_URL = "https://socket.asoode.com"
