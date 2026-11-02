"""Auto-setup for memory-mcp.

Sets up the shared HTTP daemon model:
  1. Directories (~/.claude-memory-mcp/)
  2. DuckDB VSS extension
  3. Embedding model download
  4. Runtime install (a self-contained venv + UI under the data dir)
  5. launchd agent so the daemon runs and restarts automatically
  6. /etc/hosts entry for the daemon hostname (prints a sudo command)
  7. Per-harness wiring, via the adapters in memory_mcp.harnesses:
     MCP config, hooks, agent definitions, client-quirk retirement
  8. git merge driver -> reconciles two .agent-memory snapshot databases

Steps 1-6 and 8 are machine-level and harness-neutral. Step 7 is what used to
be this module's Claude-specific half; it now lives in harnesses/claude.py and
is re-exported below, so every test and caller that imported these names from
`memory_mcp.setup` keeps working unchanged.

The runtime is installed under the data dir (not in the repo) so the launchd
background agent can run it even when the repo lives in a macOS TCC-protected
folder like ~/Desktop, ~/Documents, or ~/Downloads.
"""

import argparse
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from memory_mcp.config import settings
from memory_mcp.constants import LEGACY_MERGE_DRIVER_NAME, MERGE_DRIVER_NAME
from memory_mcp.harnesses.base import REPO_DIR

# Re-exports: the Claude wiring moved to harnesses/claude.py, but these names
# have been importable from memory_mcp.setup since the first release, and the
# suite (test_hook_install, test_agent_install, test_agent_team) pins them
# here. A move must not be a rename for every caller at once.
from memory_mcp.harnesses.claude import (  # noqa: F401
    AGENTS_DIR,
    AGENT_NAME_MARKER,
    AgentCompositionError,
    DEFAULT_SESSION_AGENT,
    EXTENSION_MARKER,
    HOOK_EVENTS,
    HOOK_MATCHERS,
    HOOKS_DIR,
    REPO_DIR_KEY,
    _add_hook,
    _installed_agents_manifest,
    _render_agent,
    _union_tool_list,
    _unquote,
    _yaml_scalar,
    claude_agents_dir,
    claude_json_path,
    claude_settings_path,
    compose_agent,
    installable_agent_text,
    is_abstract_agent,
    parse_agent,
    retire_default_agent,
    setup_agents,
    setup_claude_mcp,
    setup_hooks,
)
from memory_mcp.harnesses.claude import ClaudeHarness  # noqa: F401

LAUNCHD_LABEL = "com.claude-memory-mcp.daemon"


def runtime_dir() -> Path:
    """Self-contained venv the launchd daemon runs from."""
    return settings.data_dir / "runtime"


def runtime_ui_dir() -> Path:
    """Built frontend, copied here so the daemon serves it from a stable path."""
    return settings.data_dir / "ui"


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
    # The onnx backend never imports torch; whichever backend is configured,
    # this step proves a vector comes out at the configured dimension.
    from memory_mcp.embeddings import embed_text

    test = embed_text("test")
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


# ---------- 7. per-harness wiring ----------

def daemon_mcp_url() -> str:
    return f"http://127.0.0.1:{settings.daemon_port}/mcp"


def run_harness_steps(remote_url: str | None = None, token: str | None = None) -> None:
    """Run MCP/hooks/agents wiring for every selected harness, with reports.

    Each adapter is idempotent; a conflict it found is printed and LEFT for the
    user - an installer that silently rewrites another server's entry is the
    two-servers failure mode wearing an installer's uniform.
    """
    import memory_mcp.harnesses as registry

    names = registry.selected(None)
    if not names:
        print("    No supported harness detected - nothing wired. Install one of:"
              f" {', '.join(a.name for a in registry.ADAPTERS)}")
        return
    for name in names:
        adapter = registry.get(name)
        print(f"    -- {name} --")
        for step_name, method in (
            ("MCP config", lambda a=adapter: a.configure_mcp(daemon_mcp_url(), remote_url, token)),
            ("hooks", lambda a=adapter: a.install_hooks(remote_url, token)),
            ("agents", lambda a=adapter: a.install_agents()),
        ):
            try:
                method().print()
            except Exception as e:  # noqa: BLE001 - one harness must not stop the others
                print(f"    Warning: {step_name}: {e}")


