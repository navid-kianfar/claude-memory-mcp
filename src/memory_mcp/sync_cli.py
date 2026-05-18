"""`memory-mcp sync export|import` - the file-I/O side of project memory sync.

This runs in Claude Code's context (invoked by the SessionStart / Stop hooks),
so - unlike the launchd daemon - it can reach project folders. It does the
folder I/O and talks to the daemon over HTTP for the database work:

  export: GET the snapshot from the daemon, write <project>/.claude-memory/
  import: read <project>/.claude-memory/, POST it to the daemon to reconcile
"""

import argparse
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from memory_mcp.config import settings
from memory_mcp.context import detect_project_from_cwd
from memory_mcp.repositories import ProjectRepository
from memory_mcp.services.sync_service import SNAPSHOT_DIRNAME, SYNC_CATEGORIES

_MANIFEST = "manifest.json"


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


def _export(cwd: str) -> None:
    slug = detect_project_from_cwd(cwd)
    if not slug:
        return
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

    (snap / _MANIFEST).write_text(json.dumps({
        "version": 1, "slug": slug, "categories": present,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    total = sum(len(v) for v in categories.values())
    print(f"[Memory MCP] Exported {total} memories to {snap}")


def _import(cwd: str) -> None:
    slug = detect_project_from_cwd(cwd)
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
        pass
