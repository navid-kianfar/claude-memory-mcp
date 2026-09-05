"""Auto-setup for memory-mcp.

Sets up the shared HTTP daemon model:
  1. Directories (~/.memory-mcp/)
  2. DuckDB VSS extension
  3. Embedding model download
  4. Runtime install (a self-contained venv + UI under ~/.memory-mcp/)
  5. launchd agent so the daemon runs and restarts automatically
  6. /etc/hosts entry for the claude-memory-mcp hostname (prints a sudo command)
  7. Claude Code MCP config -> points at the HTTP daemon
  8. Claude Code hooks -> rule injection / session lifecycle
  9. Agent definitions -> ~/.claude/agents/ (the standing agent team)
 10. retire `agent: pm` -> the hook carries the lead brief instead

The runtime is installed under ~/.memory-mcp/ (not in the repo) so the launchd
background agent can run it even when the repo lives in a macOS TCC-protected
folder like ~/Desktop, ~/Documents, or ~/Downloads.
"""

import json
import re
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from memory_mcp.config import settings

LAUNCHD_LABEL = "com.claude-memory-mcp.daemon"
REPO_DIR = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_DIR / ".claude" / "hooks"
# Agent definitions live at the repo root, NOT in .claude/agents/: a folder
# there would scope the team to THIS repo, and the team has to follow the user
# into every project. They install to ~/.claude/agents/ for that reason.
AGENTS_DIR = REPO_DIR / "agents"


def runtime_dir() -> Path:
    """Self-contained venv the launchd daemon runs from."""
    return settings.data_dir / "runtime"


def runtime_ui_dir() -> Path:
    """Built frontend, copied here so the daemon serves it from a stable path."""
    return settings.data_dir / "ui"

# One event may carry several scripts. Stop carries two: the session-end
# bookkeeping, and the auto-update, which needs the end of a turn because
# installing restarts the daemon and drops any in-flight MCP call.
HOOK_EVENTS = {
    "UserPromptSubmit": ["inject-rules.sh"],
    "SessionStart": ["session-start.sh"],
    "Stop": ["session-end.sh", "auto-update-install.sh"],
}

#: Where the source repo lives. The installed hooks run from
#: ~/.claude-memory-mcp/hooks/, so they cannot infer it from their own path -
#: they ask the daemon, which reads this.
REPO_DIR_KEY = "install:repo_dir"


def print_step(step: int, total: int, msg: str) -> None:
    print(f"  [{step}/{total}] {msg}")


# ---------- 1. directories ----------

def setup_directories() -> None:
    settings.ensure_dirs()
    print(f"    Data dir: {settings.data_dir}")


# ---------- 2. VSS ----------

def setup_vss() -> None:
    import duckdb

    conn = duckdb.connect()
    conn.execute("INSTALL vss;")
    conn.execute("LOAD vss;")
    conn.close()


# ---------- 3. model ----------

def setup_embedding_model() -> None:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(settings.embedding_model)
    test = model.encode("test", normalize_embeddings=True)
    assert len(test) == settings.embedding_dim


# ---------- 4. runtime install ----------

def setup_runtime() -> None:
    """Install the daemon into a self-contained venv under the data dir.

    Keeps the runnable code out of the repo so a launchd background agent can
    start it regardless of where the repo lives (e.g. a TCC-protected folder).
    """
    rt = runtime_dir()
    subprocess.run(
        ["uv", "venv", "--allow-existing", str(rt)],
        capture_output=True, text=True, check=True,
    )
    result = subprocess.run(
        ["uv", "pip", "install", "--python", str(rt / "bin" / "python"),
         "--quiet", str(REPO_DIR)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-400:] or "uv pip install failed")
    print(f"    Runtime venv: {rt}")

    dist = REPO_DIR / "frontend" / "dist"
    ui = runtime_ui_dir()
    if dist.is_dir():
        if ui.exists():
            shutil.rmtree(ui)
        shutil.copytree(dist, ui)
        print(f"    UI installed: {ui}")
    else:
        print("    Warning: frontend/dist not found - build it with "
              "'cd frontend && npm run build', then re-run setup.")


# ---------- 5. launchd ----------