# ---------- 8. git merge driver for the memory snapshot ----------

def merge_driver_command() -> str:
    """The command git runs to merge two snapshot databases.

    The runtime venv's binary, not whatever `memory-mcp` happens to be on PATH:
    git runs the driver from inside a merge, with the environment of whatever
    started it (a GUI client, an IDE, a hook), and PATH there is not the shell's.
    The hooks resolve the same binary the same way for the same reason.
    """
    binary = runtime_dir() / "bin" / "memory-mcp"
    return f'"{binary}" merge-snapshot %O %A %B %P'


def setup_merge_driver() -> None:
    """Register the snapshot merge driver in the user's global git config.

    Two halves make this work, and only this one is machine-wide:

      1. HERE: `merge.<name>.driver` in ~/.gitconfig, so git knows what to run.
      2. PER PROJECT: `.agent-memory/.gitattributes`, written by every
         `memory-mcp sync export`, which points the snapshot at that driver.
         It is committed, so a teammate gets it from the repo - but it only
         does anything on a machine that has run this step.

    A clone WITHOUT the driver registered does not break: git finds no driver by
    that name and records an ordinary binary conflict, which is visible and
    fixable by hand. That is the safe failure, and it is why the driver is
    registered rather than the snapshot being marked `merge=ours` or `binary`.
    """
    driver = f"merge.{MERGE_DRIVER_NAME}"
    for key, value in (
        (f"{driver}.name", "memory-mcp snapshot merge (SQL, union + tombstones)"),
        (f"{driver}.driver", merge_driver_command()),
        # How git merges the BASE of a recursive merge. 'binary' means "do not
        # try to text-merge it, hand the driver the two sides" - the driver is
        # the only thing here that understands the format.
        (f"{driver}.recursive", "binary"),
    ):
        result = subprocess.run(
            ["git", "config", "--global", key, value],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git config --global {key} failed: "
                f"{result.stderr.strip() or result.returncode}"
            )
    _retire_legacy_merge_driver()
    print(f"    Merge driver: {MERGE_DRIVER_NAME} -> {merge_driver_command()}")


def _retire_legacy_merge_driver() -> None:
    """Remove the pre-rename `merge.claude-memory-snapshot.*` keys.

    The driver is OURS - registered by this function machine-wide, consumed by
    nothing but the snapshots this app writes - so the rename (2026-11-02) is a
    global config fix, not a compatibility promise. Every install and every
    update runs this, which is why the rename breaks nothing: a machine updates
    before it ever merges across the rename, and a clone that somehow merges
    first fails VISIBLY (git cannot find the driver named in .gitattributes)
    rather than merging wrongly. Removing a section that does not exist is not
    an error; that is what makes this safe to run forever.
    """
    result = subprocess.run(
        ["git", "config", "--global", "--remove-section",
         f"merge.{LEGACY_MERGE_DRIVER_NAME}"],
        capture_output=True, text=True, check=False,
    )
    # git exits 1 for "no such section" - the ordinary case after the first run.
    if result.returncode == 0:
        print(
            f"    Retired legacy merge driver {LEGACY_MERGE_DRIVER_NAME} "
            f"from the global git config."
        )


# ---------- lean update ----------

#: The paths the installer builds from. The auto-update hook's dirty-tree
#: guard watches the same set; a snapshot export (.agent-memory) or an edited
#: doc must never reload the daemon.
_INSTALL_PATHS = ("src", "agents", "frontend/src")


