"""`memory-mcp asoode ...` - point this machine at an asoode and store the PAT.

  memory-mcp asoode status                 what is configured right now
  memory-mcp asoode set-pat [--stdin]      store the token (prompted, not echoed)
  memory-mcp asoode clear-pat              forget it
  memory-mcp asoode set-url --api URL ...  on-premise overrides
  memory-mcp asoode reset-url              back to the hosted defaults
  memory-mcp asoode check                  prove the PAT reaches the server
  memory-mcp asoode link <slug>            create/find the project + board
  memory-mcp asoode push <slug>            mirror local tasks onto that board
  memory-mcp asoode open [slug]            open the board, already signed in

The token is read from a no-echo prompt, or from stdin with --stdin. It is
deliberately NOT a command-line argument: argv lands in shell history and is
visible to every other process in `ps`.
"""

import argparse
import sys

from memory_mcp.asoode import (
    AsoodeConfigError,
    redacted,
    signin_url,
    clear_pat,
    get_endpoints,
    reset_endpoints,
    set_endpoints,
    set_pat,
    status,
)

from memory_mcp.asoode_client import AsoodeError

# Both are user-facing misconfiguration, not bugs: print the message, not a
# traceback.
_CLI_ERRORS = (AsoodeConfigError, AsoodeError)

USAGE = (
    "Usage: memory-mcp asoode "
    "[status|set-pat|clear-pat|set-url|reset-url|check|link|push|open]"
)


def _print_status() -> None:
    st = status()
    ep = st["endpoints"]
    kind = "hosted defaults" if ep["is_default"] else "on-premise / overridden"
    print(f"asoode endpoints ({kind}):")
    for field in ("app_url", "api_url", "socket_url"):
        source = ep["sources"][field]
        suffix = "" if source == "default" else f"   [{source}]"
        print(f"  {field:<11} {ep[field]}{suffix}")
    if st["pat_configured"]:
        fp = st["pat"]
        print(
            f"\nPAT: stored for {ep['api_url']} "
            f"({fp['prefix']}…{fp['last4']}, {fp['length']} chars)"
        )
        print("     Shared by every project on this machine.")
    else:
        print("\nPAT: not set")
    for warning in st["warnings"]:
        print(f"\n! {warning}")


