"""Remembers which remote tasks WE just wrote, so their echo can be ignored.

asoode broadcasts every change to every member of the work package, and its
socket layer applies no actor exclusion - deliberately, so a user's other
devices stay in step (message-handler.service.ts:93-97). The actor IS known
server-side (`userId: event.actorId`, domain-event.listener.ts:131) but it is
dropped when the client model is built, so a listener CANNOT tell its own
writes from anyone else's by inspecting the payload.

Hence this: the writer records what it touched, and the listener asks. Pushing
27 tasks produced 37 events and 7 full board reads that could not find anything
to do; on a large board that is real latency and API load for no answer.

WHY NOT A TIME WINDOW: "ignore everything for N seconds after a flush" is one
line, but it drops a genuine concurrent change that lands in that window. Keying
on the task id only ignores echoes of the exact tasks we wrote, so someone
else's change to a DIFFERENT task is still prompt - which is the whole point of
having a socket rather than a poll.

The residual gap is narrow and deliberate: someone else editing THE SAME task
within the window is treated as our echo. That costs nothing today, because
`reconcile` only ever creates and would do nothing for a task it already knows.
"""

import threading
import time

# How long an echo may take to come back. The path is REST reply -> RabbitMQ ->
# socket service -> us, normally well under a second; this is slack for a slow
# queue, not a guess at the latency.
ECHO_WINDOW_SECONDS = 30.0
# A flush of a big board can touch hundreds of tasks. Bounded so a long-running
# daemon cannot grow this without limit.
MAX_ENTRIES = 4000


class EchoLog:
    """Thread-safe: written by the flusher thread, read by the event loop."""

    def __init__(self, window: float = ECHO_WINDOW_SECONDS, max_entries: int = MAX_ENTRIES):
        self._window = window
        self._max = max_entries
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self.suppressed = 0

    def note(self, remote_id: str | None) -> None:
        """Record a write we are about to make to `remote_id`."""
        if not remote_id:
            return
        now = time.monotonic()
        with self._lock:
            self._seen[remote_id] = now
            if len(self._seen) > self._max:
                self._prune(now, force=True)

    def is_echo(self, remote_ids: set[str]) -> bool:
        """True when every id given is one we wrote recently.

        An empty set is never an echo: an event we cannot attribute to a task
        gets reconciled, because being wrong the other way loses a change.
        """
        if not remote_ids:
            return False
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            hit = all(rid in self._seen for rid in remote_ids)
            if hit:
                self.suppressed += 1
            return hit

    def _prune(self, now: float, force: bool = False) -> None:
        """Caller holds the lock."""
        cutoff = now - self._window
        self._seen = {rid: at for rid, at in self._seen.items() if at > cutoff}
        if force and len(self._seen) > self._max:
            # Still over after dropping the expired: keep the newest.
            newest = sorted(self._seen.items(), key=lambda kv: kv[1], reverse=True)
            self._seen = dict(newest[: self._max])
