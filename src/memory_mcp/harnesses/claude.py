"""The Claude Code adapter: the wiring setup.py has always done, moved here.

The code is MOVED, not rewritten - setup.py's Claude-specific half lived here
in spirit since the first release, and the whole suite (test_hook_install,
test_agent_install, test_agent_team) pins its behaviour. setup.py re-exports
these names, so `setup_mod.setup_hooks` keeps working everywhere.

One behaviour ADDED, deliberately: `configure_mcp` scans ~/.claude.json for
project-scoped `memory` entries that would shadow the top-level one, and for a
top-level entry pointing somewhere other than this daemon. Both are reported
as conflicts - the one-machine-two-servers failure mode cost a week once
(the SMG fork) - but nothing is refused: a re-run of setup that repoints the
top-level entry is exactly what reinstalling means.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from memory_mcp.config import settings
from memory_mcp.harnesses.base import (
    REPO_DIR,
    ChangeReport,
    HarnessCapabilities,
)

#: Where the source repo's hook scripts live (`.claude/hooks/` is Claude's own
#: config folder in the repo; the scripts are installed FROM there TO the
#: machine-wide data dir).
HOOKS_DIR = REPO_DIR / ".claude" / "hooks"
# Agent definitions live at the repo root, NOT in .claude/agents/: a folder
# there would scope the team to THIS repo, and the team has to follow the user
# into every project. They install to ~/.claude/agents/ for that reason.
AGENTS_DIR = REPO_DIR / "agents"

# One event may carry several scripts. Stop carries two: the session-end
# bookkeeping, and the auto-update, which needs the end of a turn because
# installing restarts the daemon and drops any in-flight MCP call.
HOOK_EVENTS = {
    "UserPromptSubmit": ["inject-rules.sh"],
    "SessionStart": ["session-start.sh"],
    "Stop": ["session-end.sh", "auto-update-install.sh"],
    # The only hooks here that can REFUSE anything. The others print into the
    # model's context and hope; a PreToolUse exit status decides whether the tool
    # runs, which is what makes "put the work on the board first" an invariant
    # instead of a rule that held about 70% of the time. Two scripts, because
    # they watch different tools: one the edits, one the dispatches.
    "PreToolUse": ["require-task.sh", "record-dispatch.sh"],
    # Counts agents OUT, as record-dispatch.sh counts them in: the difference is
    # how many are running, which the dispatch hook needs to ask before a third
    # concurrent agent. Its own script because a matcher is per script, and
    # `Agent|Task` on SubagentStop would be matched against the agent's TYPE.
    "SubagentStop": ["record-subagent-stop.sh"],
}

#: Which tools each script fires for. Keyed by SCRIPT, not by event: the two
#: PreToolUse scripts must not see each other's tools - require-task.sh on an
#: Agent call would gate delegation, and record-dispatch.sh on an Edit would log
#: every file as a dispatch. `_add_hook` already makes one group per command, so
#: the installed settings.json simply gets two PreToolUse groups.
#:
#: A PreToolUse hook with no matcher at all would run on every Read and Grep: a
#: needless round trip before each one, and a gate on exploration, which must
#: always stay free.
#:
#: `Agent|Task`: the harness names the delegation tool `Agent` and parts of the
#: documentation call it `Task`. Matching both costs nothing and means the ledger
#: does not depend on which name this CLI build uses.
HOOK_MATCHERS = {
    "require-task.sh": "Edit|Write|NotebookEdit",
    "record-dispatch.sh": "Agent|Task",
}

#: Where the source repo lives. The installed hooks run from
#: ~/.claude-memory-mcp/hooks/, so they cannot infer it from their own path -
#: they ask the daemon, which reads this.
REPO_DIR_KEY = "install:repo_dir"


# ---------- MCP config ----------

def claude_json_path() -> Path:
    return Path.home() / ".claude.json"


class ClaudeHarness:
    name = "claude"

    def detect(self) -> bool:
        """Claude Code is installed when its config or config dir exists."""
        return claude_json_path().exists() or (Path.home() / ".claude").is_dir()

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            hooks=True, agents=True, per_turn_line=True, edit_gate=True
        )

    def configure_mcp(
        self, server_url: str, remote_url: str | None = None, token: str | None = None
    ) -> ChangeReport:
        report = ChangeReport(self.name)
        path = claude_json_path()
        config: dict = {}
        if path.exists():
            try:
                config = json.loads(path.read_text())
            except Exception:  # noqa: BLE001
                report.warn(
                    f"{path} is not valid JSON - skipping MCP config."
                )
                return report

        if remote_url:
            url = f"{remote_url.rstrip('/')}/mcp"
            entry: dict = {"type": "http", "url": url}
            if token:
                entry["headers"] = {"Authorization": f"Bearer {token}"}
        else:
            url = server_url
            entry = {"type": "http", "url": url}

        config.setdefault("mcpServers", {})
        previous = config["mcpServers"].get("memory")
        if isinstance(previous, dict) and previous.get("url") not in (None, url):
            # Ours to repoint - but never silently. A top-level entry aimed at
            # another server is either a stale install or a deliberate choice;
            # the report says which way it went either way.
            report.conflict(
                f"top-level mcpServers.memory pointed at "
                f"{previous.get('url')!r}; repointed to {url!r}"
            )
        config["mcpServers"]["memory"] = entry

        # Project-scoped overrides shadow the top-level entry silently - the
        # exact shape of the two-servers incident. They are the user's to
        # remove; this adapter never edits them.
        for project_path, project in (config.get("projects") or {}).items():
            if not isinstance(project, dict):
                continue
            scoped = (project.get("mcpServers") or {}).get("memory")
            if isinstance(scoped, dict) and scoped.get("url") and scoped.get("url") != url:
                report.conflict(
                    f"project {project_path!r} has its own mcpServers.memory at "
                    f"{scoped.get('url')!r}, which shadows this one for sessions "
                    f"opened there"
                )

        path.write_text(json.dumps(config, indent=2))
        report.ok(f"MCP server 'memory' -> {url}")
        return report

    # ---------- hooks ----------

    def install_hooks(
        self, remote_url: str | None = None, token: str | None = None
    ) -> ChangeReport:
        report = ChangeReport(self.name)
        # MEMORY_MCP_HARNESS=claude tells the shared hook scripts they may add
        # a top-level `systemMessage` (the person-facing update banner): Claude
        # Code accepts it, ZCode's strict schema rejects any extra key.
        parts = ["env", "MEMORY_MCP_HARNESS=claude"]
        if remote_url:
            parts.append(f"MEMORY_MCP_URL={shlex.quote(remote_url.rstrip('/'))}")
            if token:
                parts.append(f"MEMORY_MCP_TOKEN={shlex.quote(token)}")
        env_prefix = " ".join(parts) + " "
        path = claude_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        settings_obj: dict = {}
        if path.exists():
            try:
                settings_obj = json.loads(path.read_text())
            except Exception:  # noqa: BLE001
                report.warn(f"{path} invalid - skipping hooks.")
                return report

        hooks_dest = settings.data_dir / "hooks"
        hooks_dest.mkdir(parents=True, exist_ok=True)

        # Drop any prior memory-mcp hook entries (e.g. stale repo-path ones from an
        # earlier install) so re-running setup stays idempotent.
        scripts = {name for names in HOOK_EVENTS.values() for name in names}
        for event, groups in list(settings_obj.get("hooks", {}).items()):
            kept_groups = []
            for group in groups:
                group["hooks"] = [
                    h for h in group.get("hooks", [])
                    if not any(h.get("command", "").endswith(s) for s in scripts)
                ]
                if group["hooks"]:
                    kept_groups.append(group)
            settings_obj["hooks"][event] = kept_groups

        added = 0
        for event, script_names in HOOK_EVENTS.items():
            for script in script_names:
                src = HOOKS_DIR / script
                if not src.exists():
                    continue
                dst = hooks_dest / script
                shutil.copy2(src, dst)
                os.chmod(dst, 0o755)
                command = f"{env_prefix}{shlex.quote(str(dst))}"
                if _add_hook(
                    settings_obj, event, command, HOOK_MATCHERS.get(script),
                ):
                    added += 1

        # Record the source repo so the installed hooks can find it. They live in
        # ~/.claude-memory-mcp/hooks/ and would otherwise resolve their own path and
        # find no repository at all.
        try:
            from memory_mcp.db.registry import set_setting

            set_setting(REPO_DIR_KEY, str(REPO_DIR))
            # The commit this install was built from. The daemon compares it against
            # GitHub to detect an update without touching the repo, which it cannot
            # read when the repo lives in a TCC-protected folder.
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(REPO_DIR),
                capture_output=True, text=True, timeout=10,
            )
            if head.returncode == 0 and head.stdout.strip():
                set_setting("install:commit", head.stdout.strip())
        except Exception:  # noqa: BLE001 - hooks still work, updates just stay manual
            report.warn("could not record the repo path for auto-update.")

        path.write_text(json.dumps(settings_obj, indent=2))
        report.ok(f"Hooks installed to {hooks_dest} ({added} added)")
        # The `agent: pm` retirement is part of the same file's hygiene.
        _retire_default_agent_reported(report)
        return report

    # ---------- agents ----------

    def install_agents(self) -> ChangeReport:
        report = ChangeReport(self.name)
        if not AGENTS_DIR.is_dir():
            report.warn(f"No agents/ directory at {AGENTS_DIR} - nothing to install.")
            return report

        dest = claude_agents_dir()
        dest.mkdir(parents=True, exist_ok=True)

        # README.md documents the folder for maintainers; it is not an agent. A base
        # (`abstract: true`, or `_name.md`) is composed into others, never installed.
        by_stem = {
            p.stem: p for p in AGENTS_DIR.glob("*.md") if p.name.lower() != "readme.md"
        }
        sources = sorted(p for p in by_stem.values() if not is_abstract_agent(p))
        installed = [p.name for p in sources]

        manifest = _installed_agents_manifest()
        previous: list[str] = []
        if manifest.exists():
            try:
                previous = json.loads(manifest.read_text())
            except Exception:  # noqa: BLE001
                previous = []

        removed = 0
        for name in previous:
            if name not in installed:
                stale = dest / name
                if stale.exists():
                    stale.unlink()
                    removed += 1

        for src in sources:
            # Composed, not copied: `extends:` is resolved here so Claude Code only
            # ever sees a complete definition. A file that extends nothing is
            # written byte for byte.
            (dest / src.name).write_text(installable_agent_text(src, by_stem))

        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(installed, indent=2))

        summary = f"{len(installed)} agent(s) installed to {dest}"
        if removed:
            summary += f" ({removed} retired)"
        report.ok(summary)
        if installed:
            report.ok(
                "Team: " + ", ".join(p.removesuffix(".md") for p in installed)
            )
        return report


def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def claude_agents_dir() -> Path:
    return Path.home() / ".claude" / "agents"


def _add_hook(
    settings_obj: dict, event: str, command: str, matcher: str | None = None,
) -> bool:
    hooks = settings_obj.setdefault("hooks", {})
    groups = hooks.setdefault(event, [])
    for group in groups:
        for hook in group.get("hooks", []):
            if hook.get("command") == command:
                # Already installed - but an older install may predate the
                # matcher, and PreToolUse without one runs on every tool call.
                if matcher and group.get("matcher") != matcher:
                    group["matcher"] = matcher
                    return True
                return False
    group: dict = {"hooks": [{"type": "command", "command": command}]}
    if matcher:
        group["matcher"] = matcher
    groups.append(group)
    return True


def _installed_agents_manifest() -> Path:
    """Which agent files THIS installer wrote, so it only ever removes its own."""
    return settings.data_dir / "agents-installed.json"


# ---------- retire the `agent` setting ----------

#: What setup used to write into ~/.claude/settings.json to make every session
#: start as the pm agent.
DEFAULT_SESSION_AGENT = "pm"


def retire_default_agent() -> None:
    """Remove `agent: pm` from ~/.claude/settings.json if WE put it there.

    WHY IT WENT AWAY, so it is not re-added: the session is meant to BE the lead,
    and the orchestration brief now rides the UserPromptSubmit hook instead
    (enforcement.agent_team_intro / agent_team_line). Two reasons the hook won:

    - The `agent` setting is silently ignored by some clients. It was set on this
      machine and the desktop app started an ordinary session anyway, so it
      promised an orchestrator and delivered none.
    - With both, a CLI session would get pm's prompt as its system prompt AND the
      hook's brief on every turn - the same instructions twice, and pm's tool and
      effort settings forced onto every unrelated one-off session.

    Only ever removes the exact value this installer wrote. An `agent` set to
    anything else is someone's deliberate choice and is left alone.
    """
    path = claude_settings_path()
    if not path.exists():
        return
    try:
        settings_obj = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        print("    Warning: ~/.claude/settings.json invalid - leaving it alone.")
        return

    current = settings_obj.get("agent")
    if current is None:
        print("    No session agent set - the hook carries the lead brief.")
        return
    if current != DEFAULT_SESSION_AGENT:
        print(f"    Session agent is '{current}' (set by hand) - leaving it alone.")
        return

    del settings_obj["agent"]
    path.write_text(json.dumps(settings_obj, indent=2))
    print("    Removed `agent: pm` - the lead brief now rides the hook instead.")


def _retire_default_agent_reported(report: ChangeReport) -> None:
    """`retire_default_agent`, folded into the hook install's report.

    Same deletion, same guards; the lines go into the ChangeReport instead of
    straight to stdout so the orchestrator prints one coherent account.
    """
    path = claude_settings_path()
    if not path.exists():
        return
    try:
        settings_obj = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        report.warn("~/.claude/settings.json invalid - leaving it alone.")
        return

    current = settings_obj.get("agent")
    if current is None:
        return
    if current != DEFAULT_SESSION_AGENT:
        report.ok(f"Session agent is '{current}' (set by hand) - leaving it alone.")
        return

    del settings_obj["agent"]
    path.write_text(json.dumps(settings_obj, indent=2))
    report.ok("Removed `agent: pm` - the lead brief now rides the hook instead.")


# ---------- agent composition: `extends:` ----------
#
# Claude Code has no inheritance between agent definitions, and the team needs
# it: the task lifecycle, the memory contract and the token rules are the same
# for every agent, and an expert (dotnet, nodejs, react, app) is "its base plus a
# layer", not a second copy of the base prompt that drifts. So the installer
# composes. A file may say `extends: <base>` in its frontmatter; the INSTALLED
# file is the base's composed body with this file's body at the base's
# {{EXTENSION}} marker (appended when there is none), under this file's
# frontmatter merged over the base's. `extends` and `abstract` never reach
# ~/.claude/agents/. A file with `abstract: true` (or a name starting with `_`)
# is a base only and is not installed.

EXTENSION_MARKER = "{{EXTENSION}}"
#: Replaced at install with the INSTALLED agent's own `name`. A base prompt is
#: shared by every agent that extends it, so it cannot spell out which agent it
#: is; the leaf's name only exists once composition is finished. It matters
#: because `memory_session_start(agent=...)` is what keeps a subagent from
#: closing the lead's session, and an agent guessing its own type string - or
#: leaving it out - does exactly that, silently.
AGENT_NAME_MARKER = "{{AGENT_NAME}}"
_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_FRONT_ORDER = (
    "name", "description", "model", "effort", "color", "isolation",
    "tools", "disallowedTools", "skills",
)
_COMPOSE_ONLY_KEYS = ("extends", "abstract")

#: The one frontmatter key a child ADDS to rather than replaces. Every other key
#: is "nearest layer wins", which is right for a model or an effort level and
#: wrong for a denial: `_base.md` denies `Agent` to every agent, and
#: `reviewer.md` denies `Edit, Write, NotebookEdit` on top - replacing would
#: silently hand the reviewer the Agent tool back. That is not hypothetical: on
#: 2026-09-13 one reviewer spawned six "angle" reviewers and one of those spawned
#: four more, because subagents could dispatch.
_UNION_KEY = "disallowedTools"


def _union_tool_list(*values: str | None) -> str:
    """`Edit, Write` + `Agent` -> `Edit, Write, Agent`: order kept, no duplicates."""
    seen: list[str] = []
    for value in values:
        for item in (value or "").split(","):
            item = item.strip()
            if item and item not in seen:
                seen.append(item)
    return ", ".join(seen)


class AgentCompositionError(RuntimeError):
    """An agent file extends something that cannot be resolved."""


def _unquote(value: str) -> str:
    """The value YAML would hand back, without its quotes.

    A description containing a colon-space MUST be quoted in the file or a strict
    YAML parser reads the second colon as a nested mapping. Those quotes are
    syntax, not text: every consumer wants the string inside them.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _yaml_scalar(value: str) -> str:
    """`value` as a frontmatter scalar, quoted only when YAML needs it.

    The counterpart to `_unquote`: parsing drops the quotes, so rendering has to
    put them back, or a composed file would ship the invalid YAML the quotes were
    added to fix. A flow collection (`skills: [a, b]`) is left alone.
    """
    if not value or value[0] in "[{":
        return value
    if ": " in value or value.endswith(":"):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def parse_agent(text: str) -> tuple[dict, str] | None:
    """(frontmatter, body) for an agent-shaped file, else None.

    Values are unquoted - the string YAML would give you - so a caller never has
    to know whether the file needed quoting. `skills: [a, b]` is passed through
    untouched, exactly as Claude Code will read it.
    """
    match = _FRONT_RE.match(text)
    if not match:
        return None
    front: dict = {}
    for line in match.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        front[key.strip()] = _unquote(value.strip())
    return front, match.group(2)


