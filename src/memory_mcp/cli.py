"""Unified `memory-mcp` entrypoint.

  memory-mcp            -> run the MCP server over stdio (legacy / fallback)
  memory-mcp stdio      -> same, explicit
  memory-mcp serve      -> run the shared HTTP daemon (MCP + management UI)
  memory-mcp rules      -> print the current project's rules (used by hooks)
  memory-mcp setup      -> run interactive setup
"""

import sys

USAGE = "Usage: memory-mcp [stdio|serve|rules|setup]"


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
    elif cmd == "setup":
        from memory_mcp.setup import main as setup_main
        setup_main()
    elif cmd in ("-h", "--help", "help"):
        print(USAGE)
    else:
        print(f"Unknown command: {cmd}\n{USAGE}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
