"""`memory-mcp sync export|import` - the file-I/O side of project memory sync.

This runs in Claude Code's context (invoked by the SessionStart / Stop hooks),
so - unlike the launchd daemon - it can reach project folders. It does the
folder I/O and talks to the daemon over HTTP for the database work:

  export: GET the snapshot from the daemon, write <project>/.claude-memory/
  import: read <project>/.claude-memory/, POST it to the daemon to reconcile

The snapshot is ``.claude-memory/memory.duckdb`` (see ``db/snapshot.py``) beside
a small ``manifest.json``. It used to be one JSON file per category; a folder
that still holds those is migrated on the first import - written into the
database, VERIFIED by reading the rows back, and only then unlinked. On a
machine that has just cloned the repo and not yet imported, that JSON is the
only copy of the project's rules, so a truncated migration that deleted its
source would be unrecoverable.

The snapshot database is opened, written and closed here. It is a SEPARATE file
from the daemon's ``~/.claude-memory-mcp/projects/<slug>.duckdb``, which the
daemon holds open - DuckDB allows one writer per file across processes, and
nothing in this module ever touches the daemon's copy directly.
"""

import argparse
import json
import subprocess
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from memory_mcp.config import settings
from memory_mcp.constants import (
    GITATTRIBUTES_NAME,
    MANIFEST_NAME,
    MERGE_DRIVER_NAME,
    SNAPSHOT_DB_NAME,
    SNAPSHOT_DIRNAME,
    SNAPSHOT_SCHEMA_VERSION,
    SYNC_CATEGORIES,
)
from memory_mcp.db.snapshot import (
    SnapshotError,
    SnapshotVersionError,
    read_snapshot,
    snapshot_db_path,
    write_snapshot,
)
from memory_mcp.repositories import ProjectRepository

_MANIFEST = MANIFEST_NAME

# Written into .claude-memory/ on every export. Paths in a .gitattributes are
# relative to its own directory, so this scopes the driver to the snapshot and
# touches nothing else in the repo. A clone WITHOUT memory-mcp installed simply
# has no driver by that name and gets an ordinary binary conflict - which is the
# safe failure: visible, and resolved by hand.
_GITATTRIBUTES = (
    "# Written by memory-mcp. The memory snapshot is a DuckDB file, so git\n"
    "# cannot merge it textually; `memory-mcp-setup` registers a merge driver\n"
    "# that reconciles two snapshots with SQL instead of discarding one side.\n"
    f"{SNAPSHOT_DB_NAME} merge={MERGE_DRIVER_NAME}\n"
    f"{SNAPSHOT_DB_NAME} -diff\n"
    f"{SNAPSHOT_DB_NAME} -text\n"
)

# Also written on every export, and committed with it. Two things must never
# reach a commit: the half-built database an export builds beside the real one
# (normally removed, but not if the process is killed), and DuckDB's
# write-ahead log. Both are binary noise, and a committed .wal would be read
# back as part of a database it no longer belongs to.
_SNAPSHOT_GITIGNORE = (
    "# Written by memory-mcp. Transient files of the snapshot database.\n"
    ".*.tmp\n"
    ".*.tmp.wal\n"
    "*.duckdb.wal\n"
)


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


def _read_legacy_json(snap: Path) -> tuple[dict[str, list], list[str]]:
    """Read the pre-DuckDB per-category JSON. Returns (categories, unparseable)."""
    categories: dict[str, list] = {}
    parse_failed: list[str] = []
    for category in SYNC_CATEGORIES:
        path = snap / f"{category}.json"
        if not path.is_file():
            continue
        try:
            items = json.loads(path.read_text())
        except Exception:  # noqa: BLE001 - e.g. unresolved git conflict markers
            parse_failed.append(category)
            continue
        if isinstance(items, list):
            categories[category] = items
        else:
            parse_failed.append(category)
    return categories, parse_failed


def _legacy_json_files(snap: Path) -> list[Path]:
    return [p for p in (snap / f"{c}.json" for c in SYNC_CATEGORIES) if p.is_file()]


def _ids(categories: dict[str, list]) -> set[str]:
    return {
        m["id"]
        for items in categories.values()
        for m in items
        if isinstance(m, dict) and m.get("id")
    }


