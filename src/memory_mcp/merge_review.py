"""Pending reviews of memory snapshot merges - the merging session decides.

When git merges two branches that both changed `.agent-memory/memory.duckdb`,
the `merge-snapshot` driver still writes the lossless union (git needs a
result). It does NOT get to decide what main's memory becomes: the user's
decision of 2026-11-20 is that the session that merges reviews the memory diff.
So the driver saves a review record here, and `sync import` holds back exactly
the row versions that record names until the session applies or declines it
with `memory-mcp sync review ...`.

WHERE IT LIVES, AND WHY. `<data_dir>/merge-reviews/<id>.json`: one small file
per merge, written atomically. Not in the repository (it is this machine's
pending decision, not something to commit) and not in the project database
(the merge driver runs inside `git merge`, and nothing outside the daemon may
open a database the daemon holds). Applying a review is a write to the store,
so it goes to the daemon over HTTP like every other CLI write.

PINNED BY VERSION. A held change is identified by (memory id, updated_at), a
held deletion by (memory id, deleted_at). A later edit of the same memory is a
different version and flows normally; only the version the session has not
decided on, or declined, is held.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from memory_mcp.config import settings

REVIEW_DIRNAME = "merge-reviews"

STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_DECLINED = "declined"

#: A resolved review is kept this long, so its declined versions stay held
#: until the main checkout's next exports have rewritten the snapshot.
RESOLVED_RETENTION_DAYS = 90

#: How many rows `render` prints per section before summarising the rest.
_RENDER_LIMIT = 50
#: Content longer than this is cut in the rendered diff; the record keeps it all.
_CONTENT_PREVIEW = 400

# The id is also the file name, so it is validated before it touches a path.
_ID_PATTERN = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{8}$")


class ReviewError(RuntimeError):
    """A review that is missing, unreadable, or not this project's."""


def reviews_dir() -> Path:
    return settings.data_dir / REVIEW_DIRNAME


def new_record(
    review: dict,
    *,
    pathname: str | None,
    ours_label: str | None,
    theirs_label: str | None,
) -> dict:
    """Wrap a `merge_snapshots` review in the record that is stored."""
    stamp = datetime.now()
    return {
        "id": f"{stamp.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "status": STATUS_PENDING,
        "created_at": stamp.isoformat(timespec="seconds"),
        "resolved_at": None,
        "project_id": review.get("project_id"),
        "slug": review.get("slug"),
        "pathname": pathname,
        "ours": ours_label,
        "theirs": theirs_label,
        "added": review.get("added") or [],
        "changed": review.get("changed") or [],
        "removed": review.get("removed") or [],
        "kept_ours": review.get("kept_ours") or [],
        "skipped": [],
    }


def save(record: dict) -> Path:
    """Write a record atomically, readable by this user only."""
    folder = reviews_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{_checked_id(record.get('id'))}.json"
    tmp = folder / f".{target.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp"
    try:
        with tmp.open("w") as handle:
            os.chmod(tmp, 0o600)
            json.dump(record, handle, indent=2, sort_keys=True)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def load(review_id: str) -> dict:
    path = reviews_dir() / f"{_checked_id(review_id)}.json"
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ReviewError(f"No memory merge review with id {review_id}.") from None
    except (OSError, ValueError) as e:
        raise ReviewError(f"Review {review_id} could not be read: {e}") from e
    if not isinstance(data, dict):
        raise ReviewError(f"Review {review_id} is not a review record.")
    return data


def belongs_to(record: dict, project_id: str | None, slug: str) -> bool:
    """Is this record about this project? Identity first, slug only without one.

    Applying another project's rows here is the cross-project merge the rules
    forbid outright, so an unidentifiable record matches nothing.
    """
    recorded = record.get("project_id")
    if recorded and project_id:
        return recorded == project_id
    if recorded or project_id:
        return False
    return bool(slug) and record.get("slug") == slug


def for_project(project_id: str | None, slug: str) -> tuple[dict, ...]:
    """Every readable record for this project, oldest first."""
    folder = reviews_dir()
    if not folder.is_dir():
        return ()
    records: list[dict] = []
    for path in sorted(folder.glob("*.json")):
        if not _ID_PATTERN.match(path.stem):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and belongs_to(data, project_id, slug):
            records.append(data)
    return tuple(records)


def pending(project_id: str | None, slug: str) -> tuple[dict, ...]:
    return tuple(
        r for r in for_project(project_id, slug) if r.get("status") == STATUS_PENDING
    )


def resolve(record: dict, status: str, skipped: tuple[str, ...] = ()) -> dict:
    """Close a review as applied (with any skipped ids) or declined."""
    if status not in (STATUS_APPLIED, STATUS_DECLINED):
        raise ValueError(f"unknown review status {status!r}")
    if record.get("status") != STATUS_PENDING:
        raise ReviewError(
            f"Review {record.get('id')} is already {record.get('status')}."
        )
    resolved = dict(record)
    resolved["status"] = status
    resolved["resolved_at"] = datetime.now().isoformat(timespec="seconds")
    resolved["skipped"] = sorted(set(skipped))
    save(resolved)
    return resolved


def prune() -> int:
    """Delete resolved records past their retention. Pending ones are kept."""
    cutoff = datetime.now() - timedelta(days=RESOLVED_RETENTION_DAYS)
    removed = 0
    folder = reviews_dir()
    if not folder.is_dir():
        return 0
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("status") == STATUS_PENDING:
            continue
        resolved_at = _parse(data.get("resolved_at"))
        if resolved_at is not None and resolved_at < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


# ---------- what an import must hold back ----------


def version_key(value: object) -> str | None:
    """A timestamp in one canonical text form, so two spellings compare equal."""
    parsed = _parse(value)
    return parsed.isoformat() if parsed else None


