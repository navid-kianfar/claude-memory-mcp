"""HTTP daemon: one shared server for every Claude client + the management UI.

`memory-mcp serve` runs this. Both Claude Code (terminal CLI and the desktop
app) connect to the MCP endpoint at /mcp/; the management UI is served at /.

Running everything in a single process means one owner of every DuckDB file
(no lock contention between clients) and the embedding model loads only once
instead of on every client spawn.
"""

import contextlib
import logging

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount

from memory_mcp.config import settings
from memory_mcp.server import mcp
from memory_mcp.web import build_routes

log = logging.getLogger(__name__)


def _socket_credentials():
    """(socket_url, ticket), or None when asoode is not configured or reachable.

    A raw PAT does NOT work on the socket: the gateway keeps no database and
    verifies signed JWTs only, so the PAT has to be exchanged for a short-lived
    ticket over REST first (POST /account/socket-token). The ticket expires,
    which is why this is called on every connect rather than once - a reconnect
    after a long outage needs a fresh one.
    """
    from memory_mcp.asoode import get_endpoints, get_pat
    from memory_mcp.asoode_client import AsoodeClient

    endpoints = get_endpoints()
    if not get_pat(endpoints.api_url):
        return None
    try:
        ticket = AsoodeClient.from_settings().socket_ticket().get("token")
    except Exception:  # noqa: BLE001 - unreachable REST means no socket either
        return None
    return (endpoints.socket_url, ticket) if ticket else None


def _all_links() -> list:
    """Every project link on this machine - what a board event is matched against."""
    from memory_mcp.db.registry import registry_conn

    with registry_conn() as conn:
        rows = conn.execute(
            "SELECT slug, remote_work_package_id, provider FROM project_links "
            "WHERE active = 1"
        ).fetchall()
    return [dict(r) for r in rows]


def build_app() -> Starlette:
    """Compose the UI routes and the MCP streamable-http app into one ASGI app.

    The lifespan also owns the asoode bridge's INBOUND half: a Socket.IO
    subscription that reconciles a project when its board changes. It lives here
    because there is no other scheduler or background runner in this codebase -
    the only periodic triggers are the Claude Code shell hooks and launchd's
    KeepAlive - and because it only speaks HTTP/WebSocket, so unlike the project
    folder I/O in sync_cli.py it is not blocked by the daemon's macOS TCC
    sandbox.

    It is an OPTIMISATION, not a correctness requirement: reconcile already runs
    after every mirror, so a dropped socket or a revoked token degrades to the
    behaviour that existed before this. Nothing it does may raise into the app.
    """
    mcp_app = mcp.http_app(path="/mcp")
    routes = [*build_routes(), Mount("/", app=mcp_app)]

    @contextlib.asynccontextmanager
    async def lifespan(app):
        from memory_mcp.container import container
        from memory_mcp.services.socket_subscriber import SocketSubscriber
        from memory_mcp.services.update_poller import UpdatePoller

        subscriber = SocketSubscriber(
            container.task_bridge, _all_links, _socket_credentials,
        )
        app.state.asoode_socket = subscriber
        try:
            started = subscriber.start()
            if started:
                log.info("asoode socket subscription started")
        except Exception as e:  # noqa: BLE001 - never block the daemon starting
            log.warning("asoode socket subscription did not start: %s", e)

        # Detects a newer version; never installs one. Applying an update
        # reloads this daemon and drops every live MCP connection, and the
        # daemon cannot reach a repo under a TCC-protected folder anyway - the
        # Stop hook does the applying, in the user's own context.
        poller = UpdatePoller(container.update_service)
        app.state.update_poller = poller
        try:
            poller.start()
        except Exception as e:  # noqa: BLE001 - never block the daemon starting
            log.warning("update poller did not start: %s", e)

        # Drain what the previous process left behind, then keep looking. A
        # mutation nudges its own flush, but a row stranded by an outage, a
        # restart mid-flush or a CLI that exited early has nobody to nudge it.
        try:
            container.start_outbox_sweeper()
        except Exception as e:  # noqa: BLE001 - never block the daemon starting
            log.warning("outbox sweeper did not start: %s", e)

        async with mcp_app.lifespan(app):
            yield

        with contextlib.suppress(Exception):
            container.stop_outbox_sweeper()
        with contextlib.suppress(Exception):
            await subscriber.stop()
        with contextlib.suppress(Exception):
            await poller.stop()

    return Starlette(routes=routes, lifespan=lifespan)


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
