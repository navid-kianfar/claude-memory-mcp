"""asoode integration config: where the server is, and the PAT to reach it.

Two things live here, and they are deliberately separate:

*Endpoints* answer "which asoode?". The hosted service is the answer ~90% of the
time, so the three cloud URLs below are the defaults and a fresh install needs no
configuration at all. An on-premise install overrides them - which is why they are
resolved through a function instead of being read as constants at the call site.
Precedence is env > stored setting > default, so a site can bake its URLs into the
daemon's launchd environment and the UI still shows where each value came from.

*The PAT* answers "as whom?". It is stored ONCE for the whole machine, not per
project: `set_credential` keys on the API base URL and writes to the SQLite
registry's app_settings, so every project that talks to the same asoode reuses the
same entry and the user is never asked for it a second time. app_settings is local
and is never written into the committable `.claude-memory/` snapshot, so the token
cannot travel with a repo.

The raw token is write-only from the outside: `status()` and every HTTP route and
MCP tool return only a fingerprint (prefix + last4, the shape asoode's own
PersonalAccessToken record uses). Callers that actually need to authenticate use
`get_pat()` in-process.
"""

from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from memory_mcp.config import settings
from memory_mcp.constants import (
    ASOODE_DEFAULT_API_URL,
    ASOODE_DEFAULT_APP_URL,
    ASOODE_DEFAULT_SOCKET_URL,
)
from memory_mcp.db.registry import (
    get_credential,
    get_setting,
    set_credential,
    set_setting,
)

# app_settings keys for an on-premise override. Absent = use the cloud default.
_KEY_API = "asoode:api_url"
_KEY_APP = "asoode:app_url"
_KEY_SOCKET = "asoode:socket_url"


class AsoodeConfigError(ValueError):
    """A supplied URL or token is not usable."""


@dataclass(frozen=True)
class AsoodeEndpoints:
    """Where this machine talks to asoode.

    `app_url` is the human one (links a user can click), `api_url` the REST base
    that carries `Authorization: Bearer <PAT>`, `socket_url` the Socket.IO origin
    the Phase 2 inbound subscription will connect to.
    """

    app_url: str
    api_url: str
    socket_url: str
    #: False as soon as any one of the three is overridden - the UI says so.
    is_default: bool
    #: Per-field origin: "default" | "env" | "setting".
    sources: dict[str, str]

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_url(url: str, *, field: str = "url") -> str:
    """Validate an absolute http(s) URL and strip its trailing slash.

    A path is allowed - an on-premise asoode can sit behind a reverse proxy at
    https://host/asoode/api - so only the scheme and host are constrained.
    """
    raw = (url or "").strip()
    if not raw:
        raise AsoodeConfigError(f"{field} must not be empty")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise AsoodeConfigError(
            f"{field} must start with http:// or https:// (got {raw!r})"
        )
    if not parsed.netloc:
        raise AsoodeConfigError(f"{field} is missing a host (got {raw!r})")
    return raw.rstrip("/")


