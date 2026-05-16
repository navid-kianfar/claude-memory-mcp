"""Auto-setup for memory-mcp.

Sets up the shared HTTP daemon model:
  1. Directories (~/.memory-mcp/)
  2. DuckDB VSS extension
  3. Embedding model download
  4. launchd agent so the daemon runs and restarts automatically
  5. /etc/hosts entry for the claude-memory-mcp hostname (prints a sudo command)
  6. Claude Code MCP config -> points at the HTTP daemon
  7. Claude Code hooks -> rule injection / session lifecycle
"""

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from memory_mcp.config import settings

LAUNCHD_LABEL = "com.claude-memory-mcp.daemon"
REPO_DIR = Path(__file__).resolve().parents[2]
VENV_MEMORY_MCP = REPO_DIR / ".venv" / "bin" / "memory-mcp"
HOOKS_DIR = REPO_DIR / ".claude" / "hooks"

HOOK_EVENTS = {
    "UserPromptSubmit": "inject-rules.sh",
    "SessionStart": "session-start.sh",
    "Stop": "session-end.sh",
}


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


# ---------- 4. launchd ----------

def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def setup_launchd() -> None:
    """Write and (re)load the launchd agent that runs the daemon."""
    if sys.platform != "darwin":
        print("    Not macOS - skipping launchd. Run `memory-mcp serve` manually.")
        return

    program = str(VENV_MEMORY_MCP) if VENV_MEMORY_MCP.exists() else "memory-mcp"
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [program, "serve"],
        "EnvironmentVariables": {"MEMORY_MCP_DATA_DIR": str(settings.data_dir)},
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


# ---------- 5. /etc/hosts ----------

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


# ---------- 6. Claude MCP config ----------

def claude_json_path() -> Path:
    return Path.home() / ".claude.json"


def setup_claude_mcp() -> None:
    """Point Claude Code at the HTTP daemon instead of spawning a stdio server."""
    path = claude_json_path()
    config: dict = {}
    if path.exists():
        try:
            config = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            print("    Warning: ~/.claude.json is not valid JSON - skipping MCP config.")
            return
    config.setdefault("mcpServers", {})
    config["mcpServers"]["memory"] = {
        "type": "http",
        "url": f"http://127.0.0.1:{settings.daemon_port}/mcp",
    }
    path.write_text(json.dumps(config, indent=2))
    print(f"    MCP server 'memory' -> http://127.0.0.1:{settings.daemon_port}/mcp")


# ---------- 7. hooks ----------

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


def setup_hooks() -> None:
    """Install the rule-injection / session hooks into global Claude settings."""
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    settings_obj: dict = {}
    if path.exists():
        try:
            settings_obj = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            print("    Warning: ~/.claude/settings.json invalid - skipping hooks.")
            return

    added = 0
    for event, script in HOOK_EVENTS.items():
        script_path = HOOKS_DIR / script
        if not script_path.exists():
            continue
        os.chmod(script_path, 0o755)
        if _add_hook(settings_obj, event, str(script_path)):
            added += 1

    path.write_text(json.dumps(settings_obj, indent=2))
    print(f"    Hooks installed ({added} added, rest already present)")


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
        ("Installing launchd daemon agent", setup_launchd),
        ("Checking /etc/hosts entry", setup_hosts),
        ("Configuring Claude Code MCP (HTTP daemon)", setup_claude_mcp),
        ("Installing Claude Code hooks", setup_hooks),
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


if __name__ == "__main__":
    main()