def _render_agent(front: dict, body: str) -> str:
    ordered = [k for k in _FRONT_ORDER if k in front]
    ordered += [k for k in front if k not in ordered]
    lines = ["---", *(f"{k}: {_yaml_scalar(front[k])}" for k in ordered), "---", ""]
    return "\n".join(lines) + body.strip("\n") + "\n"


def is_abstract_agent(path: Path) -> bool:
    """A base other definitions extend, never installed on its own."""
    if path.stem.startswith("_"):
        return True
    parsed = parse_agent(path.read_text())
    return bool(parsed and (parsed[0].get("abstract") or "").lower() in ("true", "yes", "1"))


def compose_agent(path: Path, sources: dict[str, Path], _chain: tuple = ()) -> str:
    """The composed text of `path`, with any {{EXTENSION}} marker still in place
    so a further child can extend it. `installable_agent_text` is what goes to
    disk.

    A file that extends nothing and carries no composition keys is returned
    byte for byte - the common case, and what keeps a hand-written definition
    exactly what its author wrote. Deterministic: the same sources always
    compose to the same text, so re-running setup is a no-op.
    """
    text = path.read_text()
    parsed = parse_agent(text)
    if parsed is None:
        return text
    front, body = parsed
    base_name = (front.get("extends") or "").strip()
    if not base_name:
        if not any(k in front for k in _COMPOSE_ONLY_KEYS):
            return text
        clean = {k: v for k, v in front.items() if k not in _COMPOSE_ONLY_KEYS}
        return _render_agent(clean, body)

    chain = _chain + (path.stem,)
    if base_name in chain:
        raise AgentCompositionError(
            f"{path.name} extends {base_name!r}, which is already in the chain "
            f"{' -> '.join(chain)}: a definition cannot extend itself"
        )
    base_path = sources.get(base_name)
    if base_path is None:
        raise AgentCompositionError(
            f"{path.name} extends {base_name!r}, but there is no {base_name}.md "
            f"beside it in {path.parent}"
        )
    base_parsed = parse_agent(compose_agent(base_path, sources, chain))
    if base_parsed is None:
        raise AgentCompositionError(
            f"{path.name} extends {base_path.name}, which has no frontmatter block"
        )
    base_front, base_body = base_parsed
    merged = {**base_front, **{k: v for k, v in front.items() if k not in _COMPOSE_ONLY_KEYS}}
    merged = {k: v for k, v in merged.items() if k not in _COMPOSE_ONLY_KEYS}
    denied = _union_tool_list(base_front.get(_UNION_KEY), front.get(_UNION_KEY))
    if denied:
        merged[_UNION_KEY] = denied
    layer = body.strip("\n")
    if EXTENSION_MARKER in base_body:
        composed = base_body.replace(EXTENSION_MARKER, layer, 1)
    else:
        composed = base_body.rstrip("\n") + "\n\n" + layer + "\n"
    return _render_agent(merged, composed)


