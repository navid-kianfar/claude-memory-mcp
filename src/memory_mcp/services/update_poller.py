"""Periodic check for a newer version of memory-mcp.

WHY THIS IS ONLY THE DETECTOR, and never the installer:

The daemon runs under macOS TCC and cannot read the source repo when it lives in
a protected folder like ~/Desktop - the same constraint daemon.py's lifespan
records for project folder I/O. So this polls GitHub over the network, which is
allowed, and writes what it found to the registry. Something running in the
USER's context - the Stop hook - does the `git pull` and the reinstall.

And it never applies an update on its own, because applying one reloads the
launchd daemon and drops every live MCP connection. Observed repeatedly on
2026-09-04: each reinstall produced "MCP server memory session expired" in the
running session. A silent mid-session auto-update is therefore not something
this can offer; the safe window is the end of a turn, or an explicit approval.

UNKNOWN IS NOT UP TO DATE. UpdateService.check() reports update_available=False
both when there is genuinely no update and when the check could not run at all
(it sets source="unknown" for the latter). Conflating those would mean a network
outage silently reads as "you are current", which is the one outcome that makes
an update checker actively harmful. So a failed check never overwrites the last
known good answer - it only records that the attempt failed.
"""

import asyncio
import contextlib
import json
import logging
import random
import time

from memory_mcp.db.registry import get_setting, set_setting

log = logging.getLogger(__name__)

#: How often to ask. Unauthenticated GitHub allows 60 requests/hour per IP and
#: this is 6, so there is room; the cost of asking more often is not rate limit
#: but noise, since a release does not appear more than once in ten minutes.
POLL_INTERVAL_SECONDS = 600.0

#: Never fire the first check at t=0. launchd restarts the daemon on failure, so
#: a crash loop would otherwise become a request storm against the API.
INITIAL_DELAY_SECONDS = 45.0

#: Back-off after GitHub refuses us (403 is the rate-limit answer). Hammering a
#: rate limit is how you extend it.
BACKOFF_SECONDS = 3600.0

#: Registry keys. Machine-wide, so the SQLite registry rather than any project's
#: DuckDB store.
STATUS_KEY = "update:status"
CHECKED_AT_KEY = "update:last_checked_at"
ERROR_KEY = "update:last_error"


def read_status() -> dict | None:
    """The last known good check result, or None if we have never had one.

    Cheap: a single SQLite read. The hook path calls this on every prompt and
    must never perform the network check itself.
    """
    raw = get_setting(STATUS_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def update_available() -> bool:
    """True only when a SUCCESSFUL check found a newer version."""
    status = read_status()
    if not status:
        return False
    # source=="unknown" means the check itself failed; it is not an answer.
    return bool(status.get("update_available")) and status.get("source") != "unknown"


class UpdatePoller:
    """Asks UpdateService for a newer version on a timer, and records the answer.

    Shaped after SocketSubscriber: an asyncio task owned by the daemon lifespan,
    which is the only periodic runner this codebase has. Nothing it does may
    raise into the app - like the socket subscription, this is an optimisation,
    and a GitHub outage must not affect the daemon.
    """

    def __init__(self, update_service, interval: float = POLL_INTERVAL_SECONDS):
        self._service = update_service
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> bool:
        if self._task is not None:
            return False
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="update-poller")
        return True

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _run(self) -> None:
        # Stagger, and jitter it so several machines behind one IP do not line up.
        await asyncio.sleep(INITIAL_DELAY_SECONDS + random.uniform(0, 15))
        while not self._stopping:
            delay = self._interval
            try:
                rate_limited = await asyncio.to_thread(self.check_once)
                if rate_limited:
                    delay = BACKOFF_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - never take the daemon down
                log.warning("update poll failed: %s", e)
            await asyncio.sleep(delay)

    def check_once(self) -> bool:
        """Run one check and record it. Returns True when we were rate limited.

        Synchronous on purpose - UpdateService uses urllib and subprocess, so it
        blocks; the caller runs it on a worker thread.
        """
        set_setting(CHECKED_AT_KEY, str(time.time()))
        try:
            result = self._service.check()
        except Exception as e:  # noqa: BLE001
            set_setting(ERROR_KEY, f"{type(e).__name__}: {e}")
            return False

        if result.get("source") == "unknown":
            # The check could not run. Keep whatever we last knew rather than
            # replacing a real answer with a fabricated "up to date".
            warnings = result.get("warnings") or ["update check failed"]
            set_setting(ERROR_KEY, "; ".join(str(w) for w in warnings))
            return any("rate limit" in str(w).lower() for w in warnings)

        set_setting(ERROR_KEY, "")
        set_setting(STATUS_KEY, json.dumps(result, default=str))
        if result.get("update_available"):
            log.info(
                "update available: %s -> %s",
                result.get("current_version"), result.get("latest_version"),
            )
        return False
