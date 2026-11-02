"""Two concurrent sessions cannot contaminate each other: nothing is shared.

The incident this file used to test the FIX of (2026-09-04, seven tasks into the
wrong project) was repaired twice - first by keying the active project on the
MCP session, then, on 2026-11-03, by REMOVING the active project entirely. A
per-session map still trusted every caller to have one and every brief to name
it; the mechanism that cannot leak is the one that does not exist. What remains
session-keyed is only the memory-session mapping (clocks and claims), which
carries no project state.

These tests pin that: explicit projects stay independent because resolution
holds no state a second session could read.
"""

import threading

import pytest

from memory_mcp.context import resolve_project
from memory_mcp.server import _resolve


class TestExplicitProjectsAreIndependent:
    def test_two_threads_resolving_different_projects_never_cross(self):
        """The strongest form of the old guarantee, now trivially true: there
        is no shared state, so even truly concurrent callers cannot see each
        other's project."""
        results: dict[int, str | None] = {}
        errors: list[str] = []

        def work(slug):
            try:
                for _ in range(200):
                    got = _resolve(slug)
                    if got != slug:
                        errors.append(f"{slug} resolved to {got}")
                        return
                    results[id(threading.current_thread())] = got
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))

        threads = [
            threading.Thread(target=work, args=(f"proj-{i}",)) for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        assert len(results) == 4

    def test_a_missing_project_is_an_error_not_the_other_session_choice(self):
        with pytest.raises(ValueError, match="project=<slug>"):
            _resolve(None)

    def test_resolution_is_pure(self):
        """Same inputs, same answer, no matter what came before - the property
        a process-global broke."""
        assert resolve_project("alpha") == resolve_project("alpha") == "alpha"
        assert resolve_project(None) is None
        assert resolve_project("beta") == "beta"
        assert resolve_project(None) is None
