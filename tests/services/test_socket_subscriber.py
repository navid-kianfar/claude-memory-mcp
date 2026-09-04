"""The live inbound channel.

It is an OPTIMISATION over the reconcile poll, never a replacement: a dropped
socket, a revoked ticket or a server restart degrades to the behaviour that
existed before it. So the tests are mostly about failing quietly and reacting to
the right events - not about the socket protocol, which is the library's job.
"""

import asyncio

import pytest

from memory_mcp.services.echo_log import EchoLog
from memory_mcp.services.socket_subscriber import (
    SocketSubscriber,
    _package_ids,
    _task_ids,
)


class Bridge:
    def __init__(self, fail=False):
        self.reconciled = []
        self.fail = fail

    def reconcile(self, slug):
        if self.fail:
            raise RuntimeError("reconcile blew up")
        self.reconciled.append(slug)
        return {"imported": 0}


LINKS = [
    {"slug": "alpha", "remote_work_package_id": "wp-a"},
    {"slug": "beta", "remote_work_package_id": "wp-b"},
]


def _sub(bridge=None, links=LINKS, creds=("https://socket.test", "ticket")):
    return SocketSubscriber(
        bridge or Bridge(), lambda: links, lambda: creds,
    )


class TestFindingThePackageId:
    """asoode nests the id differently per ActivityType, so the payload is walked
    rather than read at a fixed path. A miss means a change goes unnoticed."""

    def test_top_level(self):
        assert _package_ids({"packageId": "wp-1"}) == {"wp-1"}

    def test_nested(self):
        assert _package_ids({"data": {"task": {"packageId": "wp-1"}}}) == {"wp-1"}

    def test_snake_case_too(self):
        assert _package_ids({"data": {"package_id": "wp-2"}}) == {"wp-2"}

    def test_several_in_a_list(self):
        payload = {"items": [{"packageId": "a"}, {"packageId": "b"}]}
        assert _package_ids(payload) == {"a", "b"}

    def test_absent(self):
        assert _package_ids({"type": 5, "data": {"title": "x"}}) == set()

    def test_depth_is_capped(self):
        """A pathological payload must not walk forever."""
        deep = {"a": {"b": {"c": {"d": {"e": {"packageId": "too-deep"}}}}}}
        assert _package_ids(deep) == set()


class TestStartingConditions:
    def test_does_not_start_without_a_credential(self):
        sub = _sub(creds=None)
        assert sub.start() is False
        assert "credential" in sub.not_started_because

    def test_does_not_start_when_nothing_is_linked(self):
        """No link means no event could apply to anything."""
        sub = _sub(links=[])
        assert sub.start() is False
        assert "linked" in sub.not_started_because

    def test_a_broken_credential_lookup_is_not_a_crash(self):
        def boom():
            raise RuntimeError("registry unreadable")

        sub = SocketSubscriber(Bridge(), lambda: LINKS, boom)
        assert sub.start() is False


class TestReacting:
    @pytest.mark.asyncio
    async def test_an_event_reconciles_the_project_that_owns_the_board(self):
        bridge = Bridge()
        sub = _sub(bridge)
        sub._note(({"data": {"packageId": "wp-b"}},))
        await asyncio.sleep(2.3)
        assert bridge.reconciled == ["beta"]

    @pytest.mark.asyncio
    async def test_an_event_for_an_unlinked_board_is_ignored(self):
        bridge = Bridge()
        sub = _sub(bridge)
        sub._note(({"data": {"packageId": "wp-someone-elses"}},))
        await asyncio.sleep(2.3)
        assert bridge.reconciled == []

    @pytest.mark.asyncio
    async def test_a_burst_collapses_into_one_reconcile(self):
        """Moving five cards emits five events; five board reads for one answer
        is exactly what the debounce exists to prevent."""
        bridge = Bridge()
        sub = _sub(bridge)
        for _ in range(5):
            sub._note(({"data": {"packageId": "wp-a"}},))
        await asyncio.sleep(2.3)
        assert bridge.reconciled == ["alpha"]

    @pytest.mark.asyncio
    async def test_two_boards_in_one_burst_reconcile_both(self):
        bridge = Bridge()
        sub = _sub(bridge)
        sub._note(({"data": {"packageId": "wp-a"}},))
        sub._note(({"data": {"packageId": "wp-b"}},))
        await asyncio.sleep(2.3)
        assert sorted(bridge.reconciled) == ["alpha", "beta"]

    @pytest.mark.asyncio
    async def test_a_failing_reconcile_is_recorded_not_raised(self):
        """This runs in a background task; an exception there is invisible and
        would take the subscription down with it."""
        bridge = Bridge(fail=True)
        sub = _sub(bridge)
        sub._note(({"data": {"packageId": "wp-a"}},))
        await asyncio.sleep(2.3)
        assert sub.last_error and "blew up" in sub.last_error


