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

The runtime is installed under ~/.memory-mcp/ (not in the repo) so the launchd
background agent can run it even when the repo lives in a macOS TCC-protected
folder like ~/Desktop, ~/Documents, or ~/Downloads.
"""

import json
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


def runtime_dir() -> Path:
    """Self-contained venv the launchd daemon runs from."""
    return settings.data_dir / "runtime"


def runtime_ui_dir() -> Path:
    """Built frontend, copied here so the daemon serves it from a stable path."""
    return settings.data_dir / "ui"

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


def setup_hooks() -> None:
    """Install the rule-injection / session hooks into global Claude settings.

    The hook scripts are copied to ~/.memory-mcp/hooks/ so the install does not
    depend on the repo staying in place.
    """
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
    scripts = set(HOOK_EVENTS.values())
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
    for event, script in HOOK_EVENTS.items():
        src = HOOKS_DIR / script
        if not src.exists():
            continue
        dst = hooks_dest / script
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        if _add_hook(settings_obj, event, str(dst)):
            added += 1

    path.write_text(json.dumps(settings_obj, indent=2))
    print(f"    Hooks installed to {hooks_dest} ({added} added)")


# ---------- lean update ----------

def run_update() -> None:
    """Rebuild the runtime from the current source and reload the daemon.

    Lighter than full setup (skips the model/VSS/hosts/MCP-config/hooks
    steps) - used by the auto-update hook when the repo source changes.
    """
    print("Updating the local installation...")
    setup_runtime()
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
