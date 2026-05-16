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
    """Compose the UI routes and the MCP streamable-http app into one ASGI app."""
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
    print(f"  MCP endpoint : http://{name}:{port}/mcp/")
    print(f"  Management UI: http://{name}:{port}/")
    print(f"  (bound to {host}:{port})")
    print("=" * 56)
    uvicorn.run(build_app(), host=host, port=port, log_level="info")


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
