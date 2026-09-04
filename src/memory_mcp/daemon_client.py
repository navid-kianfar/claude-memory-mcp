"""Talking to the local daemon instead of the database it has open.

DuckDB allows ONE writer per file across processes. The launchd daemon holds
that lock for every project it has touched, so a CLI command that opens the same
file directly fails with

    IO Error: Could not set lock on file ... Conflicting lock is held (PID n)

which says nothing about the daemon and offers no way forward. Worse, the crash
lands wherever the first write happens to be: `memory-mcp asoode push` created
tasks on the board and THEN died recording the mapping, leaving remote rows the
local store had no note of - the same shape as the bug that once produced 54
duplicates.

So a CLI write asks the daemon to do it. The daemon owns the lock, so the write
succeeds and every listener (the socket, the UI, the running MCP sessions) sees
it. Direct access stays as the fallback for when no daemon is running - a fresh
machine, `serve` stopped, a test - which is the same rule sync_cli already
follows for the same reason.
"""

import json
import urllib.error
import urllib.request

from memory_mcp.config import settings


class DaemonUnavailable(RuntimeError):
    """No daemon answered - the caller should fall back to direct access."""


class DaemonError(RuntimeError):
    """The daemon answered with a failure. NOT a reason to retry locally: the
    write reached the right process and was rejected on its merits."""


def base_url() -> str:
    return f"http://127.0.0.1:{settings.daemon_port}"


def call(path: str, method: str = "GET", payload: dict | None = None,
         timeout: float = 60.0) -> dict:
    """One request. Raises DaemonUnavailable if nothing is listening."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{base_url()}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode() or "{}"
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        try:
            detail = json.loads(detail).get("error", detail)
        except Exception:  # noqa: BLE001 - a non-JSON body is still the message
            pass
        raise DaemonError(f"daemon returned {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise DaemonUnavailable(str(e)) from e
    return json.loads(body) if body.strip() else {}


def is_running(timeout: float = 2.0) -> bool:
    try:
        return call("/api/health", timeout=timeout).get("status") == "ok"
    except (DaemonUnavailable, DaemonError):
        return False


def run(path: str, method: str, payload: dict | None, local):
    """Prefer the daemon; fall back to `local()` only when there is no daemon.

    A DaemonError is NOT caught: the request got to the process that owns the
    lock and failed there, so retrying locally would either fail the same way or
    fail on the lock and hide the real reason.
    """
    try:
        return call(path, method, payload), True
    except DaemonUnavailable:
        return local(), False


LOCK_HINT = (
    "the memory-mcp daemon has this project's database open (DuckDB allows one "
    "writer per file).\n"
    "  - the daemon can do this for you: it is what the MCP tools and the UI at "
    "http://127.0.0.1:{port} use\n"
    "  - or stop it first:  launchctl unload ~/Library/LaunchAgents/"
    "com.memory-mcp.daemon.plist"
)


def lock_message(err: Exception) -> str | None:
    """A readable explanation, or None when this is not the lock error."""
    text = str(err)
    if "Conflicting lock" not in text and "Could not set lock" not in text:
        return None
    return LOCK_HINT.format(port=settings.daemon_port)
