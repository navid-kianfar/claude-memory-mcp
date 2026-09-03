"""`memory-mcp sync export|import` - the file-I/O side of project memory sync.

This runs in Claude Code's context (invoked by the SessionStart / Stop hooks),
so - unlike the launchd daemon - it can reach project folders. It does the
folder I/O and talks to the daemon over HTTP for the database work:

  export: GET the snapshot from the daemon, write <project>/.claude-memory/
  import: read <project>/.claude-memory/, POST it to the daemon to reconcile
"""

import argparse
import json
import subprocess
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from memory_mcp.config import settings
from memory_mcp.constants import MANIFEST_NAME, SNAPSHOT_DIRNAME, SYNC_CATEGORIES
from memory_mcp.repositories import ProjectRepository

_MANIFEST = MANIFEST_NAME


def _daemon(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{settings.daemon_port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _snapshot_dir(cwd: str, slug: str) -> Path:
    project = ProjectRepository().get(slug)
    base = project.project_path if (project and project.project_path) else cwd
    return Path(base) / SNAPSHOT_DIRNAME


def _find_manifest(cwd: str) -> tuple[Path | None, dict]:
    """Walk up from cwd for a committed snapshot manifest. Returns (dir, data)."""
    check = Path(cwd).resolve()
    for _ in range(10):
        manifest = check / SNAPSHOT_DIRNAME / MANIFEST_NAME
        if manifest.is_file():
            try:
                return check, json.loads(manifest.read_text())
            except Exception:  # noqa: BLE001 - unresolved conflict markers, etc.
                return check, {}
        if check.parent == check:
            break
        check = check.parent
    return None, {}


def _claim(cwd: str) -> str | None:
    """Ask the daemon which project owns this folder, keyed on the committed uid.

    This runs before anything else so a moved or renamed folder rebinds its
    existing project instead of being registered a second time. Detection by
    path or folder name cannot do that - the identity has to travel with the
    repository, which is what manifest.json's project_id is for.
    """
    root, manifest = _find_manifest(cwd)
    payload = {
        "cwd": str(root or Path(cwd).resolve()),
        "project_id": manifest.get("project_id"),
        "slug": manifest.get("slug"),
    }
    result = _daemon("/api/hook/claim", "POST", payload)
    action = result.get("action")
    slug = result.get("slug")
    if action == "rebound":
        print(
            f"[Memory MCP] Project '{slug}' moved here from "
            f"{result.get('previous_path')} - re-bound, no duplicate created."
        )
    elif action == "created":
        print(f"[Memory MCP] Registered project '{slug}' from its committed memory.")
    return slug


def _warn_if_ignored(snap: Path) -> None:
    """Point out a snapshot git cannot carry - it defeats the whole point."""
    try:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", snap.name],
            cwd=snap.parent, capture_output=True, timeout=5,
        ).returncode == 0
    except Exception:  # noqa: BLE001 - no git, not a repo, whatever: stay quiet
        return
    if ignored:
        print(
            f"[Memory MCP] Warning: {snap.name}/ is gitignored in this repo, so "
            f"this memory will never reach your teammates. Remove it from "
            f".gitignore to share it."
        )


def _export(cwd: str) -> None:
    slug = _claim(cwd)
    if not slug:
        return
    project = ProjectRepository().get(slug)
    categories = _daemon(f"/api/projects/{slug}/sync-export").get("categories", {})
    snap = _snapshot_dir(cwd, slug)
    snap.mkdir(parents=True, exist_ok=True)

    present: list[str] = []
    for category in SYNC_CATEGORIES:
        path = snap / f"{category}.json"
        items = categories.get(category)
        if items:
            path.write_text(json.dumps(items, indent=2, sort_keys=True))
            present.append(category)
        elif path.exists():
            path.unlink()

    # project_id is what makes this snapshot self-identifying: whoever reads it
    # next - after a move, a rename, or a clone onto another machine - matches
    # the project by uid instead of guessing from the folder.
    (snap / _MANIFEST).write_text(json.dumps({
        "version": 1,
        "project_id": project.project_uid if project else None,
        "slug": slug, "categories": present,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    total = sum(len(v) for v in categories.values())
    print(f"[Memory MCP] Exported {total} memories to {snap}")
    _warn_if_ignored(snap)


def _import(cwd: str) -> None:
    slug = _claim(cwd)
    if not slug:
        return
    snap = _snapshot_dir(cwd, slug)
    if not (snap / _MANIFEST).is_file():
        return  # no snapshot in this folder - nothing to import

    categories: dict[str, list] = {}
    parse_failed: list[str] = []
    for category in SYNC_CATEGORIES:
        path = snap / f"{category}.json"
        if not path.is_file():
            continue
        try:
            categories[category] = json.loads(path.read_text())
        except Exception:  # noqa: BLE001 - e.g. unresolved git conflict markers
            parse_failed.append(category)

    reconcile = [c for c in SYNC_CATEGORIES if c not in parse_failed]
    result = _daemon(
        f"/api/projects/{slug}/sync-import", "POST",
        {"categories": categories, "reconcile": reconcile},
    )
    added = result.get("added", 0)
    updated = result.get("updated", 0)
    if added or updated:
        print(
            f"[Memory MCP] Imported project memory from .claude-memory/ "
            f"({added} new, {updated} updated)."
        )
    if parse_failed:
        print(
            "[Memory MCP] Skipped unparseable snapshot files (resolve git "
            f"conflicts): {', '.join(parse_failed)}"
        )


def _log_failure(action: str, cwd: str) -> None:
    """Append a failure to <data_dir>/sync.log.

    The hooks send our stderr to /dev/null so a broken sync cannot pollute a
    Claude turn - which also means a crash here is invisible. A circular import
    once killed every export and import for weeks without a trace. Failures now
    always leave a dated traceback behind.
    """
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with (settings.data_dir / "sync.log").open("a") as fh:
            fh.write(f"\n=== {stamp} sync {action} --cwd {cwd} failed ===\n")
            traceback.print_exc(file=fh)
    except Exception:  # noqa: BLE001 - logging must never be the thing that breaks
        pass


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="memory-mcp sync")
    parser.add_argument("action", choices=["export", "import"])
    parser.add_argument("--cwd", default=".", help="project directory")
    args = parser.parse_args(argv)
    try:
        if args.action == "export":
            _export(args.cwd)
        else:
            _import(args.cwd)
    except Exception:  # noqa: BLE001 - never break the hook or the Claude turn
        _log_failure(args.action, args.cwd)
