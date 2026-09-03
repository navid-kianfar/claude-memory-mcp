"""Unified `memory-mcp` entrypoint.

  memory-mcp            -> run the MCP server over stdio (legacy / fallback)
  memory-mcp stdio      -> same, explicit
  memory-mcp serve      -> run the shared HTTP daemon (MCP + management UI)
  memory-mcp rules      -> print the current project's rules (used by hooks)
  memory-mcp sync ...   -> export/import the project memory snapshot (hooks)
  memory-mcp setup      -> run interactive setup
  memory-mcp update     -> rebuild the runtime from source + reload the daemon
  memory-mcp user ...   -> manage server-mode users (create/list/rotate tokens)
  memory-mcp bind ...   -> route a project to a local or remote backend
  memory-mcp asoode ... -> asoode endpoints + the machine-wide PAT
"""

import sys

USAGE = (
    "Usage: memory-mcp "
    "[stdio|serve|rules|sync|setup|update|user|bind|asoode]"
)


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "stdio"

    if cmd == "stdio":
        from memory_mcp.server import main as stdio_main
        stdio_main()
    elif cmd == "serve":
        from memory_mcp.daemon import serve
        serve()
    elif cmd == "rules":
        from memory_mcp.rules_cli import main as rules_main
        rules_main(args[1:])
    elif cmd == "sync":
        from memory_mcp.sync_cli import main as sync_main
        sync_main(args[1:])
    elif cmd == "setup":
        rest = args[1:]
        if "--client" in rest:
            import argparse

            p = argparse.ArgumentParser(prog="memory-mcp setup --client")
            p.add_argument("--client", action="store_true")
            p.add_argument("--url", required=True, help="remote server base URL")
            p.add_argument("--token", default=None, help="API token for the server")
            ns = p.parse_args(rest)
            from memory_mcp.setup import main_client
            main_client(ns.url, ns.token)
        else:
            from memory_mcp.setup import main as setup_main
            setup_main()
    elif cmd == "update":
        from memory_mcp.setup import run_update
        run_update()
    elif cmd == "user":
        from memory_mcp.users_cli import main as users_main
        users_main(args[1:])
    elif cmd == "bind":
        _bind(args[1:])
    elif cmd == "asoode":
        from memory_mcp.asoode_cli import main as asoode_main
        asoode_main(args[1:])
    elif cmd in ("-h", "--help", "help"):
        print(USAGE)
    else:
        print(f"Unknown command: {cmd}\n{USAGE}", file=sys.stderr)
        sys.exit(1)


def _bind(argv) -> None:
    """`memory-mcp bind <slug> --remote <url> [--token T]` | `--local`."""
    import argparse

    p = argparse.ArgumentParser(prog="memory-mcp bind")
    p.add_argument("slug")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--remote", metavar="URL", help="route to an org server")
    group.add_argument("--local", action="store_true", help="keep private + local")
    p.add_argument("--token", default=None, help="API token for the remote server")
    ns = p.parse_args(argv)

    from memory_mcp.container import container

    if ns.local:
        info = container.project_service.bind_backend(ns.slug, "local")
        print(f"'{ns.slug}' is now LOCAL (private, stored on this machine).")
    else:
        info = container.project_service.bind_backend(
            ns.slug, "remote", ns.remote, ns.token
        )
        print(f"'{ns.slug}' is now REMOTE -> {info.remote_url}")


if __name__ == "__main__":
    main()