def _retire_legacy_json(snap: Path, db_path: Path, parse_failed: list[str]) -> bool:
    """Delete the JSON snapshot - but ONLY once the database provably holds it.

    THE RULE THIS ENFORCES: delete only after a verified write. On a freshly
    cloned machine the JSON is the only copy of that project's rules and
    decisions, so the check is not "did write_snapshot return without raising"
    but "read the rows back out and account for every id that was in the JSON".

    An id may be accounted for two ways: it is in the database, or it carries a
    tombstone (it was hard-deleted somewhere, and re-adding it would resurrect a
    memory someone deliberately removed). Anything else and NOTHING is unlinked
    and the caller is told why.

    A category whose JSON did not parse also blocks the delete: we cannot know
    what was in a file we could not read.
    """
    files = _legacy_json_files(snap)
    if not files:
        return True
    if parse_failed:
        print(
            "[Memory MCP] Keeping the JSON snapshot: these files could not be "
            f"parsed (resolve the git conflict first): {', '.join(parse_failed)}"
        )
        return False

    json_categories, _ = _read_legacy_json(snap)
    json_ids = _ids(json_categories)
    try:
        back = read_snapshot(db_path)
    except SnapshotError as e:
        print(f"[Memory MCP] Keeping the JSON snapshot: {e}")
        return False

    accounted = _ids(back["categories"]) | {
        t["memory_id"] for t in back["tombstones"] if t.get("memory_id")
    }
    missing = json_ids - accounted
    if missing:
        print(
            f"[Memory MCP] Keeping the JSON snapshot: {len(missing)} of "
            f"{len(json_ids)} memories are not in {db_path.name} yet "
            f"(e.g. {sorted(missing)[0]}). Nothing was deleted."
        )
        return False

    for path in files:
        path.unlink()
    print(
        f"[Memory MCP] Migrated {len(json_ids)} memories into {db_path.name} and "
        f"removed {len(files)} JSON file(s) after verifying every id read back."
    )
    return True


def _merge_legacy_into(categories: dict[str, list], legacy: dict[str, list],
                       tombstoned: set[str]) -> dict[str, list]:
    """Fold JSON rows the database does not have into what we are about to write.

    The JSON can legitimately hold memories the central store has never seen -
    a clone whose SessionStart import failed, a snapshot pulled but not yet
    applied - and dropping those on the floor is the loss this whole task exists
    to prevent. A row the store already has wins: it is the live copy.

    A tombstoned id is NOT folded back in. Resurrecting a memory somebody hard
    deleted is the mirror image of the same bug.
    """
    known = _ids(categories)
    merged = {c: list(items) for c, items in categories.items()}
    for category, items in legacy.items():
        for mem in items:
            if not isinstance(mem, dict):
                continue
            mid = mem.get("id")
            if not mid or mid in known or mid in tombstoned:
                continue
            mem.setdefault("category", category)
            merged.setdefault(category, []).append(mem)
            known.add(mid)
    return merged


def _write_sidecars(snap: Path) -> None:
    """The two committed files that make the snapshot behave inside git.

    `.gitattributes` points the database at the merge driver - without it the
    driver is registered and never used, and a pull takes one side. `.gitignore`
    keeps the transient write files out of commits. Both are rewritten on every
    export so a project picks them up on its first export after upgrading.
    """
    (snap / GITATTRIBUTES_NAME).write_text(_GITATTRIBUTES)
    (snap / ".gitignore").write_text(_SNAPSHOT_GITIGNORE)


#: The keys a manifest link carries - and so the only ones that can ever leave
#: this machine through it. Out on purpose: the PAT (credentials live in the
#: registry keyed by server URL), the SQLite `id` (machine-local, would mislead a
#: reader elsewhere), default_list_id / default_assignee_id / state_list_map
#: (rebuilt from the live board on attach; stale diff noise in git), socket_url,
#: active, created_at (per installation).
_MANIFEST_LINK_KEYS = (
    "provider", "base_url", "remote_project_id", "remote_work_package_id",
    "label", "is_default", "match_paths",
)

#: A committed file is shared input: long strings are cut rather than trusted.
_MANIFEST_TEXT_LIMIT = 500


def _manifest_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()[:_MANIFEST_TEXT_LIMIT]
    return value or None


