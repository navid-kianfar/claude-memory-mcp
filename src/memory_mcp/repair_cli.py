"""`memory-mcp repair` - find and rebuild tables whose key index has gone stale.

  memory-mcp repair                 -> every registered project
  memory-mcp repair <slug> [...]    -> just these
  memory-mcp repair --check         -> report only, rebuild nothing

A stale PRIMARY KEY index entry aborts the whole process on the next UPDATE of
that row, so the daemon cannot notice its own; see memory_mcp.db.repair. The
work runs in the daemon when one is listening (it holds the files, and DuckDB
allows one writer per file), and in this process when none is.

Exit code: 0 when every project is healthy (or was repaired), 1 otherwise.
"""

import argparse
import json

from memory_mcp import daemon_client


def _one(slug: str, check_only: bool) -> dict:
    def local() -> dict:
        from memory_mcp.db.repair import repair_project

        return repair_project(slug, check_only=check_only)

    try:
        result, _ = daemon_client.run(
            f"/api/projects/{slug}/repair", "POST", {"check_only": check_only}, local,
        )
    except daemon_client.DaemonError as exc:
        return {"project": slug, "status": "error", "error": str(exc)}
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="memory-mcp repair")
    parser.add_argument("slugs", nargs="*", help="projects to check (default: all)")
    parser.add_argument("--check", action="store_true", help="report only")
    parser.add_argument("--json", action="store_true", help="print raw results")
    args = parser.parse_args(argv)

    slugs = args.slugs
    if not slugs:
        from memory_mcp.repositories import ProjectRepository

        slugs = [p.slug for p in ProjectRepository().list_all()]

    healthy = True
    for slug in slugs:
        result = _one(slug, args.check)
        status = result.get("status")
        if args.json:
            print(json.dumps(result))
        else:
            line = f"{slug}: {status}"
            stale = [f["table"] for f in result.get("stale") or []]
            if stale:
                line += f" (stale: {', '.join(stale)})"
            if result.get("error"):
                line += f" - {result['error']}"
            print(line, flush=True)
        if status not in ("ok", "repaired", "missing"):
            healthy = False
    return 0 if healthy else 1