def _changes(record: dict) -> tuple[list[dict], list[dict]]:
    """The rows and tombstones this record would change on our side."""
    rows = list(record.get("added") or [])
    rows += [pair["theirs"] for pair in record.get("changed") or [] if pair.get("theirs")]
    tombstones = [entry["tombstone"] for entry in record.get("removed") or []
                  if entry.get("tombstone")]
    return rows, tombstones


def held_versions(project_id: str | None, slug: str) -> tuple[frozenset, frozenset]:
    """(row versions, tombstone versions) an import must not apply.

    Everything a pending review would change; everything a declined review
    would have changed; and, of an applied review, the ids the session
    skipped.
    """
    rows: set[tuple[str, str | None]] = set()
    tombs: set[tuple[str, str | None]] = set()
    for record in for_project(project_id, slug):
        status = record.get("status")
        skipped = set(record.get("skipped") or [])
        changed_rows, changed_tombs = _changes(record)
        for row in changed_rows:
            mid = row.get("id")
            if mid and (status != STATUS_APPLIED or mid in skipped):
                rows.add((mid, version_key(row.get("updated_at"))))
        for tomb in changed_tombs:
            mid = tomb.get("memory_id")
            if mid and (status != STATUS_APPLIED or mid in skipped):
                tombs.add((mid, version_key(tomb.get("deleted_at"))))
    return frozenset(rows), frozenset(tombs)


def apply_payload(record: dict, skip: frozenset[str]) -> tuple[dict, list[dict]]:
    """(categories, tombstones) to post to sync-import, minus the skipped ids."""
    rows, tombstones = _changes(record)
    categories: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("id") and row["id"] not in skip:
            categories.setdefault(row.get("category") or "", []).append(row)
    kept_tombstones = [t for t in tombstones if t.get("memory_id") not in skip]
    return categories, kept_tombstones


# ---------- what the session reads ----------


def counts(record: dict) -> dict[str, int]:
    return {key: len(record.get(key) or []) for key in ("added", "changed", "removed", "kept_ours")}


def describe(record: dict, command: str) -> str:
    """One paragraph: what is waiting, and the three commands that settle it."""
    tally = counts(record)
    between = ""
    if record.get("theirs") or record.get("ours"):
        between = (
            f" (merging {record.get('theirs') or 'their branch'} into "
            f"{record.get('ours') or 'this branch'})"
        )
    rid = record.get("id")
    return (
        f"[Memory MCP] A memory merge{between} is waiting for your review, "
        f"id {rid}. If applied, it adds {tally['added']} memories, changes "
        f"{tally['changed']} and removes {tally['removed']}. In "
        f"{tally['kept_ours']} conflicts this side's version was kept. None of "
        f"it has reached this project's memory yet. To see the diff, run "
        f"`{command} sync review show {rid}`. Then apply it with "
        f"`{command} sync review apply {rid}` (add `--skip <memory-id>` to "
        f"leave a row out), or decline it with `{command} sync review reject {rid}`."
    )


def render(record: dict) -> str:
    """The full diff, as plain text the session can read and decide from."""
    lines = [
        f"Memory merge review {record.get('id')} ({record.get('status')}), "
        f"created {record.get('created_at')}.",
    ]
    if record.get("theirs") or record.get("ours"):
        lines.append(
            f"Merging {record.get('theirs') or 'their branch'} into "
            f"{record.get('ours') or 'this branch'}."
        )
    lines.append("")
    lines += _section(
        "Added by their side", record.get("added") or [],
        lambda row: _row_lines(row, "+"),
    )
    lines += _section(
        "Changed by their side (theirs is newer)", record.get("changed") or [],
        lambda pair: _row_lines(pair["ours"], "-") + _row_lines(pair["theirs"], "+"),
    )
    lines += _section(
        "Removed by their side (hard delete)", record.get("removed") or [],
        _removed_lines,
    )
    lines += _section(
        "Conflicts kept as ours (their edit is NOT applied)",
        record.get("kept_ours") or [],
        lambda pair: _row_lines(pair["ours"], "=") + _row_lines(pair["theirs"], "x"),
    )
    if record.get("skipped"):
        lines.append(f"Skipped when applied: {', '.join(record['skipped'])}")
    return "\n".join(lines).rstrip() + "\n"


def _section(title: str, items: list, fmt) -> list[str]:
    if not items:
        return []
    lines = [f"{title}: {len(items)}"]
    for item in items[:_RENDER_LIMIT]:
        lines += fmt(item)
    if len(items) > _RENDER_LIMIT:
        lines.append(f"  ... and {len(items) - _RENDER_LIMIT} more in the record")
    lines.append("")
    return lines


def _row_lines(row: dict | None, mark: str) -> list[str]:
    if not row:
        return []
    content = str(row.get("content") or "")
    if len(content) > _CONTENT_PREVIEW:
        content = content[:_CONTENT_PREVIEW] + " ..."
    return [
        f"  {mark} [{row.get('category')}] {row.get('title')} "
        f"(id {row.get('id')}, updated {row.get('updated_at')}, "
        f"status {row.get('status')})",
        *(f"      {line}" for line in content.splitlines() or [""]),
    ]


def _removed_lines(entry: dict) -> list[str]:
    tomb = entry.get("tombstone") or {}
    head = (
        f"  - deleted {tomb.get('memory_id')} at {tomb.get('deleted_at')}"
        f"{' by ' + tomb['actor'] if tomb.get('actor') else ''}"
    )
    ours = entry.get("ours")
    return [head] + (_row_lines(ours, " ") if ours else ["      (this side did not have it)"])


# ---------- helpers ----------


def _checked_id(value: object) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        raise ReviewError(f"Not a review id: {value!r}")
    return value


def _parse(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