def _manifest_links(links: object) -> list[dict]:
    """Project links into what `.claude-memory/manifest.json` carries.

    The ONE projection, used on every side - the daemon's sync-export, the
    manifest write, and the proposals route reading a committed file back - so
    export and the proposal diff cannot disagree about what a binding is.
    Idempotent on its own output. Entries without a work package id (the
    binding's identity) and non-dict junk are dropped; `match_paths` is always a
    list, `[]` for none. Sorted default-first, then by label and id, so every
    machine writes the same order and git diffs show real changes only.
    """
    out: list[dict] = []
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict):
            continue
        package_id = _manifest_text(link.get("remote_work_package_id"))
        if package_id is None:
            continue
        paths = link.get("match_paths")
        out.append({
            "provider": _manifest_text(link.get("provider")) or "asoode",
            "base_url": (_manifest_text(link.get("base_url")) or "").rstrip("/"),
            "remote_project_id": _manifest_text(link.get("remote_project_id")),
            "remote_work_package_id": package_id,
            "label": _manifest_text(link.get("label")),
            "is_default": link.get("is_default") is True,
            "match_paths": [
                p[:_MANIFEST_TEXT_LIMIT] for p in paths if isinstance(p, str) and p.strip()
            ] if isinstance(paths, list) else [],
        })
    out.sort(key=lambda l: (not l["is_default"], (l["label"] or "").lower(),
                            l["remote_work_package_id"]))
    return out


