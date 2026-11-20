"""Pausing and resuming a project's board sync.

Somebody may want a project to stay local for a while - "only local tasks for
the next two weeks" - and then send everything to the board in one go when
they say so. This module holds that choice and the catch-up that ends it.

While a project is paused:

- the outbox keeps every row it is given; the flusher reads none of them, so
  no attempt is spent and nothing ages out, however long the pause lasts;
- nothing is read from the board either (reconcile, import, the time backfill
  and the session-start board read all stand down);
- local task tools work exactly as before.

`until` is a reminder, not a timer. Once the date arrives the brief asks the
agent to check with the user; sync never resumes on its own, because sending
two weeks of queued work is a decision for a person. It is a DATE: a full
timestamp is accepted and kept as its date (in the timestamp's own offset), and
the reply carries a `note` saying the time was dropped, so nobody believes a
pause will end at 09:30.

Resuming lifts the pause and drains the queue until it is empty or a send
fails. A long queue is drained in the caller for a short while and then
carried on in a background thread, so an MCP call or an HTTP request does not
hang on hundreds of round trips; `status` shows how far that drain has got.
"""

import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

from memory_mcp.db.registry import (
    clear_sync_pause,
    get_project_links,
    get_sync_pause,
    set_sync_pause,
)
from memory_mcp.exceptions import ValidationError

logger = logging.getLogger(__name__)

#: How long `resume` drains in the caller before handing the rest to a
#: background thread. Well inside the CLI's 60s daemon timeout.
RESUME_WAIT_SECONDS = 20.0

#: Rows per flush step while draining. Small, so a deadline check comes round
#: every few seconds even against a slow board.
DRAIN_BATCH = 25

#: A reason is a short note for the people reading the status, not a document.
MAX_REASON_LENGTH = 500

DRAIN_RUNNING = "running"
DRAIN_FINISHED = "finished"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ParsedUntil:
    """A normalised `until`, and whether a time of day was dropped to get it."""

    day: str | None
    time_dropped: bool = False


def parse_until(value: str | None, today: date) -> str | None:
    """Normalise an `until` value to YYYY-MM-DD, or None for no end date.

    A full ISO timestamp is accepted and reduced to its date; `read_until`
    also says whether that happened. A date before today is refused: a pause
    that is already over on the day it is set is almost certainly a typo.
    """
    return read_until(value, today).day


def read_until(value: str | None, today: date) -> ParsedUntil:
    """`parse_until`, plus whether the value carried a time that was dropped."""
    if value is None or not value.strip():
        return ParsedUntil(day=None)
    text = value.strip()
    time_dropped = False
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text).date()
        except ValueError:
            raise ValidationError(
                f"'until' should be an ISO date such as 2026-12-04, not {text[:40]!r}."
            ) from None
        time_dropped = True
    if parsed < today:
        raise ValidationError(
            f"'until' ({parsed.isoformat()}) is already in the past. Leave it out "
            "for a pause with no end date."
        )
    return ParsedUntil(day=parsed.isoformat(), time_dropped=time_dropped)


def until_note(day: str) -> str:
    """What the reply says when a timestamp was cut to its date."""
    return (
        f"'until' is a date, so the time was dropped and it was kept as {day}. "
        "It is a reminder on that day, not a timer: sync stays paused until "
        "someone resumes it."
    )


def is_overdue(until: str | None, today: date) -> bool:
    """Has the planned end of a pause arrived? False when there is none."""
    if not until:
        return False
    try:
        return today >= date.fromisoformat(until)
    except ValueError:
        return False


@dataclass
class _Drain:
    """How far the catch-up after a resume has got."""

    started_at: str
    state: str = DRAIN_RUNNING
    finished_at: str | None = None
    flushed: int = 0
    failed: int = 0
    skipped: int = 0
    abandoned: int = 0
    remaining: int = 0
    imported: int | None = None
    error: str | None = None
    stopped_by_pause: bool = False

    def finish(self) -> None:
        self.state = DRAIN_FINISHED
        self.finished_at = _now_iso()


