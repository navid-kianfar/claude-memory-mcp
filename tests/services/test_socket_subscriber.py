"""The live inbound channel.

It is an OPTIMISATION over the reconcile poll, never a replacement: a dropped
socket, a revoked ticket or a server restart degrades to the behaviour that
existed before it. So the tests are mostly about failing quietly and reacting to
the right events - not about the socket protocol, which is the library's job.
"""

import asyncio

import pytest

from memory_mcp.services.socket_subscriber import SocketSubscriber, _package_ids


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