def installable_agent_text(path: Path, sources: dict[str, Path]) -> str:
    """What is written to ~/.claude/agents/: composed, and with no marker left
    for Claude Code to read as prompt text."""
    text = compose_agent(path, sources)
    if EXTENSION_MARKER not in text and AGENT_NAME_MARKER not in text:
        return text
    parsed = parse_agent(text)
    if parsed is None:
        return text.replace(EXTENSION_MARKER, "").replace(AGENT_NAME_MARKER, path.stem)
    front, body = parsed
    name = front.get("name") or path.stem
    body = body.replace(EXTENSION_MARKER, "").replace(AGENT_NAME_MARKER, name)
    return _render_agent(front, body)


def setup_agents() -> None:
    """Backward-compatible entry: install the agent team, printing the report.

    The orchestrator calls `ClaudeHarness().install_agents()`; this wrapper
    keeps `memory-mcp-setup`'s old function surface (and every test that pokes
    it) working unchanged.
    """
    ClaudeHarness().install_agents().print()


def setup_hooks(remote_url: str | None = None, token: str | None = None) -> None:
    """Backward-compatible entry: install hooks, printing the report.

    Same shape as the pre-adapter function (remote_url/token for client mode);
    the work is `ClaudeHarness().install_hooks`.
    """
    ClaudeHarness().install_hooks(remote_url, token).print()


def setup_claude_mcp(remote_url: str | None = None, token: str | None = None) -> None:
    """Backward-compatible entry: point Claude Code at the daemon (or remote).

    With no remote_url the local daemon URL is derived from settings, exactly
    as the pre-adapter function did.
    """
    url = (
        f"{remote_url.rstrip('/')}/mcp" if remote_url
        else f"http://127.0.0.1:{settings.daemon_port}/mcp"
    )
    ClaudeHarness().configure_mcp(url, remote_url, token).print()
