"""The harness contract: one adapter per AI coding harness, one shared core.

A "harness" is a client of this daemon - Claude Code, ZCode, Codex, OpenCode.
Everything the daemon serves (MCP at /mcp, the JSON API, the hook payloads at
/api/hook/*) is harness-neutral already; what differs is the WIRING: where each
harness reads its MCP server list, which hook events it offers and in what
payload shape, where its agent definitions live.

An adapter answers exactly those questions. The orchestrator (setup.py) runs
the shared machine-level steps once, then every adapter for an installed
harness, and reports what each changed.

NON-NEGOTIABLE across adapters:

- A conflicting MCP entry named `memory` pointing somewhere else is REPORTED,
  never silently overwritten or shadowed. One machine, two memory servers is a
  real failure mode that took a week to diagnose once already (a project-scoped
  override pointed a project at a fork; the session saw no tasks and nobody
  knew why).
- A missing capability is reported, never faked: an adapter whose harness has
  no hook surface says so (`NotSupported`), and the core degrades the way it
  always has - tools and `memory_session_start` carry the rules, the per-turn
  line is an enhancement.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

#: The user's home, resolved through a function so tests can monkeypatch it.
def home() -> Path:
    return Path.home()

REPO_DIR = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class HarnessCapabilities:
    """What this harness can carry. Every False means a feature the core must
    not assume is present - the briefs reach the model some other way."""

    #: The harness can run command hooks on session/prompt/tool boundaries.
    hooks: bool = False
    #: The harness loads agent definitions (subagents) from files.
    agents: bool = False
    #: A UserPromptSubmit-equivalent runs on EVERY turn (rules stay visible
    #: across compaction). Without it, rules ride session start only.
    per_turn_line: bool = False
    #: A PreToolUse-equivalent exists whose exit status can refuse a tool call
    #: (the require-task gate). True only where the harness documents it.
    edit_gate: bool = False


@dataclass
class ChangeReport:
    """What one adapter did, and what needs a human decision.

    `actions` are lines the orchestrator prints. `conflicts` are problems the
    adapter found but did NOT resolve - each one names the file and the choice
    the user owns. An adapter that writes nothing reports that; silence reads
    as success and nothing was verified.
    """

    harness: str
    actions: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def ok(self, message: str) -> None:
        self.actions.append(message)

    def warn(self, message: str) -> None:
        self.actions.append("Warning: " + message)

    def conflict(self, message: str) -> None:
        self.conflicts.append(message)

    def print(self) -> None:
        for line in self.actions:
            print(f"    {line}")
        for line in self.conflicts:
            print(f"    CONFLICT: {line}")


class HarnessNotSupported(RuntimeError):
    """This harness cannot carry a capability the contract offers.

    Raised (not returned) so the orchestrator reports it the same way it
    reports any other refusal, and so a caller cannot mistake None for
    "nothing to do".
    """


@runtime_checkable
class Harness(Protocol):
    """One wiring layer onto the shared core.

    Every method must be idempotent: setup runs again on every update, and a
    re-run must leave the machine in the same state, not append a second copy
    of anything.
    """

    name: str

    def detect(self) -> bool:
        """True when this harness is installed on this machine."""
        ...

    def capabilities(self) -> HarnessCapabilities:
        """What this harness can carry, used by the core to degrade honestly."""
        ...

    def configure_mcp(
        self, server_url: str, remote_url: str | None = None, token: str | None = None
    ) -> ChangeReport:
        """Point the harness's MCP client at the daemon (or a remote server)."""
        ...

    def install_hooks(
        self, remote_url: str | None = None, token: str | None = None
    ) -> ChangeReport:
        """Wire the hook scripts the daemon installed into this harness.

        Raises HarnessNotSupported when the harness has no hook surface. The
        scripts themselves are harness-neutral: they read a JSON payload on
        stdin and talk HTTP to the daemon.
        """
        ...

    def install_agents(self) -> ChangeReport:
        """Install the agent team in this harness's definition format.

        Raises HarnessNotSupported when the harness has no subagent system.
        """
        ...


def selected(names: str | None) -> list[str]:
    """Resolve a --harness argument to adapter names, in registry order.

    "auto" (the default) means every DETECTED harness; "all" means every
    REGISTERED one, detected or not, so an adapter can be forced during its
    own verification. Anything else must name registered adapters exactly -
    a typo must fail loudly, not silently install nothing.
    """
    from memory_mcp.harnesses import ADAPTERS

    known = [a.name for a in ADAPTERS]
    if names in (None, "", "auto"):
        return [a.name for a in ADAPTERS if a.detect()]
    if names == "all":
        return known
    chosen = [n.strip() for n in names.split(",") if n.strip()]
    unknown = [n for n in chosen if n not in known]
    if unknown:
        raise ValueError(
            f"unknown harness(es): {', '.join(unknown)}; registered: {', '.join(known)}"
        )
    return [n for n in known if n in chosen]


def get(name: str) -> Harness:
    """The adapter registered under `name`. Raises for an unknown name."""
    from memory_mcp.harnesses import ADAPTERS

    for adapter in ADAPTERS:
        if adapter.name == name:
            return adapter
    raise ValueError(f"no harness adapter named {name!r}")


__all__ = [
    "REPO_DIR",
    "home",
    "Harness",
    "HarnessCapabilities",
    "ChangeReport",
    "HarnessNotSupported",
    "selected",
    "get",
]
