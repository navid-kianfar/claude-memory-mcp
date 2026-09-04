"""The inbound live channel: asoode pushes, this pulls.

`reconcile` already covers correctness - it runs after every mirror and picks up
anything added on a board. This makes it PROMPT: without it, a task someone adds
on the board is not seen until this session next writes something, which on a
quiet session is never.

So the socket is an optimisation over the poll, not a replacement for it, and
that is deliberate: a dropped connection, a revoked token or a server restart
degrades to the same correctness the poll already provides. Nothing here may
raise into the daemon.

It does not rely on that safety net alone, though. asoode replays nothing, so
every connect - the first one included, because the daemon was deaf before it
started - reconciles each linked project once to collect what was missed.

WHAT IT LISTENS FOR: asoode emits ONE event name for everything -
'push-notification' with {type, data} where `type` is the discriminator
(message-handler.service.ts:103-120). Rather than enumerate ActivityType values
that can change on the asoode side, this reacts to any event carrying a
packageId that matches a linked board, and lets `reconcile` decide what actually
changed. Being wrong here costs one extra reconcile; enumerating wrongly costs a
missed change.
"""

import asyncio
import contextlib
import logging
import time

logger = logging.getLogger(__name__)

# Coalesce a burst into one reconcile. Moving five tasks on a board emits five
# events; reconciling five times would be five full board reads for one answer.
DEBOUNCE_SECONDS = 2.0
# A dropped socket backs off rather than reconnecting in a tight loop.
RECONNECT_MIN, RECONNECT_MAX = 2.0, 60.0
# A reconnect catches up on what was missed while deaf, but no more often than
# this. The backoff cannot be the floor: it RESETS on every good connection, so
# a socket that connects and drops repeatedly would otherwise read every board
# every couple of seconds.
#
# Five minutes, not one, because a sweep is not cheap: this machine has 79 links
# across 14 projects, and reading them all takes the better part of a minute. At
# a 60s floor a flapping socket would spend most of its life sweeping. The cost
# of the floor is bounded and small - reconcile still runs after every mirror,
# so this only delays noticing a change made while nothing local is happening.
CATCH_UP_MIN_INTERVAL = 300.0


