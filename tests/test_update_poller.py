"""The update poller: detects a newer version, never installs one.

The test that carries the weight is the failed-check one. UpdateService.check()
reports update_available=False both when there is genuinely no update and when
the check could not run (source="unknown"). Conflating those means a network
outage reads as "you are current" - the one outcome that makes an update checker
worse than none.
"""

import json

import pytest

from memory_mcp.db.registry import get_setting, set_setting
from memory_mcp.services import update_poller as mod
from memory_mcp.services.update_poller import UpdatePoller


class _Service:
    """Stands in for UpdateService. Returns whatever it is handed."""

    def __init__(self, *results):
        self._results = list(results)
        self.calls = 0

    def check(self):
        self.calls += 1
        result = self._results[min(self.calls - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


def _ok(available=True, latest="9.9.9"):
    return {
        "current_version": "1.0.0", "source": "github_releases",
        "update_available": available, "latest_version": latest, "warnings": [],
    }


def _failed(warning="Could not check for updates"):
    return {
        "current_version": "1.0.0", "source": "unknown",
        "update_available": False, "warnings": [warning],
    }


@pytest.fixture(autouse=True)
def _clean():
    for key in (mod.STATUS_KEY, mod.CHECKED_AT_KEY, mod.ERROR_KEY):
        set_setting(key, "")
    yield


class TestUpdatePoller:
    def test_a_successful_check_is_recorded(self):
        UpdatePoller(_Service(_ok())).check_once()

        assert mod.update_available() is True
        assert mod.read_status()["latest_version"] == "9.9.9"

    def test_no_update_is_recorded_as_no_update(self):
        UpdatePoller(_Service(_ok(available=False))).check_once()

        assert mod.update_available() is False
        assert mod.read_status() is not None, "a successful check is still an answer"

    def test_a_failed_check_never_claims_you_are_up_to_date(self):
        """source='unknown' is the absence of an answer, not a negative one."""
        UpdatePoller(_Service(_failed())).check_once()

        assert mod.update_available() is False
        assert mod.read_status() is None, "a failed check must not be stored as status"
        assert get_setting(mod.ERROR_KEY)

    def test_a_failed_check_does_not_clobber_a_known_update(self):
        """The case that would silently hide a real update behind an outage."""
        poller = UpdatePoller(_Service(_ok(), _failed()))
        poller.check_once()
        assert mod.update_available() is True

        poller.check_once()  # GitHub goes down

        assert mod.update_available() is True, "last known answer must survive"
        assert mod.read_status()["latest_version"] == "9.9.9"

    def test_an_exception_is_swallowed_and_recorded(self):
        """A GitHub outage must never take the daemon down."""
        UpdatePoller(_Service(RuntimeError("boom"))).check_once()

        assert "RuntimeError" in get_setting(mod.ERROR_KEY)
        assert mod.update_available() is False

    def test_rate_limiting_is_reported_so_the_caller_backs_off(self):
        limited = UpdatePoller(_Service(_failed("API rate limit exceeded"))).check_once()
        normal = UpdatePoller(_Service(_failed("no network"))).check_once()

        assert limited is True
        assert normal is False

    def test_every_check_records_when_it_ran(self):
        """Even a failed one - otherwise 'never checked' and 'check broken' look alike."""
        UpdatePoller(_Service(_failed())).check_once()

        assert float(get_setting(mod.CHECKED_AT_KEY)) > 0

    def test_corrupt_stored_status_reads_as_no_status(self):
        set_setting(mod.STATUS_KEY, "{ not json")

        assert mod.read_status() is None
        assert mod.update_available() is False

    def test_the_poller_never_installs_anything(self):
        """It is the detector. Applying an update drops every live MCP session.

        Checks what the module IMPORTS, not what it says: the docstring explains
        the git pull and the reinstall it deliberately does not do, so a naive
        substring search matches its own explanation.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(mod))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        for forbidden in ("subprocess", "os", "shutil"):
            assert forbidden not in imported, (
                f"the poller must not be able to apply an update (imports {forbidden!r})"
            )

    def test_the_interval_is_ten_minutes_and_the_first_check_is_delayed(self):
        assert mod.POLL_INTERVAL_SECONDS == 600.0
        assert mod.INITIAL_DELAY_SECONDS > 0, (
            "a launchd crash loop would otherwise be a request storm"
        )


class TestNetworkOnlyStrategy:
    """The commit comparison that works inside the daemon's sandbox.

    It exists because the other two strategies both fail there, verified live on
    2026-09-04: this repo publishes no releases (/releases/latest answers 404),
    and the local git comparison needs to read a repo the daemon cannot open when
    it sits under a TCC-protected folder. Without this the poller had no working
    strategy at all.
    """

    @pytest.fixture
    def service(self):
        from memory_mcp.services.update_service import UpdateService

        return UpdateService()

    def test_no_recorded_install_commit_means_no_answer(self, service, monkeypatch):
        """None, not False - 'cannot tell' must fall through, not read as current."""
        set_setting("install:commit", "")

        assert service._check_via_github_commits() is None

    def test_an_unreachable_api_means_no_answer(self, service, monkeypatch):
        from memory_mcp.services import update_service as us

        set_setting("install:commit", "a" * 40)
        monkeypatch.setattr(us, "_fetch_github_json", lambda path: None)

        assert service._check_via_github_commits() is None

    def test_the_same_commit_is_up_to_date(self, service, monkeypatch):
        from memory_mcp.services import update_service as us

        sha = "b" * 40
        set_setting("install:commit", sha)
        monkeypatch.setattr(us, "_fetch_github_json", lambda path: {"sha": sha})

        assert service._check_via_github_commits()["update_available"] is False

    def test_a_newer_commit_reports_how_far_behind(self, service, monkeypatch):
        from memory_mcp.services import update_service as us

        set_setting("install:commit", "c" * 40)

        def fake(path):
            if path.startswith("/commits/"):
                return {"sha": "d" * 40}
            return {
                "ahead_by": 4,
                "commits": [{"commit": {"message": "fix a thing\n\nbody"}}],
            }

        monkeypatch.setattr(us, "_fetch_github_json", fake)

        result = service._check_via_github_commits()

        assert result["update_available"] is True
        assert result["commits_behind"] == 4
        assert result["recent_commits"] == ["fix a thing"]

    def test_a_missing_compare_still_reports_the_update(self, service, monkeypatch):
        """Best effort: how far behind is a nicety, that there IS an update is not."""
        from memory_mcp.services import update_service as us

        set_setting("install:commit", "e" * 40)
        monkeypatch.setattr(
            us, "_fetch_github_json",
            lambda path: {"sha": "f" * 40} if path.startswith("/commits/") else None,
        )

        assert service._check_via_github_commits()["update_available"] is True
