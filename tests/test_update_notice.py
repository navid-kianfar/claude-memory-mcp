"""The update notice injected by the hook.

Two failure modes matter, and they pull in opposite directions: never telling
the user, and telling them on every single prompt. The second is the one that
does real damage - an injected block the user has learned to skip costs them the
binding rules too, which is the whole reason the hook exists.
"""

import json
import time

import pytest

from memory_mcp import enforcement
from memory_mcp.db.registry import set_setting
from memory_mcp.services import update_poller as poller


def _store(available=True, current="1.0.0", latest="2.0.0", source="github_releases"):
    set_setting(poller.STATUS_KEY, json.dumps({
        "current_version": current, "latest_version": latest,
        "update_available": available, "source": source, "warnings": [],
    }))


@pytest.fixture(autouse=True)
def _clean():
    for key in (poller.STATUS_KEY, enforcement.NOTIFIED_AT_KEY, poller.ERROR_KEY):
        set_setting(key, "")
    yield


class TestUpdateNotice:
    def test_nothing_is_said_when_there_is_no_update(self):
        _store(available=False)

        assert enforcement.update_intro() == ""
        assert enforcement.update_line() == ""

    def test_nothing_is_said_when_the_check_failed(self):
        """'unknown' is the absence of an answer. Never announce an update on it."""
        _store(available=True, source="unknown")

        assert enforcement.update_intro() == ""
        assert enforcement.update_line() == ""

    def test_nothing_is_said_when_no_check_has_ever_run(self):
        assert enforcement.update_intro() == ""
        assert enforcement.update_line() == ""

    def test_the_intro_names_both_versions(self):
        _store()

        text = enforcement.update_intro()

        assert "1.0.0" in text and "2.0.0" in text

    def test_the_intro_says_when_it_will_be_applied(self):
        """A notice that does not say 'end of turn' invites a mid-turn install."""
        _store()

        assert "END of a turn" in enforcement.update_intro()

    def test_the_per_turn_line_fires_once_then_goes_quiet(self):
        """The anti-nag rule. This injects on EVERY prompt."""
        _store()

        first = enforcement.update_line()
        second = enforcement.update_line()
        third = enforcement.update_line()

        assert first, "the first turn should mention it"
        assert second == "" and third == "", "and then stop"

    def test_it_speaks_again_after_the_interval(self):
        """A session running all day should still hear about a lunchtime release."""
        _store()
        enforcement.update_line()
        stale = time.time() - enforcement.NOTIFY_INTERVAL_SECONDS - 1
        set_setting(enforcement.NOTIFIED_AT_KEY, str(stale))

        assert enforcement.update_line()

    def test_the_per_turn_line_stays_one_short_line(self):
        _store()

        line = enforcement.update_line()

        assert "\n" not in line
        assert len(line) < 300, f"per-turn line is {len(line)} chars"

    def test_a_broken_cache_never_breaks_the_hook(self):
        """The hook carries the binding rules; a notice must not be able to kill it."""
        set_setting(poller.STATUS_KEY, "{ not json")

        assert enforcement.update_intro() == ""
        assert enforcement.update_line() == ""