def install_is_current() -> bool:
    """True when the installed runtime already matches this source tree.

    Compares the recorded install:commit to HEAD, plus a dirty check over the
    built paths. A daemon reload drops nothing any more (the MCP transport is
    stateless since v0.18.0) but it is still a restart - and a rebuild that
    would produce the same bytes is a restart for nothing, fired on every Stop
    hook turn whose only change was a snapshot export.
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_DIR),
            capture_output=True, text=True, timeout=10,
        )
        if head.returncode != 0:
            return False
        from memory_mcp.db.registry import get_setting

        if get_setting("install:commit") != head.stdout.strip():
            return False
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", *_INSTALL_PATHS],
            cwd=str(REPO_DIR), capture_output=True, text=True, timeout=10,
        )
        return dirty.returncode == 0 and not dirty.stdout.strip()
    except Exception:  # noqa: BLE001 - unsure means rebuild, the safe direction
        return False


def run_update() -> None:
    """Rebuild the runtime from the current source and reload the daemon.

    Lighter than full setup (skips the model/VSS/hosts/MCP-config steps) - used
    by the auto-update hook when the repo source changes. Skipped entirely when
    the installed runtime already matches the source: rebuilding identical
    bytes and reloading launchd bought nothing and cost every client a daemon
    restart (harmless to MCP sessions since v0.18.0, still pointless). The
    merge driver is re-registered either way - one idempotent `git config`,
    and the legacy-name retirement rides on it.
    """
    if install_is_current():
        print("Install already matches the source - no rebuild, no restart.")
        try:
            setup_merge_driver()
        except Exception as e:  # noqa: BLE001 - no git, or a locked config
            print(f"    Warning: merge driver not registered: {e}")
        return
    print("Updating the local installation...")
    setup_runtime()
    setup_launchd()
    try:
        setup_merge_driver()
    except Exception as e:  # noqa: BLE001 - no git, or a locked config
        print(f"    Warning: merge driver not registered: {e}")
    run_harness_steps()
    print("Local installation updated.")


# ---------- main ----------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="memory-mcp-setup", description="Install the memory-mcp daemon and wire every detected harness."
    )
    parser.add_argument(
        "--harness", default="auto",
        help="which harness(es) to wire: auto (every detected), all, or a "
             "comma-separated list of names (claude, zcode, codex, opencode)",
    )
    args = parser.parse_args()

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
    ]
    total = len(steps) + 2
    for i, (msg, fn) in enumerate(steps, 1):
        print_step(i, total, msg + "...")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"    Warning: {e}")
        print()

    print_step(len(steps) + 1, total, "Wiring the harnesses (MCP, hooks, agents)...")
    try:
        run_harness_steps()
    except Exception as e:  # noqa: BLE001 - unknown --harness name fails loudly
        parser.error(str(e))
    print()

    print_step(total, total, "Registering the memory snapshot git merge driver")
    try:
        setup_merge_driver()
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
    print("  Restart each harness (Claude Code, ZCode, ...) to pick up the")
    print("  new MCP + hook configuration.")
    print("=" * 60)
    print()


def main_client(remote_url: str, token: str | None = None) -> None:
    """Configure this machine as a pure CLIENT of a remote memory server.

    No local daemon, launchd, /etc/hosts, DB, model, or runtime - just point
    every harness's MCP config + hooks at the remote server (with an auth token).
    """
    print()
    print("=" * 60)
    print("  Claude Memory MCP - Client Setup")
    print("=" * 60)
    print(f"  Server: {remote_url}")
    print()

    steps = [
        ("Creating hooks directory", setup_directories),
    ]
    total = len(steps) + 1
    for i, (msg, fn) in enumerate(steps, 1):
        print_step(i, total, msg + "...")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"    Warning: {e}")
        print()

    print_step(total, total, "Wiring the harnesses (MCP + hooks, pointed at the server)...")
    run_harness_steps(remote_url, token)
    print()

    print("=" * 60)
    print("  Client setup complete.")
    print()
    print(f"  MCP endpoint : {remote_url.rstrip('/')}/mcp")
    print("  Restart each harness to pick up the new MCP + hook configuration.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