def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def setup_launchd() -> None:
    """Write and (re)load the launchd agent that runs the daemon."""
    if sys.platform != "darwin":
        print("    Not macOS - skipping launchd. Run `memory-mcp serve` manually.")
        return

    program = str(runtime_dir() / "bin" / "memory-mcp")
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [program, "serve"],
        "EnvironmentVariables": {
            "MEMORY_MCP_DATA_DIR": str(settings.data_dir),
            "MEMORY_MCP_UI_DIR": str(runtime_ui_dir()),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(settings.data_dir / "daemon.log"),
        "StandardErrorPath": str(settings.data_dir / "daemon.log"),
    }
    path = launchd_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(plist, f)

    subprocess.run(["launchctl", "unload", str(path)],
                   capture_output=True, check=False)
    result = subprocess.run(["launchctl", "load", "-w", str(path)],
                            capture_output=True, text=True, check=False)
    if result.returncode == 0:
        print(f"    launchd agent loaded ({path})")
    else:
        print(f"    Warning: launchctl load failed: {result.stderr.strip()}")


# ---------- 6. /etc/hosts ----------

def setup_hosts() -> None:
    hostname = settings.daemon_hostname
    hosts = Path("/etc/hosts")
    content = hosts.read_text() if hosts.exists() else ""
    if hostname in content:
        print(f"    /etc/hosts already maps {hostname}")
        return
    print(f"    /etc/hosts has no entry for {hostname}.")
    print("    Run this once (needs sudo) so the UI URL resolves:")
    print(f'      echo "127.0.0.1 {hostname}" | sudo tee -a /etc/hosts')


# ---------- 7. Claude MCP config ----------

def claude_json_path() -> Path:
    return Path.home() / ".claude.json"


def setup_claude_mcp(remote_url: str | None = None, token: str | None = None) -> None:
    """Point Claude Code at the HTTP daemon (or a remote server) over HTTP.

    With remote_url set (client mode), the entry points at the remote server and
    carries a Bearer token header; otherwise it points at the local daemon,
    exactly as before.
    """
    path = claude_json_path()
    config: dict = {}
    if path.exists():
        try:
            config = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            print("    Warning: ~/.claude.json is not valid JSON - skipping MCP config.")
            return
    config.setdefault("mcpServers", {})
    if remote_url:
        url = f"{remote_url.rstrip('/')}/mcp"
        entry: dict = {"type": "http", "url": url}
        if token:
            entry["headers"] = {"Authorization": f"Bearer {token}"}
    else:
        url = f"http://127.0.0.1:{settings.daemon_port}/mcp"
        entry = {"type": "http", "url": url}
    config["mcpServers"]["memory"] = entry
    path.write_text(json.dumps(config, indent=2))
    print(f"    MCP server 'memory' -> {url}")


# ---------- 8. hooks ----------

def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _add_hook(settings_obj: dict, event: str, command: str) -> bool:
    hooks = settings_obj.setdefault("hooks", {})
    groups = hooks.setdefault(event, [])
    for group in groups:
        for hook in group.get("hooks", []):
            if hook.get("command") == command:
                return False
    groups.append({"hooks": [{"type": "command", "command": command}]})
    return True


def setup_hooks(remote_url: str | None = None, token: str | None = None) -> None:
    """Install the rule-injection / session hooks into global Claude settings.

    The hook scripts are copied to ~/.memory-mcp/hooks/ so the install does not
    depend on the repo staying in place. In client mode (remote_url set) each
    hook command is prefixed with `env MEMORY_MCP_URL=… MEMORY_MCP_TOKEN=…` so the
    generic scripts talk to the remote server with auth.
    """
    import shlex

    env_prefix = ""
    if remote_url:
        parts = ["env", f"MEMORY_MCP_URL={shlex.quote(remote_url.rstrip('/'))}"]
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
            print("    Warning: ~/.claude/settings.json invalid - skipping hooks.")
            return

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
            command = (
                f"{env_prefix}{shlex.quote(str(dst))}" if env_prefix else str(dst)
            )
            if _add_hook(settings_obj, event, command):
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
        print("    Warning: could not record the repo path for auto-update.")

    path.write_text(json.dumps(settings_obj, indent=2))
    print(f"    Hooks installed to {hooks_dest} ({added} added)")


# ---------- 9. agent definitions ----------

def claude_agents_dir() -> Path:
    return Path.home() / ".claude" / "agents"


def _installed_agents_manifest() -> Path:
    """Which agent files THIS installer wrote, so it only ever removes its own."""
    return settings.data_dir / "agents-installed.json"


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
_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_FRONT_ORDER = (
    "name", "description", "model", "effort", "color", "isolation",
    "tools", "disallowedTools", "skills",
)
_COMPOSE_ONLY_KEYS = ("extends", "abstract")


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
    if EXTENSION_MARKER not in text:
        return text
    parsed = parse_agent(text)
    if parsed is None:
        return text.replace(EXTENSION_MARKER, "")
    front, body = parsed
    return _render_agent(front, body.replace(EXTENSION_MARKER, ""))


