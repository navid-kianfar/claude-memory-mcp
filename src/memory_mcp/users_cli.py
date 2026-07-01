"""`memory-mcp user ...` - manage server-mode accounts and API tokens.

Used to bootstrap the first admin on a fresh server and to manage members
afterwards. Tokens are shown exactly once (only their hash is stored). These
commands operate on the registry directly, so run them on the server host with
the same MEMORY_MCP_DATA_DIR the daemon uses.
"""

import argparse
import sys

from memory_mcp.db import registry as R


def _print_row(u: dict) -> None:
    status = "active" if u["active"] else "inactive"
    print(f"  {u['username']:<20} {u['role']:<8} {status:<10} {u['id']}")


def cmd_create(args) -> None:
    role = "admin" if args.admin else "member"
    try:
        user, token = R.create_user(args.username, args.name, role)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Created {role} '{user['username']}'.")
    print()
    print("  API token (shown once - store it now):")
    print(f"    {token}")
    print()
    print("  Point a client machine at this server with:")
    print(f"    memory-mcp setup --client --url https://<server> --token {token}")


def cmd_list(_args) -> None:
    users = R.list_users()
    if not users:
        print("No users yet. Create the first admin:")
        print("  memory-mcp user create <name> --admin")
        return
    print(f"  {'USERNAME':<20} {'ROLE':<8} {'STATUS':<10} ID")
    for u in users:
        _print_row(u)


def _require_user(username: str) -> dict:
    u = R.get_user_by_username(username)
    if not u:
        print(f"error: no such user '{username}'", file=sys.stderr)
        sys.exit(1)
    return u


def cmd_deactivate(args) -> None:
    u = _require_user(args.username)
    R.set_user_active(u["id"], False)
    print(f"Deactivated '{args.username}'.")


def cmd_activate(args) -> None:
    u = _require_user(args.username)
    R.set_user_active(u["id"], True)
    print(f"Activated '{args.username}'.")


def cmd_rotate(args) -> None:
    u = _require_user(args.username)
    token = R.rotate_token(u["id"])
    print(f"New API token for '{args.username}' (shown once):")
    print(f"    {token}")


def main(argv) -> None:
    p = argparse.ArgumentParser(
        prog="memory-mcp user", description="Manage server-mode users"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a user and print a one-time API token")
    c.add_argument("username")
    c.add_argument("--admin", action="store_true", help="grant the admin role")
    c.add_argument("--name", default=None, help="display name")
    c.set_defaults(func=cmd_create)

    ls = sub.add_parser("list", help="list users")
    ls.set_defaults(func=cmd_list)

    d = sub.add_parser("deactivate", help="disable a user's login")
    d.add_argument("username")
    d.set_defaults(func=cmd_deactivate)

    a = sub.add_parser("activate", help="re-enable a user's login")
    a.add_argument("username")
    a.set_defaults(func=cmd_activate)

    r = sub.add_parser("rotate", help="issue a new API token for a user")
    r.add_argument("username")
    r.set_defaults(func=cmd_rotate)

    args = p.parse_args(argv)
    args.func(args)
