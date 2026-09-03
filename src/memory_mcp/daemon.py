"""HTTP daemon: one shared server for every Claude client + the management UI.

`memory-mcp serve` runs this. Both Claude Code (terminal CLI and the desktop
app) connect to the MCP endpoint at /mcp/; the management UI is served at /.

Running everything in a single process means one owner of every DuckDB file
(no lock contention between clients) and the embedding model loads only once
instead of on every client spawn.
"""

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount

from memory_mcp.config import settings
from memory_mcp.server import mcp
from memory_mcp.web import build_routes


def build_app() -> Starlette:
    """Compose the UI routes and the MCP streamable-http app into one ASGI app.

    Phase 2 seam - the asoode bridge's inbound half belongs here, and nowhere
    else. There is no scheduler, background runner or webhook receiver anywhere
    in this codebase today: the only periodic triggers are the Claude Code shell
    hooks and launchd's KeepAlive. A live task subscription therefore has to be
    a new asyncio task wrapped around this lifespan (plus an outbox flusher for
    the outbound half). It only ever talks HTTP/WebSocket, so unlike the project
    folder I/O in sync_cli.py it is NOT blocked by the launchd daemon's macOS
    TCC sandbox and can live in this process. It needs a Socket.IO client, which
    is not currently a dependency - httpx is, but it does not speak Socket.IO.
    """
    mcp_app = mcp.http_app(path="/mcp")
    routes = [*build_routes(), Mount("/", app=mcp_app)]
    return Starlette(routes=routes, lifespan=mcp_app.lifespan)


def serve() -> None:
    """Run the daemon (blocking)."""
    host = settings.daemon_host
    port = settings.daemon_port
    name = settings.daemon_hostname
    print("=" * 56)
    print("  Claude Memory MCP - daemon")
    print(f"  Mode         : {settings.mode}")
    print(f"  MCP endpoint : http://{name}:{port}/mcp/")
    print(f"  Management UI: http://{name}:{port}/")
    print(f"  (bound to {host}:{port})")
    # Safety guardrail: a non-loopback bind with no auth (local mode) exposes
    # every project unauthenticated to the network. Warn loudly.
    loopback = host in ("127.0.0.1", "::1", "localhost")
    if not loopback and not settings.server_mode:
        print("  " + "!" * 52)
        print("  WARNING: bound to a non-loopback address in LOCAL mode.")
        print("  There is NO authentication - anyone who can reach this")
        print("  port has full access. Set MEMORY_MCP_MODE=server (with")
        print("  users/tokens) before exposing the daemon to a network.")
        print("  " + "!" * 52)
    print("=" * 56)
    uvicorn.run(build_app(), host=host, port=port, log_level="info")


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