def setup_agents() -> None:
    """Install the agent team from agents/ into ~/.claude/agents/.

    agents/ in the repo is the source of truth; ~/.claude/agents/ is a build
    artefact of it. That is the whole point - an agent edited in place under
    ~/.claude/agents/ has no history, no review, and no way to tell whether a
    prompt change helped. Edit agents/<name>.md and re-run setup.

    Installed copies are OVERWRITTEN, exactly as the hook scripts are.

    Retiring an agent removes its installed copy too, but only ever a file this
    installer previously wrote: the manifest records what was installed last
    time, so a hand-written agent sitting in the same directory is never touched.
    """
    if not AGENTS_DIR.is_dir():
        print(f"    No agents/ directory at {AGENTS_DIR} - nothing to install.")
        return

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

    summary = f"    {len(installed)} agent(s) installed to {dest}"
    if removed:
        summary += f" ({removed} retired)"
    print(summary)
    if installed:
        print(f"    Team: {', '.join(p.removesuffix('.md') for p in installed)}")


# ---------- 10. retire the `agent` setting ----------

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


# ---------- lean update ----------

def run_update() -> None:
    """Rebuild the runtime from the current source and reload the daemon.

    Lighter than full setup (skips the model/VSS/hosts/MCP-config/hooks
    steps) - used by the auto-update hook when the repo source changes. Agent
    definitions ARE source, so they refresh here too.
    """
    print("Updating the local installation...")
    setup_runtime()
    setup_agents()
    setup_launchd()
    print("Local installation updated.")


# ---------- main ----------

def main() -> None:
    print()
    print("=" * 60)
    print("  Claude Memory MCP - Setup")
    print("=" * 60)
    print()

    steps = [
        ("Creating directories", setup_directories),
        ("Installing DuckDB VSS extension", setup_vss),
        ("Downloading embedding model (~80MB first run)", setup_embedding_model),
        ("Installing the daemon runtime (this can take a minute)", setup_runtime),
        ("Installing launchd daemon agent", setup_launchd),
        ("Checking /etc/hosts entry", setup_hosts),
        ("Configuring Claude Code MCP (HTTP daemon)", setup_claude_mcp),
        ("Installing Claude Code hooks", setup_hooks),
        ("Installing the agent team", setup_agents),
        ("Retiring the `agent` setting (the hook carries it)", retire_default_agent),
    ]
    total = len(steps)
    for i, (msg, fn) in enumerate(steps, 1):
        print_step(i, total, msg + "...")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"    Warning: {e}")
        print()

    port = settings.daemon_port
    host = settings.daemon_hostname
    print("=" * 60)
    print("  Setup complete.")
    print()
    print(f"  Daemon     : http://127.0.0.1:{port}  (auto-starts via launchd)")
    print(f"  Management UI : http://{host}:{port}/   (after the /etc/hosts step)")
    print(f"  MCP endpoint  : http://127.0.0.1:{port}/mcp")
    print()
    print("  Restart Claude Code to pick up the new MCP + hook configuration.")
    print("=" * 60)
    print()


def main_client(remote_url: str, token: str | None = None) -> None:
    """Configure this machine as a pure CLIENT of a remote memory server.

    No local daemon, launchd, /etc/hosts, DB, model, or runtime - just point
    Claude Code's MCP config + hooks at the remote server (with an auth token).
    """
    print()
    print("=" * 60)
    print("  Claude Memory MCP - Client Setup")
    print("=" * 60)
    print(f"  Server: {remote_url}")
    print()

    steps = [
        ("Creating hooks directory", setup_directories),
        ("Configuring Claude Code MCP (remote server)",
         lambda: setup_claude_mcp(remote_url, token)),
        ("Installing Claude Code hooks (pointed at the server)",
         lambda: setup_hooks(remote_url, token)),
    ]
    total = len(steps)
    for i, (msg, fn) in enumerate(steps, 1):
        print_step(i, total, msg + "...")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"    Warning: {e}")
        print()

    print("=" * 60)
    print("  Client setup complete.")
    print()
    print(f"  MCP endpoint : {remote_url.rstrip('/')}/mcp")
    print("  Restart Claude Code to pick up the new MCP + hook configuration.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