class TestStatusIsHonest:
    def test_it_reports_not_started_and_why(self):
        sub = _sub(creds=None)
        sub.start()
        status = sub.status()
        assert status["started"] is False
        assert status["connected"] is False
        assert status["not_started_because"]

    def test_a_silent_dead_task_is_visible(self):
        """A background task that might be dead is worse than none, so the
        status carries enough to tell the difference."""
        sub = _sub()
        assert set(sub.status()) >= {
            "started", "connected", "events_seen", "reconciles", "last_error",
        }


class TestFindingTheTaskId:
    """The payload shapes below are taken from asoode's own emitters
    (apps/backend/src/modules/tasks/tasks.service.ts), not invented here."""

    def test_task_add_carries_the_view_model_id(self):
        # data: { ...viewModel, packageId }
        payload = {"type": 34, "data": {"id": "t-1", "title": "x", "packageId": "wp-a"}}
        assert _task_ids(payload) == {"t-1"}

    def test_task_edit_carries_id(self):
        payload = {"data": {"id": "t-2", "title": "new", "packageId": "wp-a",
                            "listId": "l-1"}}
        assert _task_ids(payload) == {"t-2"}

    def test_task_move_carries_id(self):
        payload = {"data": {"id": "t-3", "packageId": "wp-a", "listId": "l-2",
                            "oldListId": "l-1"}}
        assert _task_ids(payload) == {"t-3"}

    def test_comment_prefers_taskid_over_the_comment_id(self):
        """data is { ...commentViewModel, taskId, ... }, so `id` is the COMMENT.

        Collecting both would put an id we never wrote into the set and make our
        own comment look like somebody else's change."""
        payload = {"data": {"id": "comment-9", "message": "hi", "taskId": "t-4",
                            "packageId": "wp-a"}}
        assert _task_ids(payload) == {"t-4"}

    def test_time_entry_carries_taskid(self):
        payload = {"data": {"taskId": "t-5", "packageId": "wp-a", "action": "add",
                            "entry": {"id": "entry-1"}}}
        assert _task_ids(payload) == {"t-5"}

    def test_attachment_carries_taskid(self):
        payload = {"data": {"taskId": "t-6", "packageId": "wp-a",
                            "attachment": {"id": "att-1"}}}
        assert _task_ids(payload) == {"t-6"}

    def test_nothing_found_is_not_an_error(self):
        assert _task_ids({"type": 5, "data": {"packageId": "wp-a"}}) == set()

    def test_a_non_dict_is_survivable(self):
        assert _task_ids("nonsense") == set()


