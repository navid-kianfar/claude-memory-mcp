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
from memory_mcp.harnesses.codex import CodexHarness
from memory_mcp.harnesses.opencode import OpenCodeHarness
from memory_mcp.harnesses.zcode import ZCodeHarness

ADAPTERS: list[Harness] = [
    ClaudeHarness(), ZCodeHarness(), OpenCodeHarness(), CodexHarness(),
]

__all__ = [
    "ADAPTERS",
    "ChangeReport",
    "Harness",
    "HarnessCapabilities",
    "HarnessNotSupported",
    "get",
    "selected",
]
