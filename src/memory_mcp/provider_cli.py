"""`memory-mcp provider ...` - platforms and their credentials.

  memory-mcp provider list                       what this build can talk to
  memory-mcp provider set-credential <name>      store one (prompted, not echoed)
  memory-mcp provider clear-credential <name>

Platform-neutral on purpose: `memory-mcp asoode set-pat` predates there being
more than one platform and stays for asoode, but a Trello key or a Jira token has
no business going through a command named after asoode.

The secret is read from a no-echo prompt or stdin, never argv - argv lands in
shell history and is visible to every other process in `ps`.
"""

import argparse
import sys

from memory_mcp.providers import available, get_provider
from memory_mcp.providers.credentials import (
    clear_credential,
    fingerprint,
    get_credential,
    set_credential,
)

USAGE = "Usage: memory-mcp provider [list|set-credential|clear-credential]"

# What a platform's secret actually is, so the prompt asks for the right thing.
_HINTS = {
    "asoode": "a personal access token (asoode_pat_…)",
    "trello": 'an API key and token together as "key:token" (trello.com/app-key)',
}


def _read_secret(name: str, from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.read().strip()
    import getpass

    hint = _HINTS.get(name, "the credential")
    return getpass.getpass(f"{name} - {hint} (input hidden): ").strip()


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "list"
    rest = argv[1:]

    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return

    if cmd == "list":
        print(f"{len(available())} provider(s):")
        for name in available():
            try:
                caps = get_provider(name).capabilities
                flags = ", ".join(filter(None, [
                    "idempotent-create" if caps.supports_external_ref else None,
                    "comments" if caps.supports_comments else None,
                    "time-tracking" if caps.supports_time_tracking else None,
                    "independent-state" if caps.supports_independent_state else None,
                ])) or "no optional capabilities"
            except Exception as e:  # noqa: BLE001
                flags = f"unavailable: {e}"
            stored = get_credential(name)
            fp = fingerprint(stored)
            cred = f"{fp['prefix']}…{fp['last4']}" if fp else "no credential"
            print(f"  {name:<10} [{cred}]  {flags}")
        return

    parser = argparse.ArgumentParser(prog=f"memory-mcp provider {cmd}")
    parser.add_argument("name", help="provider name, e.g. trello")
    parser.add_argument(
        "--account", default="",
        help="which account/site this credential is for (Jira needs one)",
    )
    if cmd == "set-credential":
        parser.add_argument("--stdin", action="store_true",
                            help="read the secret from stdin instead of prompting")
    ns = parser.parse_args(rest)

    if ns.name not in available():
        print(f"Unknown provider {ns.name!r}. Known: {', '.join(available())}",
              file=sys.stderr)
        sys.exit(1)

    if cmd == "set-credential":
        secret = _read_secret(ns.name, ns.stdin)
        if not secret:
            print("Nothing given; nothing stored.", file=sys.stderr)
            sys.exit(1)
        set_credential(ns.name, secret, ns.account)
        fp = fingerprint(secret)
        where = f" for {ns.account}" if ns.account else ""
        print(f"Stored {ns.name} credential {fp['prefix']}…{fp['last4']}{where}.")
        print("Every project on this machine can use it - no per-project setup.")
    elif cmd == "clear-credential":
        clear_credential(ns.name, ns.account)
        print(f"Cleared the stored {ns.name} credential.")
    else:
        print(f"Unknown provider command: {cmd}\n{USAGE}", file=sys.stderr)
        sys.exit(1)
