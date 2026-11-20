"""`memory-mcp rules`: the daemon's text when it is up, local text only offline.

The daemon holds each project's DuckDB lock, so a local read while it runs fails
on the lock and the intro's counts all read 0. These tests pin which source the
command prints from, not the wording of the intro itself.
"""

import pytest

from memory_mcp import daemon_client, rules_cli
from memory_mcp.daemon_client import DaemonError, DaemonUnavailable

SLUG = "rules-cli-test"
FOLDER = "/work/rules-cli-test"


@pytest.fixture
def world(monkeypatch):
    """A project at FOLDER that detection maps back to SLUG, and spies on both
    sources of text."""
    import memory_mcp.context as context
    import memory_mcp.enforcement as enforcement
    from memory_mcp.container import container

    calls: dict[str, list] = {"daemon": [], "local": []}

    class _Project:
        project_path = FOLDER

    monkeypatch.setattr(
        context, "detect_project_from_cwd",
        lambda cwd: SLUG if cwd and cwd.startswith(FOLDER) else None,
    )
    monkeypatch.setattr(
        container.project_repo, "get", lambda slug: _Project() if slug == SLUG else None,
    )

    def _intro(slug, cwd=None):
        calls["local"].append(("intro", slug, cwd))
        return "LOCAL INTRO"

    def _rules(slug, cwd=None):
        calls["local"].append(("rules", slug, cwd))
        return "LOCAL RULES"

    monkeypatch.setattr(enforcement, "format_intro", _intro)
    monkeypatch.setattr(enforcement, "rules_text_for_project", _rules)

    def answer_with(result):
        def _call_text(path, timeout=10.0):
            calls["daemon"].append(path)
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(daemon_client, "call_text", _call_text)

    def daemon_running(value: bool):
        monkeypatch.setattr(daemon_client, "is_running", lambda timeout=2.0: value)

    calls["answer_with"] = answer_with
    calls["daemon_running"] = daemon_running
    return calls


def test_with_the_daemon_up_the_intro_comes_from_it(world, capsys):
    world["answer_with"]("DAEMON INTRO: 3 changes are queued\n")

    rules_cli.main(["--slug", SLUG, "--intro"])

    assert capsys.readouterr().out == "DAEMON INTRO: 3 changes are queued\n"
    assert world["local"] == []
    [path] = world["daemon"]
    assert path.startswith("/api/hook/rules?")
    assert "mode=intro" in path
    assert "cwd=%2Fwork%2Frules-cli-test" in path, "--slug resolves to the project's folder"


def test_the_rules_block_also_comes_from_the_daemon(world, capsys):
    world["answer_with"]("DAEMON RULES")

    rules_cli.main(["--cwd", f"{FOLDER}/src"])

    assert capsys.readouterr().out == "DAEMON RULES\n"
    assert "mode=rules" in world["daemon"][0]
    assert world["local"] == []


def test_with_no_daemon_it_builds_the_text_locally(world, capsys):
    world["answer_with"](DaemonUnavailable("connection refused"))

    rules_cli.main(["--slug", SLUG, "--intro"])

    assert capsys.readouterr().out == "LOCAL INTRO\n"
    assert world["local"] == [("intro", SLUG, None)]


def test_a_daemon_refusal_prints_nothing_rather_than_reading_locally(world, capsys):
    world["answer_with"](DaemonError("daemon returned 500"))

    rules_cli.main(["--slug", SLUG, "--intro"])

    assert capsys.readouterr().out == ""
    assert world["local"] == []


def test_a_slug_the_daemon_cannot_reach_prints_nothing_while_it_runs(
    world, capsys, monkeypatch,
):
    """No folder maps to the slug, and the running daemon holds the lock."""
    world["daemon_running"](True)

    rules_cli.main(["--slug", "elsewhere", "--intro"])

    assert capsys.readouterr().out == ""
    assert world["local"] == [] and world["daemon"] == []


def test_a_slug_the_daemon_cannot_reach_is_built_locally_when_it_is_down(world, capsys):
    world["daemon_running"](False)

    rules_cli.main(["--slug", "elsewhere", "--intro"])

    assert capsys.readouterr().out == "LOCAL INTRO\n"


def test_a_folder_that_is_another_project_is_never_sent_to_the_daemon(world, capsys):
    """--slug wins; a --cwd that detects as something else must not be asked
    about, or the daemon would answer for the wrong project."""
    world["daemon_running"](False)

    rules_cli.main(["--slug", SLUG, "--cwd", "/somewhere/else", "--intro"])

    assert world["daemon"] == []
    assert capsys.readouterr().out == "LOCAL INTRO\n"


def test_outside_a_project_it_prints_nothing(world, capsys):
    rules_cli.main(["--cwd", "/not/a/project"])

    assert capsys.readouterr().out == ""
    assert world["daemon"] == [] and world["local"] == []