class EchoBridge(Bridge):
    """A bridge that remembers its own writes, like the real one."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.echo = EchoLog()


class TestIgnoringOurOwnEcho:
    """asoode broadcasts our writes back to us - it does no actor exclusion and
    drops the actor id before the client sees it - so the writer has to say."""

    @pytest.mark.asyncio
    async def test_an_echo_of_our_own_write_does_not_reconcile(self):
        bridge = EchoBridge()
        bridge.echo.note("t-1")
        sub = _sub(bridge)
        sub._note(({"data": {"id": "t-1", "packageId": "wp-a"}},))
        await asyncio.sleep(2.3)
        assert bridge.reconciled == []
        assert sub.echoes == 1

    @pytest.mark.asyncio
    async def test_someone_elses_change_still_reconciles(self):
        bridge = EchoBridge()
        bridge.echo.note("t-1")
        sub = _sub(bridge)
        sub._note(({"data": {"id": "t-999", "packageId": "wp-a"}},))
        await asyncio.sleep(2.3)
        assert bridge.reconciled == ["alpha"]

    @pytest.mark.asyncio
    async def test_a_real_change_inside_a_burst_of_our_echoes_survives(self):
        """The reason suppression is decided per EVENT and not after the
        debounce: our 27 pushes must not swallow one real change."""
        bridge = EchoBridge()
        for i in range(5):
            bridge.echo.note(f"t-{i}")
        sub = _sub(bridge)
        for i in range(5):
            sub._note(({"data": {"id": f"t-{i}", "packageId": "wp-a"}},))
        sub._note(({"data": {"id": "someone-else", "packageId": "wp-a"}},))
        await asyncio.sleep(2.3)
        assert bridge.reconciled == ["alpha"]
        assert sub.echoes == 5

    @pytest.mark.asyncio
    async def test_an_event_with_no_task_id_is_never_treated_as_an_echo(self):
        """A list reorder or a package setting carries no task; not being able
        to attribute it must mean reconcile, not skip."""
        bridge = EchoBridge()
        bridge.echo.note("t-1")
        sub = _sub(bridge)
        sub._note(({"data": {"packageId": "wp-a"}},))
        await asyncio.sleep(2.3)
        assert bridge.reconciled == ["alpha"]

    @pytest.mark.asyncio
    async def test_a_bridge_without_an_echo_log_still_works(self):
        """Older callers and tests inject a bare bridge; suppression is an
        optimisation and must not be a requirement."""
        bridge = Bridge()
        sub = _sub(bridge)
        sub._note(({"data": {"id": "t-1", "packageId": "wp-a"}},))
        await asyncio.sleep(2.3)
        assert bridge.reconciled == ["alpha"]

    def test_the_count_is_reported(self):
        assert "echoes_suppressed" in _sub().status()


class TestCatchingUpAfterAnOutage:
    """asoode replays nothing. Whatever changed while the socket was down is
    invisible until something asks - so a connect asks."""

    @pytest.mark.asyncio
    async def test_a_connect_reconciles_every_linked_project(self):
        bridge = Bridge()
        sub = _sub(bridge)
        await sub._catch_up()
        assert sorted(bridge.reconciled) == ["alpha", "beta"]
        assert sub.catch_ups == 2

    @pytest.mark.asyncio
    async def test_the_first_connect_counts_as_an_outage(self):
        """Before the daemon started it was as deaf as a dropped socket, so the
        first connect must catch up too - not just reconnects."""
        bridge = Bridge()
        sub = _sub(bridge)
        assert sub._last_catch_up is None
        await sub._catch_up()
        assert bridge.reconciled != []

    @pytest.mark.asyncio
    async def test_a_flapping_socket_does_not_read_every_board_repeatedly(self):
        """The reconnect backoff RESETS on every good connection, so it cannot
        be the floor here - the catch-up needs its own."""
        bridge = Bridge()
        sub = _sub(bridge)
        for _ in range(5):
            await sub._catch_up()
        assert sorted(bridge.reconciled) == ["alpha", "beta"], "one catch-up, not five"

    @pytest.mark.asyncio
    async def test_the_floor_expires(self):
        from memory_mcp.services import socket_subscriber

        bridge = Bridge()
        sub = _sub(bridge)
        await sub._catch_up()
        sub._last_catch_up -= socket_subscriber.CATCH_UP_MIN_INTERVAL + 1
        await sub._catch_up()
        assert len(bridge.reconciled) == 4

    @pytest.mark.asyncio
    async def test_a_failing_catch_up_is_recorded_not_raised(self):
        """It runs inside the connect path; raising there would drop the socket
        that just came up."""
        bridge = Bridge(fail=True)
        sub = _sub(bridge)
        await sub._catch_up()
        assert sub.last_error and "catch-up" in sub.last_error

    @pytest.mark.asyncio
    async def test_no_links_is_not_an_error(self):
        bridge = Bridge()
        sub = _sub(bridge, links=[])
        await sub._catch_up()
        assert bridge.reconciled == []

    def test_the_count_is_reported(self):
        assert "catch_ups" in _sub().status()
