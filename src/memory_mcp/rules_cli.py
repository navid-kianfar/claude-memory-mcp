"""`memory-mcp rules` - print the current project's rules for hook injection.

  memory-mcp rules --cwd /path/to/project          -> print the rules block
  memory-mcp rules --cwd /path/to/project --intro  -> print the session-start nudge

Prints nothing and exits 0 when the directory is not a registered memory
project, so the hook can be installed globally without noise in other repos.
This works offline (no daemon needed); the daemon also exposes the same text
at /api/hook/rules for a faster, no-spawn hook path.
"""

import argparse


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="memory-mcp rules")
    parser.add_argument("--cwd", default=None, help="directory to resolve the project from")
    parser.add_argument("--slug", default=None, help="explicit project slug (skips detection)")
    parser.add_argument("--intro", action="store_true", help="print the session-start nudge")
    args = parser.parse_args(argv)

    try:
        from memory_mcp.context import detect_project_from_cwd
        from memory_mcp.enforcement import format_intro, rules_text_for_project

        slug = args.slug or detect_project_from_cwd(args.cwd)
        if not slug:
            return
        text = format_intro(slug) if args.intro else rules_text_for_project(slug)
        if text:
            print(text)
    except Exception:  # noqa: BLE001 - never break the prompt flow
        return
