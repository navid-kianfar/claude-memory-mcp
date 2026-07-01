"""Bearer-token authentication for the MCP endpoint (server mode only).

FastMCP calls `verify_token` for every MCP request when `mcp.auth` is set. We
resolve the token against the registry `users` table and hand back an
`AccessToken` whose `claims` carry the caller's identity, which tools read via
`fastmcp.server.dependencies.get_access_token()`. In local mode `mcp.auth` stays
None and none of this runs, so the `/mcp` endpoint is mounted exactly as before.
"""

from anyio import to_thread
from fastmcp.server.auth import AccessToken, TokenVerifier

from memory_mcp.db.registry import authenticate_token


class RegistryTokenVerifier(TokenVerifier):
    """Verify bearer tokens against the SQLite registry's users table."""

    async def verify_token(self, token: str) -> AccessToken | None:
        # authenticate_token is blocking SQLite; keep the event loop free.
        user = await to_thread.run_sync(authenticate_token, token)
        if not user:
            return None
        role = user.get("role", "member")
        return AccessToken(
            token=token,
            client_id=user["id"],
            scopes=[role],
            claims={
                "user_id": user["id"],
                "username": user["username"],
                "role": role,
            },
        )
