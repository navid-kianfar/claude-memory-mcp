"""Every `memory-mcp <command>` module must import on its own.

`memory-mcp sync` was dead for weeks: `sync_cli` imported `memory_mcp.context`
first, which imported a *service* for one constant, which pulled in
`services/__init__` -> `memory_service` -> `memory_mcp.context` (still
half-initialized) -> ImportError. The hooks discard stderr, so export and
import silently did nothing.

Each module is imported in a fresh interpreter, first in the process, so the
import order the real CLI uses is the order under test. An in-process
`import memory_mcp.sync_cli` would not catch it - by then a previous test has
already finished importing `memory_mcp.services`.
"""

import subprocess
import sys

import pytest

# The modules `cli.main` dispatches to, one per subcommand.
ENTRYPOINT_MODULES = [
    "memory_mcp.cli",
    "memory_mcp.context",
    "memory_mcp.daemon",
    "memory_mcp.rules_cli",
    "memory_mcp.server",
    "memory_mcp.setup",
    "memory_mcp.sync_cli",
    "memory_mcp.users_cli",
]


@pytest.mark.parametrize("module", ENTRYPOINT_MODULES)
def test_module_imports_standalone(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"`import {module}` fails as the first import in a fresh interpreter:\n"
        f"{result.stderr}"
    )


def test_sync_cli_runs():
    """`memory-mcp sync` must at least parse args - i.e. get past import."""
    result = subprocess.run(
        [sys.executable, "-m", "memory_mcp.cli", "sync", "--help"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "export" in result.stdout