def _read_manifest(snap: Path) -> dict:
    try:
        data = json.loads((snap / _MANIFEST).read_text())
    except Exception:  # noqa: BLE001 - missing, half-written, conflict markers
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(snap: Path, project_id: str | None, slug: str,
                    categories: dict[str, list], links: list | None = None) -> None:
    """The one file in .claude-memory/ that stays JSON, and why.

    `context.detect_project_from_cwd` reads it on every project detection in the
    daemon to answer "which project is this folder?". Making that hot path open
    a DuckDB file - and take its lock - would be a real regression, so identity
    stays in ~300 bytes of text that git can also merge line by line. The
    memories, which are what actually grew, are in the database.

    Version 3 adds `links`: the path->board bindings, so a clone knows which
    work package owns which subtree and the mapping is reviewable in git. They
    are small identity data of the same kind, hence here and not in the
    snapshot database. `links=None` (a daemon too old to send them, or a JSON
    migration) keeps what the file already carries rather than erasing it.
    """
    if links is None:
        links = _read_manifest(snap).get("links")
    (snap / _MANIFEST).write_text(json.dumps({
        "version": 3,
        "project_id": project_id,
        "slug": slug,
        "snapshot": SNAPSHOT_DB_NAME,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "categories": sorted(c for c, items in categories.items() if items),
        "links": _manifest_links(links),
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


def _export(cwd: str) -> None:
    slug = _claim(cwd)
    if not slug:
        return
    project = ProjectRepository().get(slug)
    payload = _daemon(f"/api/projects/{slug}/sync-export")
    categories = payload.get("categories") or {}
    provenance = payload.get("provenance") or []
    tombstones = payload.get("tombstones") or []

    snap = _snapshot_dir(cwd, slug)
    snap.mkdir(parents=True, exist_ok=True)
    db_path = snapshot_db_path(snap)

    legacy, parse_failed = _read_legacy_json(snap)
    tombstoned = {t["memory_id"] for t in tombstones if t.get("memory_id")}
    if legacy:
        categories = _merge_legacy_into(categories, legacy, tombstoned)

    counts = write_snapshot(
        db_path,
        project_id=project.project_uid if project else None,
        slug=slug,
        categories=categories,
        provenance=provenance,
        tombstones=tombstones,
    )
    _write_manifest(snap, project.project_uid if project else None, slug, categories,
                    payload.get("links"))
    _write_sidecars(snap)
    _retire_legacy_json(snap, db_path, parse_failed)

    print(
        f"[Memory MCP] Exported {counts['memories']} memories "
        f"({counts['provenance']} provenance, {counts['tombstones']} tombstones) "
        f"to {db_path}"
    )
    _warn_if_ignored(snap)


def _import(cwd: str) -> None:
    slug = _claim(cwd)
    if not slug:
        return
    snap = _snapshot_dir(cwd, slug)
    db_path = snapshot_db_path(snap)
    has_db = db_path.is_file()
    if not has_db and not (snap / _MANIFEST).is_file():
        return  # no snapshot in this folder - nothing to import

    # Before the memories, and independent of them: a snapshot this build
    # refuses to read must not also hide the bindings notice.
    _propose_links(slug, _read_manifest(snap))

    parse_failed: list[str] = []
    provenance: list[dict] = []
    tombstones: list[dict] = []

    if has_db:
        # The database is authoritative. Legacy JSON beside it is folded in only
        # for ids the database has never heard of, then retired below.
        try:
            snapshot = read_snapshot(db_path)
        except SnapshotVersionError as e:
            # Refuse the WHOLE import. Applying the columns we happen to
            # recognise from a newer writer is how half a rule set arrives.
            print(f"[Memory MCP] {e}")
            return
        except SnapshotError as e:
            print(f"[Memory MCP] Could not read {db_path.name}: {e}")
            return
        categories = snapshot["categories"]
        provenance = snapshot["provenance"]
        tombstones = snapshot["tombstones"]
        legacy, parse_failed = _read_legacy_json(snap)
        if legacy:
            categories = _merge_legacy_into(
                categories, legacy,
                {t["memory_id"] for t in tombstones if t.get("memory_id")},
            )
    else:
        categories, parse_failed = _read_legacy_json(snap)

    reconcile = [c for c in SYNC_CATEGORIES if c not in parse_failed]
    result = _daemon(
        f"/api/projects/{slug}/sync-import", "POST",
        {
            "categories": categories, "reconcile": reconcile,
            "provenance": provenance, "tombstones": tombstones,
        },
    )
    added = result.get("added", 0)
    updated = result.get("updated", 0)
    removed = result.get("tombstoned", 0)
    if added or updated or removed:
        print(
            f"[Memory MCP] Imported project memory from {SNAPSHOT_DIRNAME}/ "
            f"({added} new, {updated} updated, {removed} removed by tombstone)."
        )

    # Only now, with the daemon holding the rows, is the JSON safe to retire -
    # and _retire_legacy_json still verifies the database itself before it
    # unlinks anything.
    if _legacy_json_files(snap):
        _write_snapshot_from_import(snap, db_path, slug, categories,
                                    provenance, tombstones)
        _retire_legacy_json(snap, db_path, parse_failed)

    if parse_failed:
        print(
            "[Memory MCP] Skipped unparseable snapshot files (resolve git "
            f"conflicts): {', '.join(parse_failed)}"
        )


def _propose_links(slug: str, manifest: dict) -> None:
    """Hand the manifest's bindings to the daemon as PROPOSALS, and say so.

    Never a bind: the daemon stores them and diffs them against this machine's
    links; project_links is not touched, because linking is always explicit and
    a hook must not be able to put a private project on someone's server. One
    line is printed when anything is not already applied. A manifest without a
    `links` key (version 2 and older) proposes nothing. A daemon without the
    route (older build) or not running costs nothing but the notice.
    """
    links = manifest.get("links")
    if not isinstance(links, list):
        return
    try:
        result = _daemon(
            f"/api/projects/{slug}/asoode/link-proposals", "POST", {"links": links},
        )
    except (OSError, ValueError):  # URLError/HTTPError are OSErrors; bad JSON
        return
    pending = [
        p for p in (result.get("proposals") or [])
        if isinstance(p, dict) and p.get("status") != "matches"
    ]
    if pending:
        n = len(pending)
        print(
            f"[Memory MCP] {n} path->board {'binding' if n == 1 else 'bindings'} in "
            f"{SNAPSHOT_DIRNAME}/ {'is' if n == 1 else 'are'} not applied. "
            "Review with memory_asoode_links."
        )


def _write_snapshot_from_import(snap: Path, db_path: Path, slug: str,
                                categories: dict, provenance: list,
                                tombstones: list) -> None:
    """Write the DuckDB snapshot during a JSON migration.

    Uses what was READ FROM THE JSON, not a fresh export from the daemon: the
    daemon may hold less (a memory that failed to import) and the point of the
    migration is that nothing in the JSON is lost. The export at the end of the
    session rewrites it from the store in the normal way.
    """
    _, manifest = _find_manifest(str(snap.parent))
    project = ProjectRepository().get(slug)
    project_id = (project.project_uid if project else None) or manifest.get("project_id")
    write_snapshot(
        db_path, project_id=project_id, slug=slug, categories=categories,
        provenance=provenance, tombstones=tombstones,
    )
    _write_manifest(snap, project_id, slug, categories)
    _write_sidecars(snap)


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
