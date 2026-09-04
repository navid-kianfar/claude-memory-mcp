"""The catch-up sweep asks what changed before re-reading every board.

MEASURED CONTEXT: 79 links across 14 projects. The first sweep took ~15 minutes,
almost all of it local DuckDB writes for ~1,148 imported tasks; a second full
sweep finished in under a minute. So the delta does not rescue the first run - it
makes the steady state one request instead of seventy-nine, and the steady state
is what runs every few minutes forever.

THE RULE THE TESTS ENFORCE: when we cannot be sure what changed, sweep
EVERYTHING. A delta that silently returns nothing is indistinguishable from being
up to date, and that is the failure mode that would quietly stop syncing.
"""

import asyncio

import pytest

from memory_mcp.db.registry import get_setting, set_setting
from memory_mcp.services.socket_subscriber import (
    CATCH_UP_WATERMARK_KEY, SocketSubscriber,
)


class _Caps:
    def __init__(self, feed): self.supports_change_feed = feed


class _Provider:
    def __init__(self, feed=True, containers=None, watermark="2026-01-01T00:00:00Z",
                 boom=None):
        self.capabilities = _Caps(feed)
        self._containers = containers if containers is not None else set()
        self._watermark = watermark
        self._boom = boom
        self.calls = 0

    def changed_containers_since(self, since):
        self.calls += 1
        if self._boom:
            raise self._boom
        return set(self._containers), self._watermark


class _Bridge:
    def __init__(self, provider): self._provider = provider
    def provider_for(self, link): return self._provider
    def reconcile(self, slug): pass


LINKS = [
    {"slug": "alpha", "remote_work_package_id": "wp-a"},
    {"slug": "beta", "remote_work_package_id": "wp-b"},
    {"slug": "gamma", "remote_work_package_id": "wp-c"},
]


def _subscriber(provider):
    return SocketSubscriber(_Bridge(provider), lambda: LINKS, lambda: None)


@pytest.fixture(autouse=True)
def _clean():
    set_setting(CATCH_UP_WATERMARK_KEY, "")
    yield
    set_setting(CATCH_UP_WATERMARK_KEY, "")


class TestCatchUpDelta:
    def test_no_watermark_sweeps_everything(self):
        """First run. There is nothing to be incremental about."""
        provider = _Provider()
        assert _subscriber(provider)._changed_slugs(LINKS) == (None, None)
        assert provider.calls == 0, "must not even ask without a watermark"

    def test_a_provider_without_a_change_feed_sweeps_everything(self):
        set_setting(CATCH_UP_WATERMARK_KEY, "2026-01-01T00:00:00Z")
        provider = _Provider(feed=False)

        assert _subscriber(provider)._changed_slugs(LINKS) == (None, None)

    def test_a_failed_delta_sweeps_everything(self):
        """The important one: doubt must never read as 'nothing changed'."""
        set_setting(CATCH_UP_WATERMARK_KEY, "2026-01-01T00:00:00Z")
        provider = _Provider(boom=RuntimeError("network"))

        sub = _subscriber(provider)
        assert sub._changed_slugs(LINKS) == (None, None)
        assert "catch-up delta" in (sub.last_error or "")

    def test_changed_containers_map_back_to_their_projects(self):
        set_setting(CATCH_UP_WATERMARK_KEY, "2026-01-01T00:00:00Z")
        provider = _Provider(containers={"wp-a", "wp-c"})

        assert _subscriber(provider)._changed_slugs(LINKS)[0] == {"alpha", "gamma"}

    def test_nothing_changed_is_an_empty_set_not_none(self):
        """The whole point: the common answer costs one call and zero reads."""
        set_setting(CATCH_UP_WATERMARK_KEY, "2026-01-01T00:00:00Z")
        provider = _Provider(containers=set())

        assert _subscriber(provider)._changed_slugs(LINKS)[0] == set()

    def test_the_watermark_advances_on_success(self):
        set_setting(CATCH_UP_WATERMARK_KEY, "2026-01-01T00:00:00Z")
        provider = _Provider(containers={"wp-a"}, watermark="2026-06-06T06:06:06Z")

        asyncio.run(_subscriber(provider)._catch_up())

        assert get_setting(CATCH_UP_WATERMARK_KEY) == "2026-06-06T06:06:06Z"

    def test_the_watermark_is_returned_not_stored_by_the_delta_query(self):
        """Storing it there advanced past a board whose reconcile then failed."""
        set_setting(CATCH_UP_WATERMARK_KEY, "2026-01-01T00:00:00Z")
        provider = _Provider(containers={"wp-a"}, watermark="2026-06-06T06:06:06Z")

        slugs, watermark = _subscriber(provider)._changed_slugs(LINKS)

        assert slugs == {"alpha"}
        assert watermark == "2026-06-06T06:06:06Z"
        assert get_setting(CATCH_UP_WATERMARK_KEY) == "2026-01-01T00:00:00Z"

    def test_a_failed_reconcile_holds_the_watermark_back(self):
        """A board that could not be read must be read next time, not skipped."""
        set_setting(CATCH_UP_WATERMARK_KEY, "2026-01-01T00:00:00Z")
        provider = _Provider(containers={"wp-a"}, watermark="2026-06-06T06:06:06Z")
        sub = _subscriber(provider)
        sub._bridge.reconcile = lambda slug: (_ for _ in ()).throw(RuntimeError("board down"))

        asyncio.run(sub._catch_up())

        assert get_setting(CATCH_UP_WATERMARK_KEY) == "2026-01-01T00:00:00Z"
        assert "board down" in (sub.last_error or "")

    def test_the_catch_up_drains_the_outbox_before_reading(self):
        """What could not be sent while deaf goes out before the board is read."""
        set_setting(CATCH_UP_WATERMARK_KEY, "2026-01-01T00:00:00Z")
        provider = _Provider(containers=set())
        sub = _subscriber(provider)
        flushed = []
        sub._bridge.flush = lambda slug: flushed.append(slug)

        asyncio.run(sub._catch_up())

        assert flushed == ["alpha", "beta", "gamma"]

    def test_a_truncated_crawl_does_not_advance_the_watermark(self):
        """No watermark means pages ran out; skipping ahead would lose the rest."""
        set_setting(CATCH_UP_WATERMARK_KEY, "2026-01-01T00:00:00Z")
        provider = _Provider(containers={"wp-a"}, watermark=None)

        asyncio.run(_subscriber(provider)._catch_up())

        assert get_setting(CATCH_UP_WATERMARK_KEY) == "2026-01-01T00:00:00Z"

    def test_a_full_sweep_seeds_the_watermark(self):
        """So the run after a first sweep can be incremental."""
        sub = _subscriber(_Provider())
        sub._seed_watermark()

        assert get_setting(CATCH_UP_WATERMARK_KEY)

    def test_seeding_never_overwrites_an_existing_watermark(self):
        set_setting(CATCH_UP_WATERMARK_KEY, "2026-01-01T00:00:00Z")
        sub = _subscriber(_Provider())

        sub._seed_watermark()

        assert get_setting(CATCH_UP_WATERMARK_KEY) == "2026-01-01T00:00:00Z"
