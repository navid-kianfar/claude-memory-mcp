"""Load a project straight from a local folder.

Picks the project name from package.json (its "name" field) or the folder
name, then seeds the project: if the folder already holds a portable
.memory-mcp.duckdb it is attached as-is; otherwise a fresh project is created
and, if a CLAUDE.md is present, imported into memory.
"""

import json
from pathlib import Path

from memory_mcp.container import container
from memory_mcp.context import set_active_project
from memory_mcp.utils.text import slugify


def _detect_name(folder: Path) -> tuple[str, str]:
    """Return (slug, display_name) from package.json's name, else the folder name."""
    pkg = folder / "package.json"
    if pkg.is_file():
        try:
            name = json.loads(pkg.read_text()).get("name")
            if name and isinstance(name, str):
                return slugify(name), name
        except Exception:  # noqa: BLE001 - a malformed package.json is non-fatal
            pass
    return slugify(folder.name), folder.name


def load_project_from_folder(path: str) -> dict:
    """Register/attach a project from `path` and seed it. Auto-activates it."""
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"Not a directory: {path}")

    slug, display_name = _detect_name(folder)

    attach = container.portable_service.attach(str(folder), slug, display_name)
    if attach.get("error"):
        raise ValueError(attach["error"])

    project = attach["project"]
    project_slug = project["slug"]
    result: dict = {
        "status": "ok",
        "project": project,
        "folder": str(folder),
        "claude_md_imported": 0,
    }

    if attach.get("action") == "attached_existing_db":
        # An existing .memory-mcp.duckdb is authoritative - use it as-is.
        result["source"] = "existing_memory_db"
    else:
        claude_md = folder / "CLAUDE.md"
        if claude_md.is_file():
            imported = container.claude_md_service.import_file(project_slug, str(folder))
            result["claude_md_imported"] = imported.get("imported", 0)
            result["source"] = "claude_md"
        else:
            result["source"] = "new_empty"

    set_active_project(project_slug)
    result["active"] = True
    return result
