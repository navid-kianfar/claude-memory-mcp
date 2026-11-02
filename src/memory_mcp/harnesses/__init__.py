"""Harness adapters: one wiring layer per AI coding harness, one shared core.

Registry order is the install order the orchestrator uses. `claude` first:
it is the original and every other adapter's report is read next to what
Claude already carries.
"""

from memory_mcp.harnesses.base import (
    ChangeReport,
    Harness,
    HarnessCapabilities,
    HarnessNotSupported,
    get,
    selected,
)
from memory_mcp.harnesses.claude import ClaudeHarness

ADAPTERS: list[Harness] = [ClaudeHarness()]

__all__ = [
    "ADAPTERS",
    "ChangeReport",
    "Harness",
    "HarnessCapabilities",
    "HarnessNotSupported",
    "get",
    "selected",
]
