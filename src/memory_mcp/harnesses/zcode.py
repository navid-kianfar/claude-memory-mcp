"""The ZCode adapter.

ZCode reads the same kind of wiring Claude does, from different files:

- MCP: `~/.zcode/cli/config.json` under `mcp.servers` (`{"type": "http",
  "url": ...}`, canonical fields; user scope overrides workspace, and every
  scope auto-connects at session start).
- Hooks: the same file's `hooks` key - `{"enabled": true, "events": {...}}`,
  seven events (`SessionStart`, `UserPromptSubmit`, `PreToolUse`,
  `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`, `Stop`).
  **Configuration-file hooks are disabled by default**: without
  `enabled: true` nothing fires. `SubagentStop` does not exist in ZCode, so
  the agent-stop ledger has no source here - the dispatch concurrency gate
  then asks more often than strictly needed, which is the safe direction.
- Agents: `~/.zcode/agents/*.md`, frontmatter compatible with the composed
  Claude definitions (name/description/model/tools/color), so the same
  composition machinery produces both.

The hook scripts are the SAME files Claude installs: they read a JSON payload
on stdin and talk HTTP to the daemon. Where a payload field may differ, the
scripts fall back to the environment variables ZCode documents injecting
(`${CLAUDE_PROJECT_DIR}`, `${CLAUDE_SESSION_ID}`) - and every script fails
open, so an unverified payload shape degrades to silence, never to a block.
"""

import json
import os
import shutil
from pathlib import Path

from memory_mcp.config import settings
from memory_mcp.harnesses.base import ChangeReport, HarnessCapabilities
from memory_mcp.harnesses.claude import (
    AGENTS_DIR,
    HOOK_EVENTS,
    HOOK_MATCHERS,
    HOOKS_DIR,
    _render_agent,
    installable_agent_text,
    is_abstract_agent,
    parse_agent,
)

#: ZCode has no SubagentStop event; the stop ledger simply has no source here.
UNSUPPORTED_EVENTS = frozenset({"SubagentStop"})


def zcode_dir() -> Path:
    return Path.home() / ".zcode"


def zcode_config_path() -> Path:
    return zcode_dir() / "cli" / "config.json"


def zcode_agents_dir() -> Path:
    return zcode_dir() / "agents"


def zcode_agent_text(text: str) -> str:
    """A composed Claude definition, adjusted for ZCode's tool resolution.

    ZCode computes a subagent's tools as `tools ?? []` (empty = every tool),
    then appends `Skill` when `skills:` is set. A definition with `skills:` but
    no `tools:` therefore becomes a Skill-ONLY agent - no Read, Grep or Bash
    (the reviewer could not open a file). The skills we name are Claude Code
    built-ins anyway, so `skills:` is dropped here; Claude keeps it.
    """
    parsed = parse_agent(text)
    if parsed is None or "skills" not in parsed[0]:
        return text
    front, body = parsed
    front.pop("skills")
    return _render_agent(front, body)