def _is_plaintext_remote(url: str) -> bool:
    """True for http:// to somewhere other than this machine - the PAT would
    cross the network in the clear."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1")


def _resolve(key: str, env_value: str, default: str) -> tuple[str, str]:
    """Resolve one endpoint to (value, source). env beats stored beats default."""
    if env_value:
        return normalize_url(env_value, field=key), "env"
    stored = get_setting(key)
    if stored:
        return stored, "setting"
    return default, "default"


def get_endpoints() -> AsoodeEndpoints:
    """The asoode URLs in force on this machine."""
    app, app_src = _resolve(_KEY_APP, settings.asoode_app_url, ASOODE_DEFAULT_APP_URL)
    api, api_src = _resolve(_KEY_API, settings.asoode_api_url, ASOODE_DEFAULT_API_URL)
    sock, sock_src = _resolve(
        _KEY_SOCKET, settings.asoode_socket_url, ASOODE_DEFAULT_SOCKET_URL
    )
    sources = {"app_url": app_src, "api_url": api_src, "socket_url": sock_src}
    return AsoodeEndpoints(
        app_url=app,
        api_url=api,
        socket_url=sock,
        is_default=all(s == "default" for s in sources.values()),
        sources=sources,
    )


def derive_siblings(api_url: str) -> dict[str, str]:
    """Guess the app/socket URLs for an api URL shaped like the hosted service.

    asoode's own deployment splits by subdomain (api./app./socket.), and most
    on-premise installs copy that layout - so `api.example.com` yields the other
    two. Anything else returns {} rather than a guess: a wrong socket URL fails
    at connect time, far from the config that caused it.
    """
    parsed = urlparse(api_url)
    host = (parsed.hostname or "").lower()
    if not host.startswith("api."):
        return {}
    rest = host[len("api.") :]
    port = f":{parsed.port}" if parsed.port else ""
    base = f"{parsed.scheme}://{{sub}}.{rest}{port}{parsed.path}".rstrip("/")
    return {
        "app_url": base.format(sub="app"),
        "socket_url": base.format(sub="socket"),
    }


def set_endpoints(
    *,
    api_url: str | None = None,
    app_url: str | None = None,
    socket_url: str | None = None,
    derive: bool = True,
) -> AsoodeEndpoints:
    """Point this machine at an on-premise asoode.

    Supplying only `api_url` fills in the other two by subdomain when the host is
    shaped like the hosted service (see `derive_siblings`); pass them explicitly
    for any other layout. Only the fields given are written, so a site can
    override the socket alone and keep the hosted app and api.
    """
    updates: dict[str, str] = {}
    if api_url is not None:
        normalized = normalize_url(api_url, field="api_url")
        updates[_KEY_API] = normalized
        if derive:
            for field, value in derive_siblings(normalized).items():
                key = {"app_url": _KEY_APP, "socket_url": _KEY_SOCKET}[field]
                updates.setdefault(key, value)
    if app_url is not None:
        updates[_KEY_APP] = normalize_url(app_url, field="app_url")
    if socket_url is not None:
        updates[_KEY_SOCKET] = normalize_url(socket_url, field="socket_url")
    if not updates:
        raise AsoodeConfigError(
            "give at least one of api_url, app_url, socket_url"
        )
    for key, value in updates.items():
        set_setting(key, value)
    return get_endpoints()


def reset_endpoints() -> AsoodeEndpoints:
    """Drop every override and go back to the hosted defaults.

    The stored PAT is keyed by API URL, so it is left alone - going back to the
    cloud restores the cloud token if one was ever set for it.
    """
    for key in (_KEY_API, _KEY_APP, _KEY_SOCKET):
        set_setting(key, "")
    return get_endpoints()


# ---------- the PAT: one per asoode server, shared by every project ----------


def pat_fingerprint(token: str) -> dict | None:
    """A safe-to-display identity for a token: prefix, last4, length.

    Mirrors asoode's own PersonalAccessToken record (tokenPrefix/last4), so the
    same token is recognisable on both sides without either showing it.
    """
    tok = (token or "").strip()
    if not tok:
        return None
    return {
        "prefix": tok[:6],
        "last4": tok[-4:] if len(tok) >= 8 else "",
        "length": len(tok),
    }


def get_pat(api_url: str | None = None) -> str | None:
    """The stored PAT for an asoode server - the raw value, for in-process use."""
    return get_credential(api_url or get_endpoints().api_url)


def set_pat(token: str, api_url: str | None = None) -> dict:
    """Store the PAT once, for every project on this machine.

    Returns the fingerprint, never the token: this value crosses an HTTP response
    or an MCP result, and the caller already has the secret.
    """
    tok = (token or "").strip()
    if not tok:
        raise AsoodeConfigError("token must not be empty")
    if any(ch.isspace() for ch in tok):
        raise AsoodeConfigError(
            "token contains whitespace - paste the PAT alone, without quotes"
        )
    url = normalize_url(api_url, field="api_url") if api_url else get_endpoints().api_url
    set_credential(url, tok)
    return {"api_url": url, "fingerprint": pat_fingerprint(tok)}


def clear_pat(api_url: str | None = None) -> dict:
    """Forget the stored PAT for an asoode server."""
    url = normalize_url(api_url, field="api_url") if api_url else get_endpoints().api_url
    set_credential(url, "")
    return {"api_url": url, "cleared": True}


def status() -> dict:
    """Everything a UI, CLI or MCP tool may see - the token itself excluded."""
    endpoints = get_endpoints()
    token = get_pat(endpoints.api_url)
    warnings: list[str] = []
    if not token:
        warnings.append(
            "No asoode PAT stored. Set it once with `memory-mcp asoode set-pat` - "
            "it then applies to every project on this machine."
        )
    for field in ("api_url", "socket_url"):
        url = getattr(endpoints, field)
        if _is_plaintext_remote(url):
            warnings.append(
                f"{field} is plain http to a remote host - the PAT would cross "
                f"the network unencrypted ({url})."
            )
    return {
        "endpoints": endpoints.as_dict(),
        "pat_configured": bool(token),
        "pat": pat_fingerprint(token) if token else None,
        "warnings": warnings,
    }


# ---------- opening asoode already signed in ----------
#
# asoode ships a deep link for exactly this (apps/frontend: AccessTokenCallbackPage
# + lib/access-token-link.ts): /auth/token?returnUrl=<path>#t=<PAT> signs the
# browser in and strips the fragment from the address bar on arrival. The token
# rides in the FRAGMENT, never a query string - a fragment is not sent to the
# server, not written to an access log, and not leaked through Referer.
#
# Because the resulting string contains the secret, it stays in-process: the CLI
# hands it straight to the browser and prints a redacted line instead. There is
# deliberately no HTTP route that returns or redirects to it - that would put the
# PAT in a response body or a Location header, readable by anything that can
# reach the daemon.


class AsoodeNotAuthenticated(AsoodeConfigError):
    """No PAT is stored, so no signed-in link can be built."""


def signin_url(return_path: str = "") -> str:
    """A deep link that opens asoode already signed in as the stored PAT's user.

    `return_path` must be an app-relative path (asoode's safeReturnUrl rejects
    anything else, so an absolute URL here would silently drop to the dashboard).

    NEVER print, log, or send the result anywhere but a browser.
    """
    from urllib.parse import quote, urlencode

    endpoints = get_endpoints()
    token = get_pat(endpoints.api_url)
    if not token:
        raise AsoodeNotAuthenticated(
            f"No asoode PAT stored for {endpoints.api_url}. Run "
            "`memory-mcp asoode set-pat` once and every project can open signed in."
        )
    url = f"{endpoints.app_url}/auth/token"
    if return_path:
        if not return_path.startswith("/") or return_path.startswith("//"):
            raise AsoodeConfigError(
                f"return_path must be an app-relative path like '/projects/x' "
                f"(got {return_path!r})"
            )
        url += "?" + urlencode({"returnUrl": return_path})
    return f"{url}#t={quote(token, safe='')}"


def redacted(url: str) -> str:
    """The same link with the token replaced - safe to print or log."""
    head, _, _ = url.partition("#t=")
    return f"{head}#t=<PAT>"