def _read_token(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.read().strip()
    import getpass

    return getpass.getpass("asoode PAT (input hidden): ").strip()


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "status"
    rest = argv[1:]

    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return

    try:
        if cmd == "status":
            _print_status()

        elif cmd == "set-pat":
            p = argparse.ArgumentParser(prog="memory-mcp asoode set-pat")
            p.add_argument(
                "--stdin", action="store_true",
                help="read the token from stdin instead of prompting",
            )
            p.add_argument(
                "--api-url", default=None,
                help="store it for this API base instead of the configured one",
            )
            ns = p.parse_args(rest)
            token = _read_token(ns.stdin)
            if not token:
                print("No token given; nothing stored.", file=sys.stderr)
                sys.exit(1)
            result = set_pat(token, ns.api_url)
            fp = result["fingerprint"]
            print(
                f"Stored PAT {fp['prefix']}…{fp['last4']} for {result['api_url']}.\n"
                "Every project on this machine now uses it - no per-project setup."
            )

        elif cmd == "clear-pat":
            p = argparse.ArgumentParser(prog="memory-mcp asoode clear-pat")
            p.add_argument("--api-url", default=None)
            ns = p.parse_args(rest)
            result = clear_pat(ns.api_url)
            print(f"Cleared the stored PAT for {result['api_url']}.")

        elif cmd == "set-url":
            p = argparse.ArgumentParser(prog="memory-mcp asoode set-url")
            p.add_argument("--api", default=None, metavar="URL")
            p.add_argument("--app", default=None, metavar="URL")
            p.add_argument("--socket", default=None, metavar="URL")
            p.add_argument(
                "--no-derive", action="store_true",
                help="do not infer app/socket from an api.<host> URL",
            )
            ns = p.parse_args(rest)
            before = get_endpoints().api_url
            set_endpoints(
                api_url=ns.api, app_url=ns.app, socket_url=ns.socket,
                derive=not ns.no_derive,
            )
            _print_status()
            after = get_endpoints().api_url
            if after != before:
                print(
                    f"\nNote: the API base changed ({before} -> {after}). The PAT is "
                    "stored per server, so set one for the new base with "
                    "`memory-mcp asoode set-pat`."
                )

        elif cmd == "reset-url":
            reset_endpoints()
            _print_status()

        elif cmd == "check":
            from memory_mcp.asoode_client import AsoodeClient

            client = AsoodeClient.from_settings()
            projects = client.list_projects()
            print(
                f"Reached {client.base_url} and the PAT was accepted. "
                f"{len(projects)} project(s) visible:"
            )
            for project in projects:
                print(f"  {project.get('id')}  {project.get('title')}")

        elif cmd == "link":
            p = argparse.ArgumentParser(prog="memory-mcp asoode link")
            p.add_argument("slug")
            p.add_argument("--project-title", default=None)
            p.add_argument("--board-title", default=None)
            p.add_argument(
                "--asoode-project-id", default=None,
                help="put the board in this existing asoode project",
            )
            ns = p.parse_args(rest)
            from memory_mcp.container import container

            result = container.asoode_bridge.bootstrap(
                ns.slug, project_title=ns.project_title,
                board_title=ns.board_title, reuse_project_id=ns.asoode_project_id,
            )
            print(f"project      {result['project']['id']}  {result['project']['title']}")
            print(f"work package {result['work_package']['id']}  {result['work_package']['title']}")
            print("lists        " + ", ".join(item["title"] for item in result["lists"]))
            print(f"open it at   {result['url']}")

        elif cmd == "push":
            p = argparse.ArgumentParser(prog="memory-mcp asoode push")
            p.add_argument("slug")
            p.add_argument(
                "--skip-done", action="store_true",
                help="leave finished tasks out of the mirror",
            )
            ns = p.parse_args(rest)
            from memory_mcp.container import container

            result = container.asoode_bridge.push(
                ns.slug, include_done=not ns.skip_done,
            )
            counts = result["counts"]
            print(
                f"{counts['pushed']} task(s) mirrored, {counts['failed']} failed "
                f"(of {counts['considered']} considered)."
            )
            for failure in result["failed"]:
                print(f"  FAILED {failure['title']}: {failure['error']}", file=sys.stderr)

        elif cmd == "open":
            p = argparse.ArgumentParser(prog="memory-mcp asoode open")
            p.add_argument(
                "slug", nargs="?", default=None,
                help="open this project's bound board (default: the app itself)",
            )
            p.add_argument(
                "--path", default=None,
                help="an app-relative path to land on, e.g. /projects/<id>",
            )
            p.add_argument(
                "--print", dest="print_only", action="store_true",
                help="print the link instead of opening it. It CONTAINS THE TOKEN - "
                     "pipe it to a browser, never into a log or a chat",
            )
            ns = p.parse_args(rest)

            return_path = ns.path
            if return_path is None and ns.slug:
                from memory_mcp.db.registry import get_default_project_link

                link = get_default_project_link(ns.slug)
                if link is None:
                    print(
                        f"'{ns.slug}' is not linked to an asoode board - run "
                        f"`memory-mcp asoode link {ns.slug}` first.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                return_path = f"/projects/{link['remote_project_id']}"

            url = signin_url(return_path or "")
            if ns.print_only:
                print(url)
            else:
                import subprocess

                opener = {"darwin": "open", "win32": "start"}.get(sys.platform, "xdg-open")
                subprocess.run([opener, url], check=False)
                # Redacted on purpose: the real link carries the PAT.
                print(f"Opened {redacted(url)}")

        else:
            print(f"Unknown asoode command: {cmd}\n{USAGE}", file=sys.stderr)
            sys.exit(1)

    except _CLI_ERRORS as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