class ZCodeHarness:
    name = "zcode"

    def detect(self) -> bool:
        """ZCode is installed when its config root exists (it always exists
        for the desktop app, which creates it on first run)."""
        return zcode_dir().is_dir()

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            hooks=True, agents=True, per_turn_line=True, edit_gate=True,
        )

    # ---------- MCP ----------

    def configure_mcp(
        self, server_url: str, remote_url: str | None = None, token: str | None = None
    ) -> ChangeReport:
        report = ChangeReport(self.name)
        config = self._read_config(report)
        if config is None:
            return report

        if remote_url:
            url = f"{remote_url.rstrip('/')}/mcp"
            entry: dict = {"type": "http", "url": url}
            if token:
                entry["headers"] = {"Authorization": f"Bearer {token}"}
        else:
            url = server_url
            entry = {"type": "http", "url": url}

        servers = config.setdefault("mcp", {}).setdefault("servers", {})
        previous = servers.get("memory")
        if isinstance(previous, dict) and previous.get("url") not in (None, url):
            report.conflict(
                f"mcp.servers.memory pointed at {previous.get('url')!r}; "
                f"repointed to {url!r}"
            )
        servers["memory"] = entry

        zcode_config_path().parent.mkdir(parents=True, exist_ok=True)
        zcode_config_path().write_text(json.dumps(config, indent=2))
        report.ok(f"MCP server 'memory' -> {url}")
        return report

    # ---------- hooks ----------

    def install_hooks(
        self, remote_url: str | None = None, token: str | None = None
    ) -> ChangeReport:
        report = ChangeReport(self.name)
        config = self._read_config(report)
        if config is None:
            return report

        hooks_dest = settings.data_dir / "hooks"
        hooks_dest.mkdir(parents=True, exist_ok=True)
        for script in sorted({
            name for names in HOOK_EVENTS.values() for name in names
        }):
            src = HOOKS_DIR / script
            if src.exists():
                dst = hooks_dest / script
                shutil.copy2(src, dst)
                os.chmod(dst, 0o755)

        hooks = config.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        # Configuration-file hooks are DISABLED by default in ZCode; without
        # this flag the wiring below is decorative. Flipping it on is the
        # point of installing hooks; an existing True stays True.
        hooks["enabled"] = True
        events = hooks.setdefault("events", {})

        # Idempotency: strip entries that run OUR scripts (an older install may
        # carry a stale path) while KEEPING everyone else's groups, then add
        # the current ones after them. Other hooks in the file are untouched.
        ours = {
            hooks_dest / script
            for names in HOOK_EVENTS.values() for script in names
        }
        ours_names = {p.name for p in ours}
        kept_by_event: dict[str, list] = {}
        for event, groups in list(events.items()):
            kept = []
            for group in groups:
                group["hooks"] = [
                    h for h in group.get("hooks", [])
                    if not any(
                        str(h.get("command", "")).endswith(n) for n in ours_names
                    )
                ]
                if group["hooks"]:
                    kept.append(group)
            if kept:
                kept_by_event[event] = kept
            else:
                events.pop(event, None)

        env_prefix = ""
        if remote_url:
            import shlex

            parts = ["env", f"MEMORY_MCP_URL={shlex.quote(remote_url.rstrip('/'))}"]
            if token:
                parts.append(f"MEMORY_MCP_TOKEN={shlex.quote(token)}")
            env_prefix = " ".join(parts) + " "

        installed_events = []
        for event, script_names in HOOK_EVENTS.items():
            if event in UNSUPPORTED_EVENTS:
                continue
            groups = []
            for script in script_names:
                command = f"{env_prefix}{hooks_dest / script}"
                groups.append({
                    "hooks": [{"type": "command", "command": command}],
                    # ZCode's matcher is a case-sensitive REGEX over the tool
                    # name; HOOK_MATCHERS' pipe syntax is already valid regex.
                    **({"matcher": HOOK_MATCHERS[script]}
                       if script in HOOK_MATCHERS else {}),
                })
            # Everyone else's surviving groups come FIRST, ours after - the
            # strip pass ran on the same list, so a re-install cannot
            # duplicate ours and cannot lose theirs.
            events[event] = kept_by_event.get(event, []) + groups
            installed_events.append(event)

        config["hooks"] = hooks
        zcode_config_path().parent.mkdir(parents=True, exist_ok=True)
        zcode_config_path().write_text(json.dumps(config, indent=2))
        report.ok(
            f"Hook scripts installed to {hooks_dest}; events wired: "
            f"{', '.join(installed_events)}"
        )
        report.warn(
            "ZCode has no SubagentStop event: the agent-stop ledger has no "
            "source here, so the dispatch concurrency gate may ask more often "
            "than strictly needed"
        )
        return report

    # ---------- agents ----------

    def install_agents(self) -> ChangeReport:
        report = ChangeReport(self.name)
        if not AGENTS_DIR.is_dir():
            report.warn(f"No agents/ directory at {AGENTS_DIR} - nothing to install.")
            return report

        dest = zcode_agents_dir()
        dest.mkdir(parents=True, exist_ok=True)

        # The same composition machinery, the same source of truth: ZCode
        # reads the composed definitions Claude Code reads, from the same
        # agents/ folder - one team, two runtimes.
        by_stem = {
            p.stem: p for p in AGENTS_DIR.glob("*.md") if p.name.lower() != "readme.md"
        }
        sources = sorted(p for p in by_stem.values() if not is_abstract_agent(p))

        manifest = settings.data_dir / "agents-installed-zcode.json"
        previous: list[str] = []
        if manifest.exists():
            try:
                previous = json.loads(manifest.read_text())
            except Exception:  # noqa: BLE001
                previous = []

        removed = 0
        installed = [p.name for p in sources]
        for name in previous:
            if name not in installed:
                stale = dest / name
                if stale.exists():
                    stale.unlink()
                    removed += 1

        for src in sources:
            (dest / src.name).write_text(zcode_agent_text(installable_agent_text(src, by_stem)))

        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(installed, indent=2))
        summary = f"{len(installed)} agent(s) installed to {dest}"
        if removed:
            summary += f" ({removed} retired)"
        report.ok(summary)
        return report

    # ---------- shared ----------

    def _read_config(self, report: ChangeReport) -> dict | None:
        path = zcode_config_path()
        config: dict = {}
        if path.exists():
            try:
                config = json.loads(path.read_text())
            except Exception:  # noqa: BLE001
                report.warn(f"{path} is not valid JSON - skipping.")
                return None
        if not isinstance(config, dict):
            report.warn(f"{path} is not a JSON object - skipping.")
            return None
        return config
