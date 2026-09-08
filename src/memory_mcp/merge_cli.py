"""`memory-mcp merge-snapshot` - the git merge driver for the memory snapshot.

git calls this with the three versions of the file when a pull conflicts:

    memory-mcp merge-snapshot %O %A %B [%P]
      %O  the merge base (an EMPTY file when the two branches added it
          independently - a normal state, not an error)
      %A  our version. The driver MUST leave the merged result here.
      %B  their version
      %P  the real pathname, used only for messages

Exit 0 means "%A now holds the merge". Any other exit means "I could not decide"
and git records a conflict for a human, leaving %A as it was.

WHY THIS EXISTS AT ALL. The snapshot is a DuckDB file, which is binary to git.
Two clones that both add a rule produce a conflict git cannot resolve, and the
default resolution - take one side - silently discards the other side's
memories. Without this driver, moving the snapshot to DuckDB would be a
DOWNGRADE from the per-category JSON, which at least conflicted visibly, line by
line. The two shipped together for that reason.

The resolution policy lives in `db.snapshot.merge_snapshots`; this module is the
git-facing shell around it. The one thing worth repeating here: a row missing
from one side is NOT a deletion, it is a row that side has not pulled yet. Only
an explicit tombstone deletes.
"""

import argparse
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from memory_mcp.db.snapshot import SnapshotError, merge_snapshots

PROG = "memory-mcp merge-snapshot"


def _log_failure(argv, error: Exception) -> None:
    """Leave a trace: git shows the driver's stderr, but only sometimes.

    A merge driver that fails quietly is indistinguishable from one that is not
    installed, and the difference matters - one leaves a conflict to resolve by
    hand, the other means memories were nearly lost.
    """
    try:
        from memory_mcp.config import settings

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with (settings.data_dir / "sync.log").open("a") as fh:
            fh.write(f"\n=== {stamp} merge-snapshot {' '.join(argv)} failed ===\n")
            fh.write(f"{type(error).__name__}: {error}\n")
            traceback.print_exc(file=fh)
    except Exception:  # noqa: BLE001 - logging must never be the failure
        pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Reconcile two memory snapshot databases with SQL.",
    )
    parser.add_argument("base", help="%%O - merge base (may be an empty file)")
    parser.add_argument("ours", help="%%A - our version; the result is written here")
    parser.add_argument("theirs", help="%%B - their version")
    parser.add_argument("pathname", nargs="?", default=None, help="%%P - for messages")
    args = parser.parse_args(argv)

    ours = Path(args.ours)
    name = args.pathname or ours.name
    try:
        summary = merge_snapshots(args.base, ours, args.theirs, ours)
    except SnapshotError as e:
        # A conflict git leaves to a human is the correct outcome here: it is
        # visible and recoverable, which is more than "pick a side" ever is.
        print(f"[Memory MCP] Cannot merge {name}: {e}", file=sys.stderr)
        _log_failure(argv, e)
        return 1
    except Exception as e:  # noqa: BLE001 - never resolve a merge by accident
        print(
            f"[Memory MCP] Unexpected failure merging {name}: {e}. "
            f"Left as a conflict for you to resolve.",
            file=sys.stderr,
        )
        _log_failure(argv, e)
        return 1

    detail = ""
    if summary["deleted_by_tombstone"]:
        detail += f", {summary['deleted_by_tombstone']} removed by tombstone"
    if summary["resurrected"]:
        detail += f", {summary['resurrected']} kept over a tombstone (newer edit)"
    print(
        f"[Memory MCP] Merged {name}: {summary['memories']} memories, "
        f"{summary['provenance']} provenance rows{detail}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