class SyncPauseService:
    """Pause, resume and report a project's board sync."""

    def __init__(self, bridge, outbox_repo, project_service) -> None:
        self._bridge = bridge
        self._outbox = outbox_repo
        self._projects = project_service
        self._lock = threading.Lock()
        self._drains: dict[str, _Drain] = {}
        self._threads: dict[str, threading.Thread] = {}

    # ---------- reading ----------

    def status(self, slug: str) -> dict:
        """The pause, the queue behind it, and the latest resume's progress."""
        self._projects.get(slug)
        pause = get_sync_pause(slug)
        until = (pause or {}).get("until")
        with self._lock:
            drain = self._drains.get(slug)
            drain_view = asdict(drain) if drain is not None else None
        return {
            "slug": slug,
            "linked": bool(get_project_links(slug)),
            "paused": pause is not None,
            "since": (pause or {}).get("since"),
            "until": until,
            "reason": (pause or {}).get("reason"),
            "overdue": pause is not None and is_overdue(until, date.today()),
            "queued_ops": self._outbox.depth(slug),
            "drain": drain_view,
        }

    # ---------- pausing ----------

    def pause(self, slug: str, until: str | None = None, reason: str | None = None) -> dict:
        """Stop board traffic for a project. Pausing again updates until/reason
        and keeps the original `since`.

        Answers the sync state; a `note` is added only when `until` carried a
        time of day, to say it was kept as a date."""
        self._projects.get(slug)
        if not get_project_links(slug):
            raise ValidationError(
                f"'{slug}' is not linked to a board, so there is no sync to pause."
            )
        clean_reason = (reason or "").strip() or None
        if clean_reason is not None and len(clean_reason) > MAX_REASON_LENGTH:
            raise ValidationError(
                f"Please keep the reason under {MAX_REASON_LENGTH} characters."
            )
        parsed_until = read_until(until, date.today())
        clean_until = parsed_until.day
        existing = get_sync_pause(slug)
        since = (existing or {}).get("since") or _now_iso()
        set_sync_pause(slug, {"since": since, "until": clean_until, "reason": clean_reason})
        logger.info("board sync paused for %s (until %s)", slug, clean_until or "further notice")
        state = self.status(slug)
        if parsed_until.time_dropped and clean_until:
            state["note"] = until_note(clean_until)
        return state

    # ---------- resuming ----------

    def resume(self, slug: str, *, wait_seconds: float | None = RESUME_WAIT_SECONDS) -> dict:
        """Lift the pause and send what was queued.

        `wait_seconds` bounds how long this call drains before a background
        thread takes over; None drains to the end here, which is what a
        short-lived process without a daemon needs.
        """
        self._projects.get(slug)
        with self._lock:
            was_paused = clear_sync_pause(slug)
            running = self._threads.get(slug)
            if running is not None and running.is_alive():
                # A drain from an earlier resume is still going; it will pick
                # up anything queued since, so starting a second one gains
                # nothing and would only queue behind the same flush lock.
                drain = None
            elif not get_project_links(slug):
                # Never drain an unlinked project: the flusher drops rows it
                # has nowhere to send, and that is not what resume asked for.
                drain = None
            else:
                drain = _Drain(started_at=_now_iso())
                self._drains[slug] = drain
        if was_paused:
            logger.info("board sync resumed for %s", slug)
        if drain is not None:
            deadline = None if wait_seconds is None else time.monotonic() + wait_seconds
            if not self._drain(slug, drain, deadline):
                self._continue_in_background(slug, drain)
        return {**self.status(slug), "was_paused": was_paused}

    def _drain(self, slug: str, drain: _Drain, deadline: float | None) -> bool:
        """Flush until the queue is empty, a send fails, the queue stops moving
        or sync is paused again. False means the deadline came first and the
        drain is not finished."""
        while True:
            if self._bridge.is_paused(slug):
                drain.stopped_by_pause = True
                break
            result = self._bridge.flush(slug, limit=DRAIN_BATCH, passes=1)
            drain.flushed += result.get("flushed", 0)
            drain.failed += result.get("failed", 0)
            drain.skipped += result.get("skipped", 0)
            drain.abandoned += result.get("abandoned", 0)
            drain.remaining = result.get("remaining", 0)
            if result.get("paused"):
                drain.stopped_by_pause = True
                break
            if result.get("failed"):
                drain.error = self._outbox.last_failure(slug) or result.get("reason")
                break
            if not drain.remaining:
                self._catch_up_inbound(slug, drain)
                break
            moved = result.get("flushed", 0) + result.get("skipped", 0) + result.get("abandoned", 0)
            if not moved:
                drain.error = result.get("reason") or "the queue stopped moving"
                break
            if deadline is not None and time.monotonic() >= deadline:
                return False
        drain.finish()
        return True

    def _catch_up_inbound(self, slug: str, drain: _Drain) -> None:
        """Read the board once, now that our side has landed. The socket's
        catch-up may have moved its watermark past changes made while this
        project was paused, so a full reconcile is the only reliable read."""
        try:
            result = self._bridge.reconcile(slug)
        except Exception as e:  # noqa: BLE001 - reported in the drain, not lost
            drain.error = f"reading the board after the catch-up failed: {e}"
            return
        drain.imported = result.get("imported", 0)
        failures = result.get("failed") or []
        if failures and drain.error is None:
            drain.error = f"reading the board after the catch-up failed: {failures[0].get('error')}"

    def _continue_in_background(self, slug: str, drain: _Drain) -> None:
        def _run() -> None:
            try:
                self._drain(slug, drain, None)
            except Exception as e:  # noqa: BLE001 - recorded where status reads it
                logger.exception("the catch-up after resuming %s stopped", slug)
                drain.error = f"{type(e).__name__}: {e}"
                drain.finish()
            finally:
                with self._lock:
                    self._threads.pop(slug, None)

        thread = threading.Thread(target=_run, name=f"asoode-resume-{slug}", daemon=True)
        with self._lock:
            self._threads[slug] = thread
        thread.start()

    def wait_for_drains(self, timeout: float) -> None:
        """Give background catch-ups a chance to finish, for a clean shutdown."""
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = tuple(self._threads.values())
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            thread.join(remaining)