class SocketSubscriber:
    """Keeps a socket open and reconciles the projects a board event touches."""

    def __init__(self, bridge, get_links, get_credentials):
        self._bridge = bridge
        self._get_links = get_links          # () -> list[dict] of every link
        self._get_credentials = get_credentials  # () -> (socket_url, token) | None
        self._task: asyncio.Task | None = None
        self._pending: set[str] = set()
        self._timer: asyncio.Task | None = None
        self._client = None
        self._stopping = False
        self.connected = False
        self.started = False
        self.not_started_because: str | None = None
        self.events_seen = 0
        self.reconciles = 0
        self.echoes = 0
        self.catch_ups = 0
        self._last_catch_up: float | None = None
        self.last_error: str | None = None

    # ---------- lifecycle ----------

    def start(self) -> bool:
        """Begin, if there is anything to listen for. Returns whether it started."""
        creds = self._safe(self._get_credentials)
        if not creds:
            self.not_started_because = "no socket url or stored credential"
            return False
        if not self._safe(self._get_links):
            # Nothing is linked, so no event could apply to anything.
            self.not_started_because = "no project is linked to a board"
            return False
        self._task = asyncio.create_task(self._run(), name="asoode-socket")
        self.started = True
        return True

    async def stop(self) -> None:
        """Disconnect FIRST, then cancel.

        The other order kills engineio's read loop and then asks disconnect() to
        await it, which raises CancelledError out of stop() - a noisy shutdown
        for a task that was going away anyway.
        """
        self._stopping = True
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        for task in (self._timer, self._task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self.connected = False

    # ---------- the loop ----------

    async def _run(self) -> None:
        delay = RECONNECT_MIN
        while not self._stopping:
            try:
                await self._connect_once()
                delay = RECONNECT_MIN          # a good connection resets the backoff
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the daemon must survive anything
                self.last_error = f"{type(e).__name__}: {e}"
                logger.debug("asoode socket: %s", self.last_error)
            self.connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _connect_once(self) -> None:
        import socketio

        creds = self._get_credentials()
        if not creds:
            raise RuntimeError("no socket url or credential")
        url, token = creds

        client = socketio.AsyncClient(reconnection=False, logger=False,
                                      engineio_logger=False)
        self._client = client

        @client.event
        async def connect():  # noqa: D401
            self.connected = True
            logger.info("asoode socket connected")

        @client.event
        async def disconnect():  # noqa: D401
            self.connected = False

        @client.on("*")
        async def any_event(event, *args):  # noqa: ANN001
            self.events_seen += 1
            self._note(args)

        await client.connect(
            url, auth={"token": token},
            headers={"Authorization": f"Bearer {token}"},
            transports=["websocket"],
        )
        # Whatever happened while we were not listening is not replayed by
        # asoode, so ask. The FIRST connect counts too: before the daemon
        # started it was as deaf as a dropped socket.
        await self._catch_up()
        await client.wait()

    # ---------- reacting ----------

    def _note(self, args) -> None:
        """Queue a reconcile for whichever project owns the board in this event."""
        package_ids = set()
        task_ids = set()
        for arg in args:
            package_ids |= _package_ids(arg)
            task_ids |= _task_ids(arg)
        if not package_ids:
            return
        # Our own write, echoed back. asoode broadcasts to every member without
        # excluding the actor and drops the actor id on the way, so the only way
        # to recognise it is to have recorded the write - see EchoLog. Decided
        # per EVENT, before the debounce: a burst of our echoes plus one real
        # change must still reconcile.
        echo = getattr(self._bridge, "echo", None)
        if echo is not None and echo.is_echo(task_ids):
            self.echoes += 1
            return
        links = self._safe(self._get_links) or []
        slugs = {
            link["slug"] for link in links
            if link.get("remote_work_package_id") in package_ids
        }
        if not slugs:
            return
        self._pending |= slugs
        if self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(self._flush_soon())

    async def _catch_up(self) -> None:
        """Reconcile every linked project once, rate-limited.

        This is the poll the socket was always an optimisation over: it is what
        makes a dropped connection cost promptness rather than correctness.
        """
        now = time.monotonic()
        if (self._last_catch_up is not None
                and now - self._last_catch_up < CATCH_UP_MIN_INTERVAL):
            return
        self._last_catch_up = now
        links = self._safe(self._get_links) or []
        for slug in sorted({link["slug"] for link in links if link.get("slug")}):
            try:
                await asyncio.to_thread(self._bridge.reconcile, slug)
                self.catch_ups += 1
            except Exception as e:  # noqa: BLE001 - a catch-up must not kill the socket
                self.last_error = f"catch-up {slug}: {e}"

    async def _flush_soon(self) -> None:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        slugs, self._pending = self._pending, set()
        for slug in slugs:
            try:
                # reconcile is blocking (HTTP + DuckDB); off the event loop.
                await asyncio.to_thread(self._bridge.reconcile, slug)
                self.reconciles += 1
            except Exception as e:  # noqa: BLE001
                self.last_error = f"reconcile {slug}: {e}"

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None

    def status(self) -> dict:
        return {
            "started": self.started,
            "not_started_because": self.not_started_because,
            "connected": self.connected,
            "events_seen": self.events_seen,
            "reconciles": self.reconciles,
            "echoes_suppressed": self.echoes,
            "catch_ups": self.catch_ups,
            "last_error": self.last_error,
        }


def _task_ids(value) -> set[str]:
    """The task this event is about, read SHALLOWLY on purpose.

    asoode sends {type, data, push}; the task id sits at data.id (add/edit/move)
    or data.taskId (comment, time, attachment). Walking the whole payload the
    way `_package_ids` does would also collect the comment id, the attachment id
    and every member id, and one unrecognised id is enough to make an echo look
    like someone else's change.

    Finding nothing is safe: an unattributable event reconciles.
    """
    if not isinstance(value, dict):
        return set()
    scopes = [v for v in (value, value.get("data")) if isinstance(v, dict)]

    def pick(*keys) -> set[str]:
        return {
            scope[k] for scope in scopes for k in keys
            if isinstance(scope.get(k), str) and scope[k]
        }

    # `taskId` WINS over `id`. A comment event is {...commentViewModel, taskId},
    # so `id` there is the COMMENT - taking both would put an id we never wrote
    # in the set and make our own comment look like someone else's change.
    return pick("taskId", "task_id") or pick("id")


def _package_ids(value, depth: int = 0) -> set[str]:
    """Every packageId anywhere in an event payload.

    Walks rather than reading a fixed path because asoode nests the id
    differently per ActivityType - `data.packageId` for a task add, and inside a
    nested model for others. A miss here means a change is not noticed, which is
    worse than walking a small dict.
    """
    found: set[str] = set()
    if depth > 4:
        return found
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("packageId", "package_id") and isinstance(item, str):
                found.add(item)
            else:
                found |= _package_ids(item, depth + 1)
    elif isinstance(value, list):
        for item in value[:50]:
            found |= _package_ids(item, depth + 1)
    return found
