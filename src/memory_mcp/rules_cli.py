"""`memory-mcp rules` - print the current project's rules for hook injection.

  memory-mcp rules --cwd /path/to/project          -> print the rules block
  memory-mcp rules --cwd /path/to/project --intro  -> print the session-start nudge

Prints nothing and exits 0 when the directory is not a registered memory
project, so the hook can be installed globally without noise in other repos.

When the daemon is running, the text comes from it (/api/hook/rules): the daemon
holds the project's DuckDB lock, so a direct read here would fail on the lock and
every count in the intro - queued board changes, open tasks, pending imports -
would read as 0. Only when no daemon answers is the text built locally, which is
what makes the command work offline.
"""

import argparse
from urllib.parse import urlencode

_HOOK_RULES_PATH = "/api/hook/rules"
_MODE_INTRO = "intro"
_MODE_RULES = "rules"


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="memory-mcp rules")
    parser.add_argument("--cwd", default=None, help="directory to resolve the project from")
    parser.add_argument("--slug", default=None, help="explicit project slug (skips detection)")
    parser.add_argument("--intro", action="store_true", help="print the session-start nudge")
    args = parser.parse_args(argv)

    try:
        text = _text_for(args.slug, args.cwd, intro=args.intro)
    except Exception:  # noqa: BLE001 - never break the prompt flow
        return
    if text:
        print(text)


def _text_for(slug_arg: str | None, cwd: str | None, *, intro: bool) -> str:
    from memory_mcp.context import detect_project_from_cwd
    from memory_mcp.daemon_client import DaemonError, DaemonUnavailable, is_running

    slug = slug_arg or detect_project_from_cwd(cwd)
    if not slug:
        return ""
    mode = _MODE_INTRO if intro else _MODE_RULES
    folder = _daemon_folder(slug, cwd)
    if folder is None:
        # No folder the daemon would resolve to this project. If the daemon is
        # up it holds the lock, and a local read would print zeros and missing
        # rules as if they were true; saying nothing is the honest answer.
        return "" if is_running() else _local_text(slug, cwd, mode)
    try:
        return _from_daemon(folder, mode)
    except DaemonUnavailable:
        # Nothing listening: build it here, as offline always has.
        return _local_text(slug, cwd, mode)
    except DaemonError:
        # The daemon answered and refused. It holds the lock, so a local read
        # would only fail on it; print nothing rather than wrong numbers.
        return ""


def _daemon_folder(slug: str, cwd: str | None) -> str | None:
    """A folder the daemon resolves to exactly this project, or None.

    The hook route resolves a project from a folder, not a slug. With `--slug`
    and no `--cwd`, the project's registered path stands in; it is used only
    when detection maps it back to the same slug, so the daemon can never answer
    for a different project than the one asked about.
    """
    from memory_mcp.container import container
    from memory_mcp.context import detect_project_from_cwd

    folder = cwd
    if not folder:
        project = container.project_repo.get(slug)
        folder = project.project_path if project else None
    if not folder:
        return None
    resolved = detect_project_from_cwd(folder)
    return folder if resolved == slug else None


def _from_daemon(folder: str, mode: str) -> str:
    from memory_mcp.daemon_client import call_text

    query = urlencode({"cwd": folder, "mode": mode})
    path = f"{_HOOK_RULES_PATH}?{query}"
    return call_text(path).strip()


def _local_text(slug: str, cwd: str | None, mode: str) -> str:
    # The cwd names this repo's own specialists in the team text. There is no
    # Claude session id on this path, so the per-turn line never escalates.
    from memory_mcp.enforcement import format_intro, rules_text_for_project

    if mode == _MODE_INTRO:
        return format_intro(slug, cwd=cwd)
    return rules_text_for_project(slug, cwd=cwd)
