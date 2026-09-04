"""Starlette routes for the management UI: a JSON API + the single-page app.

Handlers are plain sync functions wrapped by `_api`, which reads the request
body, runs the handler in a worker thread (DuckDB calls are blocking), and
serializes the result. The daemon owns the only writable DB connections, so
the UI and the Claude clients never contend for locks.
"""

from pathlib import Path

import httpx
from anyio import to_thread
from starlette.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from memory_mcp import __version__
from memory_mcp import asoode
from memory_mcp.asoode_client import AsoodeAuthError, AsoodeError
from memory_mcp.config import settings
from memory_mcp.container import container
from memory_mcp.context import (
    LOCAL_USER, RequestUser, current_user, get_active_project, get_request_user,
    reset_request_user, set_active_project, set_request_user,
)
from memory_mcp.db.registry import get_credential
from memory_mcp.exceptions import (
    AuthError, ForbiddenError, MemoryMCPError, MemoryNotFoundError,
    ProjectNotFoundError, TaskNotFoundError,
)
from memory_mcp.repositories import TemplateNotFoundError
from memory_mcp.services.adaptation import adaptation_brief
from memory_mcp.models import (
    CreateTaskRequest, GLOBAL_PROJECT_SLUG, MemoryCategory, MemoryFilter,
    Pagination, RULE_CATEGORIES, SearchRequest, StoreMemoryRequest, TaskFilter,
    TaskSource, TaskState, UpdateMemoryRequest, UpdateTaskRequest, rule_category,
)

def _dist_dir() -> Path:
    """Locate the built frontend: an explicit MEMORY_MCP_UI_DIR wins, otherwise
    the repo-relative frontend/dist (works for source + Docker installs)."""
    if settings.ui_dir:
        return Path(settings.ui_dir)
    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


_DIST = _dist_dir()

_PLACEHOLDER = """<!doctype html>
<html><head><meta charset="utf-8"><title>Memory MCP</title></head>
<body style="font-family:system-ui,sans-serif;background:#09090b;color:#e4e4e7;padding:48px;line-height:1.6">
<h1 style="color:#fafafa">Memory MCP - UI not built</h1>
<p>The React management UI has not been built yet. Build it with:</p>
<pre style="background:#18181b;padding:14px;border-radius:8px">cd frontend &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>Or run the Docker image, which builds the UI automatically.</p>
<p>The MCP server and JSON API are unaffected by this.</p>
</body></html>"""


def _flag(value: str | None) -> bool:
    """Parse a boolean query parameter the way the rest of this module does."""
    return (value or "").lower() in ("1", "true", "yes")


def _mem(memory) -> dict:
    """Serialize a Memory without the bulky embedding vector."""
    data = memory.model_dump(mode="json")
    data.pop("embedding", None)
    return data


# ---------- auth (server mode) ----------

SESSION_COOKIE = "mmcp_session"
# CSRF defense for cookie-authenticated writes: the SPA sends this custom header,
# which a cross-site attacker cannot set without a CORS preflight the daemon never
# grants. Bearer-token callers (MCP/CLI) carry no ambient cookie, so are exempt.
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "memory-mcp"
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _authenticate(request) -> tuple[RequestUser | None, str | None]:
    """Resolve the caller and how they authenticated: ('bearer'|'cookie'|None).

    Only meaningful in server mode; the lookups are fast indexed SQLite reads.
    """
    from memory_mcp.db.registry import authenticate_session, authenticate_token

    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        u = authenticate_token(auth[7:].strip())
        if u:
            return RequestUser(id=u["id"], username=u["username"], role=u["role"]), "bearer"
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        u = authenticate_session(cookie)
        if u:
            return RequestUser(id=u["id"], username=u["username"], role=u["role"]), "cookie"
    return None, None


def _request_user_obj(request) -> RequestUser | None:
    return _authenticate(request)[0]


def _hook_authorized(request) -> bool:
    """Hook endpoints (_hook_rules, _hook_auto_register) are raw handlers, not
    _api-wrapped. In server mode they must carry a valid bearer token so rules
    are never served to an unauthenticated caller; in local mode always allowed.
    """
    if not settings.server_mode:
        return True
    return _request_user_obj(request) is not None


async def _proxy_to_remote(request, project) -> Response:
    """Forward a request for a remote-bound project to its org server, verbatim,
    carrying the stored credential. The local handler is bypassed entirely, so a
    remote project's data never touches local storage."""
    token = await to_thread.run_sync(get_credential, project.remote_url)
    url = project.remote_url.rstrip("/") + request.url.path
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    ct = request.headers.get("content-type")
    if ct:
        headers["Content-Type"] = ct
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.request(
                request.method, url,
                params=dict(request.query_params), content=body, headers=headers,
            )
    except httpx.HTTPError as e:
        return JSONResponse(
            {"error": f"Remote server unreachable: {e}", "type": "RemoteError"},
            status_code=502,
        )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


def _remote_project(slug: str | None):
    """Return the ProjectInfo if slug names a remote-bound project, else None."""
    if not slug:
        return None
    try:
        proj = container.project_repo.get(slug)
    except Exception:  # noqa: BLE001
        return None
    if proj and proj.backend == "remote" and proj.remote_url:
        return proj
    return None


def _api(fn, *, public: bool = False, admin: bool = False, remote_aware: bool = False):
    """Wrap a sync handler `fn(params, body, query) -> data | (data, status)`.

    In server mode the caller is authenticated (Bearer token or session cookie)
    and bound to the request context so the handler thread and everything it
    calls (current_user, per-user active project, rule attribution) see the right
    identity. `public=True` skips the auth requirement (login/meta/health);
    `admin=True` additionally requires the admin role. In local mode auth is a
    no-op: a synthetic local admin is bound and nothing is ever rejected.

    `remote_aware=True` marks a project-scoped data route: when its `slug`
    project is bound to a remote server, the request is proxied there instead of
    running the local handler (the gateway - see _proxy_to_remote).
    """

    async def handler(request):
        body: dict = {}
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                body = await request.json()
            except Exception:
                body = {}
        params = dict(request.path_params)
        query = dict(request.query_params)

        # Gateway: hand off remote-bound projects to their org server before any
        # local work. Not gated on server_mode - a local daemon gateways too.
        if remote_aware:
            proj = await to_thread.run_sync(_remote_project, params.get("slug"))
            if proj is not None:
                return await _proxy_to_remote(request, proj)

        if settings.server_mode:
            user, method = _authenticate(request)
            if user is None and not public:
                return JSONResponse(
                    {"error": "Authentication required", "type": "AuthError"},
                    status_code=401,
                )
            if admin and (user is None or not user.is_admin):
                return JSONResponse(
                    {"error": "Admin role required", "type": "ForbiddenError"},
                    status_code=403,
                )
            # CSRF: a cookie-authenticated write must carry the SPA's custom header.
            if (
                method == "cookie"
                and request.method in _WRITE_METHODS
                and request.headers.get(CSRF_HEADER) != CSRF_VALUE
            ):
                return JSONResponse(
                    {"error": "Missing CSRF header", "type": "ForbiddenError"},
                    status_code=403,
                )
            bound = user  # may be None for an unauthenticated public endpoint
        else:
            bound = LOCAL_USER

        token = set_request_user(bound)
        try:
            result = await to_thread.run_sync(lambda: fn(params, body, query))
        except (
            ProjectNotFoundError, MemoryNotFoundError, TemplateNotFoundError,
            TaskNotFoundError,
        ) as e:
            return JSONResponse({"error": str(e), "type": type(e).__name__}, status_code=404)
        except AsoodeAuthError as e:
            # The stored PAT was refused. 502, not 401: the caller of THIS API is
            # fine - it is our credential for the upstream that is not.
            return JSONResponse({"error": str(e), "type": type(e).__name__}, status_code=502)
        except AsoodeError as e:
            return JSONResponse({"error": str(e), "type": type(e).__name__}, status_code=502)
        except AuthError as e:
            return JSONResponse({"error": str(e), "type": type(e).__name__}, status_code=401)
        except ForbiddenError as e:
            return JSONResponse({"error": str(e), "type": type(e).__name__}, status_code=403)
        except (MemoryMCPError, ValueError) as e:
            return JSONResponse({"error": str(e), "type": type(e).__name__}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e), "type": type(e).__name__}, status_code=500)
        finally:
            reset_request_user(token)
        data, status = result if isinstance(result, tuple) else (result, 200)
        return JSONResponse(data, status_code=status)

    return handler


# ---------- Handlers ----------


async def _hook_auto_register(request):
    """Auto-register the working directory as a project (used by the hook).

    When Claude Code starts a session in a git repository that is not yet a
    memory project, register it so it shows up in the UI - even before it has
    any rules. Returns a short note, or empty when nothing was done.
    """
    if not _hook_authorized(request):
        return PlainTextResponse("")
    cwd = request.query_params.get("cwd", "")

    def _resolve() -> str:
        from pathlib import Path

        from memory_mcp.context import detect_project_from_cwd
        from memory_mcp.utils.text import slugify

        if not cwd:
            return ""
        folder = Path(cwd)
        if not folder.is_dir():
            return ""
        if detect_project_from_cwd(cwd):
            return ""  # already a registered project
        if not (folder / ".git").is_dir():
            return ""  # only auto-register actual repositories
        slug = slugify(folder.name)
        if not slug or container.project_repo.get(slug) is not None:
            return ""  # no name, or the slug is already taken by another project
        container.project_service.init_project(slug, folder.name, project_path=cwd)
        return (
            f"[Memory MCP] Registered this folder as project '{slug}' - "
            f"it now appears in the management UI."
        )

    try:
        text = await to_thread.run_sync(_resolve)
    except Exception:  # noqa: BLE001
        text = ""
    return PlainTextResponse(text)


async def _hook_claim(request):
    """Bind a folder to the project its committed snapshot names.

    The SessionStart hook calls this before anything else. Keying on
    manifest.json's project_id means a project that was moved or renamed is
    re-bound to its new location instead of being registered a second time,
    and a teammate's fresh clone adopts the same identity.
    """
    if not _hook_authorized(request):
        return JSONResponse({"slug": None, "action": "unauthorized"})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    def _resolve() -> dict:
        cwd = (body.get("cwd") or "").strip()
        if not cwd:
            return {"slug": None, "action": "unclaimed"}
        return container.project_service.claim_folder(
            cwd,
            project_uid=body.get("project_id"),
            slug_hint=body.get("slug"),
            display_name=body.get("display_name"),
        )

    try:
        result = await to_thread.run_sync(_resolve)
    except Exception as exc:  # noqa: BLE001 - a hook must never see a 500
        result = {"slug": None, "action": "error", "error": str(exc)}
    return JSONResponse(result)


def _index(_request):
    """Serve the built React SPA, or a placeholder when it has not been built."""
    index = _DIST / "index.html"
    if index.is_file():
        return FileResponse(str(index))
    return HTMLResponse(_PLACEHOLDER)


async def _hook_rules(request):
    """Plain-text rules block for Claude Code hooks (cwd -> project -> rules).

    Returns an empty body when the directory is not a memory project, so the
    hook stays silent in unrelated repos.
    """
    if not _hook_authorized(request):
        return PlainTextResponse("")
    cwd = request.query_params.get("cwd", "")
    mode = request.query_params.get("mode", "rules")

    def _resolve() -> str:
        from memory_mcp.context import detect_project_from_cwd
        from memory_mcp.enforcement import (
            format_intro, format_session_end, rules_text_for_project,
        )

        slug = detect_project_from_cwd(cwd)
        if not slug:
            return ""
        if mode == "intro":
            return format_intro(slug)
        if mode == "end":
            return format_session_end(slug)
        return rules_text_for_project(slug)

    try:
        text = await to_thread.run_sync(_resolve)
    except Exception:  # noqa: BLE001
        text = ""
    return PlainTextResponse(text)


async def _login(request):
    """Exchange a username + API token for an HttpOnly session cookie."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not settings.server_mode:
        # No accounts in local mode; login is a no-op so the UI can call it safely.
        return JSONResponse({"status": "ok", "user": None})

    from memory_mcp.db.registry import authenticate_token, create_session

    username = (body.get("username") or "").strip()
    token = (body.get("token") or "").strip()
    user = authenticate_token(token)
    if not user or (username and user["username"] != username):
        return JSONResponse(
            {"error": "Invalid username or token", "type": "AuthError"}, status_code=401
        )
    session = create_session(user["id"])
    if not session:
        return JSONResponse(
            {"error": "Account is inactive", "type": "ForbiddenError"}, status_code=403
        )
    resp = JSONResponse({"status": "ok", "user": user})
    resp.set_cookie(
        SESSION_COOKIE, session,
        httponly=True, secure=settings.cookie_secure, samesite="strict", path="/",
    )
    return resp


async def _logout(request):
    """Invalidate the current session and clear the cookie."""
    resp = JSONResponse({"status": "ok"})
    if settings.server_mode:
        from memory_mcp.db.registry import clear_session

        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            clear_session(cookie)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


async def _whoami(request):
    """Report the current mode and authenticated identity (null if none)."""
    if not settings.server_mode:
        return JSONResponse({"mode": "local", "user": None})
    user, _ = _authenticate(request)
    return JSONResponse(
        {
            "mode": "server",
            "user": None if user is None
            else {"id": user.id, "username": user.username, "role": user.role},
        }
    )


def _health(params, body, query):
    return {"status": "ok", "version": __version__}


def _meta(params, body, query):
    server = settings.server_mode
    # The actually-authenticated user (None when unauthenticated); in local mode
    # there are no accounts, so current_user/role are reported as null and the UI
    # renders exactly as today.
    user = get_request_user() if server else None
    user_public = (
        None if user is None or user.id == LOCAL_USER.id
        else {"id": user.id, "username": user.username, "role": user.role}
    )
    return {
        "version": __version__,
        "categories": [c.value for c in MemoryCategory],
        "rule_categories": ["mandatory_rules", "forbidden_rules"],
        "active_project": get_active_project(),
        "model": settings.embedding_model,
        "mode": "server" if server else "local",
        "current_user": user_public,
        "role": None if user_public is None else user_public["role"],
    }


def _list_projects(params, body, query):
    projects = []
    for p in container.project_service.list_all():
        d = p.model_dump(mode="json")
        try:
            _, total = container.memory_repo.list(
                p.slug, MemoryFilter(status="active"), Pagination(limit=1),
            )
            d["memory_count"] = total
        except Exception:  # noqa: BLE001
            d["memory_count"] = None
        projects.append(d)
    return {"projects": projects}


def _create_project(params, body, query):
    slug = (body.get("slug") or "").strip()
    display_name = (body.get("display_name") or slug).strip()
    if not slug:
        raise ValueError("slug is required")
    project = container.project_service.init_project(
        slug, display_name, body.get("description"),
        project_path=(body.get("project_path") or "").strip() or None,
    )
    return {"status": "ok", "project": project.model_dump(mode="json")}, 201


def _link_folder(params, body, query):
    path = (body.get("path") or "").strip()
    if not path:
        raise ValueError("path is required")
    info = container.project_service.link_folder(params["slug"], path)
    return {"status": "ok", "project": info.model_dump(mode="json")}


def _bind_backend(params, body, query):
    backend = (body.get("backend") or "").strip()
    info = container.project_service.bind_backend(
        params["slug"], backend,
        remote_url=body.get("remote_url"), token=body.get("token"),
    )
    return {"status": "ok", "project": info.model_dump(mode="json")}


def _pick_folder(params, body, query):
    """Open a native OS folder picker on the daemon host, return the chosen path.

    Browsers cannot expose absolute filesystem paths, so the picker runs here
    (the daemon is a local process). macOS uses `osascript`, Linux uses
    `zenity`; anywhere else - or headless - it reports unavailable so the UI
    falls back to a plain typed path.
    """
    import subprocess
    import sys

    prompt = (body.get("prompt") or "Select the project folder")
    prompt = prompt.replace('"', "").replace("\\", "")[:120]
    try:
        if sys.platform == "darwin":
            proc = subprocess.run(
                ["osascript", "-e",
                 f'POSIX path of (choose folder with prompt "{prompt}")'],
                capture_output=True, text=True, timeout=600,
            )
        elif sys.platform.startswith("linux"):
            proc = subprocess.run(
                ["zenity", "--file-selection", "--directory", "--title", prompt],
                capture_output=True, text=True, timeout=600,
            )
        else:
            return {"status": "unavailable"}
    except FileNotFoundError:
        return {"status": "unavailable"}
    except subprocess.TimeoutExpired:
        return {"status": "cancelled"}

    if proc.returncode != 0:
        return {"status": "cancelled"}  # user dismissed the dialog
    path = proc.stdout.strip().rstrip("/")
    return {"status": "ok", "path": path} if path else {"status": "cancelled"}


def _load_from_folder(params, body, query):
    from memory_mcp.folder_import import load_project_from_folder

    path = (body.get("path") or "").strip()
    if not path:
        raise ValueError("path is required")
    return load_project_from_folder(path)


def _project_info(params, body, query):
    slug = params["slug"]
    project = container.project_service.get(slug)
    counts: dict[str, int] = {}
    for cat in MemoryCategory:
        _, total = container.memory_repo.list(
            slug, MemoryFilter(status="active", category=cat.value), Pagination(limit=1),
        )
        counts[cat.value] = total
    return {"project": project.model_dump(mode="json"), "counts": counts}


def _update_project(params, body, query):
    """Rename a project / update its description."""
    info = container.project_service.update_project(
        params["slug"],
        display_name=(body.get("display_name") or None),
        description=body.get("description"),
    )
    return {"status": "ok", "project": info.model_dump(mode="json")}


def _set_active(params, body, query):
    slug = (body.get("slug") or "").strip()
    if not slug:
        raise ValueError("slug is required")
    container.project_service.get(slug)  # validate it exists
    set_active_project(slug)
    return {"status": "ok", "active_project": slug}


def _list_memories(params, body, query):
    slug = params["slug"]
    q = (query.get("q") or "").strip()
    category = query.get("category") or None
    status = query.get("status") or "active"
    limit = int(query.get("limit") or 50)
    offset = int(query.get("offset") or 0)

    if q:
        req = SearchRequest(
            project=slug, query=q,
            category=MemoryCategory(category) if category else None,
            status=status, limit=min(limit, 100), min_similarity=0.0,
        )
        response = container.search_service.search(req)
        hits = getattr(response, "results", [])
        return {
            "mode": "search",
            "memories": [
                {**_mem(h.memory), "_similarity": round(h.similarity, 3),
                 "_relevance": round(h.relevance_score, 3)}
                for h in hits
            ],
            "total": len(hits),
        }

    filters = MemoryFilter(status=status, category=category)
    pagination = Pagination(limit=limit, offset=offset)
    memories, total = container.memory_repo.list(slug, filters, pagination)
    return {
        "mode": "list",
        "memories": [_mem(m) for m in memories],
        "total": total, "limit": limit, "offset": offset,
    }


def _create_memory(params, body, query):
    slug = params["slug"]
    category = body.get("category")
    if category not in {c.value for c in MemoryCategory}:
        raise ValueError(f"invalid or missing category: {category!r}")
    req = StoreMemoryRequest(
        project=slug,
        category=MemoryCategory(category),
        title=body.get("title") or "",
        content=body.get("content") or "",
        tags=body.get("tags") or [],
        metadata=body.get("metadata"),
        priority=body.get("priority", 0),
        source=body.get("source", "user"),
    )
    memory = container.memory_service.store(req)
    return {"status": "ok", "memory": _mem(memory)}, 201


def _get_memory(params, body, query):
    slug = params["slug"]
    memory = container.memory_repo.get_by_id(slug, params["mid"])
    if memory is None:
        raise MemoryNotFoundError(f"Memory not found: {params['mid']}")
    return {"memory": _mem(memory)}


def _update_memory(params, body, query):
    slug = params["slug"]
    req = UpdateMemoryRequest(
        project=slug,
        memory_id=params["mid"],
        title=body.get("title"),
        content=body.get("content"),
        tags=body.get("tags"),
        metadata=body.get("metadata"),
        status=body.get("status"),
        priority=body.get("priority"),
    )
    memory = container.memory_service.update(req)
    return {"status": "ok", "memory": _mem(memory)}


def _delete_memory(params, body, query):
    slug = params["slug"]
    hard = (query.get("hard") or "").lower() in ("1", "true", "yes")
    return container.memory_service.delete(
        slug, params["mid"], hard=hard, reason=query.get("reason"),
    )


def _import_claude_md(params, body, query):
    slug = params["slug"]
    path = (body.get("path") or "").strip()
    if not path:
        raise ValueError("path is required")
    container.project_service.get(slug)
    return container.claude_md_service.import_file(
        slug, path, bool(body.get("stub_rewrite", False)),
    )


def _rules(params, body, query):
    # Management view: the project's OWN rules in every approval state (so the UI
    # can badge proposed/approved/revoked and offer approve/revoke), WITHOUT the
    # injected org-wide rules or the approval filter that enforcement applies. In
    # local mode every rule is 'approved', so this is identical to before.
    mandatory, forbidden = container.memory_repo.get_rules(
        params["slug"], enforce_approval=False
    )
    return {
        "mandatory_rules": [_mem(m) for m in mandatory],
        "forbidden_rules": [_mem(m) for m in forbidden],
        "total": len(mandatory) + len(forbidden),
    }


def _load_rule(slug: str, rule_id: str):
    existing = container.memory_repo.get_by_id(slug, rule_id)
    if existing is None or existing.category not in RULE_CATEGORIES:
        raise MemoryNotFoundError(f"Rule not found: {rule_id}")
    return existing


def _add_rule(params, body, query):
    slug = params["slug"]
    req = StoreMemoryRequest(
        project=slug,
        category=rule_category(body.get("rule_type")),
        title=body.get("title") or "",
        content=body.get("content") or "",
        priority=body.get("priority", 2),
        source="user",
    )
    memory = container.memory_service.store(req)
    return {"status": "ok", "rule": _mem(memory)}, 201


def _update_rule(params, body, query):
    slug = params["slug"]
    _load_rule(slug, params["rid"])
    req = UpdateMemoryRequest(
        project=slug, memory_id=params["rid"],
        title=body.get("title"), content=body.get("content"),
        status=body.get("status"),
    )
    memory = container.memory_service.update(req)
    return {"status": "ok", "rule": _mem(memory)}


def _delete_rule(params, body, query):
    slug = params["slug"]
    _load_rule(slug, params["rid"])
    hard = (query.get("hard") or "").lower() in ("1", "true", "yes")
    return container.memory_service.delete(slug, params["rid"], hard=hard)


def _bulk_add_rule(params, body, query):
    """Add one rule to many projects at once (all registered, or a chosen list)."""
    category = rule_category(body.get("rule_type"))
    title = (body.get("title") or "").strip()
    content = (body.get("content") or "").strip()
    if not title or not content:
        raise ValueError("title and content are required")
    priority = body.get("priority", 2)

    slugs = body.get("projects")
    if not slugs:  # empty / omitted -> every registered project
        slugs = [p.slug for p in container.project_service.list_all()]

    results = []
    for slug in slugs:
        try:
            container.project_service.get(slug)  # validate it exists
            memory = container.memory_service.store(
                StoreMemoryRequest(
                    project=slug, category=category, title=title,
                    content=content, priority=priority, source="user",
                )
            )
            results.append({"project": slug, "status": "ok", "rule_id": memory.id})
        except Exception as e:  # noqa: BLE001
            results.append({"project": slug, "status": "error", "error": str(e)})

    added = sum(1 for r in results if r["status"] == "ok")
    return {"status": "ok", "added": added, "total": len(slugs), "results": results}


def _sessions(params, body, query):
    sessions = container.session_repo.list_all(params["slug"], limit=50)
    return {"sessions": [s.model_dump(mode="json") for s in sessions]}


def _provenance(params, body, query):
    entries = container.provenance_repo.for_memory(params["slug"], params["mid"])
    return {
        "memory_id": params["mid"],
        "provenance": [e.model_dump(mode="json") for e in entries],
    }


def _sync_export(params, body, query):
    """Return the project's memory as a category-keyed snapshot for the CLI."""
    slug = params["slug"]
    container.project_service.get(slug)
    return {"categories": container.sync_service.build_snapshot(slug)}


def _sync_import(params, body, query):
    """Reconcile the project DB to a snapshot the CLI read from .claude-memory/."""
    slug = params["slug"]
    container.project_service.get(slug)
    categories = body.get("categories") or {}
    reconcile = body.get("reconcile")
    if reconcile is None:
        reconcile = list(categories.keys())
    result = container.sync_service.apply_snapshot(slug, categories, reconcile)
    return {"status": "ok", **result}


def _tpl(template) -> dict:
    return template.model_dump(mode="json")


# ---------- asoode integration (machine-wide, not per project) ----------
#
# Endpoints default to the hosted service and the PAT is stored once per asoode
# server, so a project never carries either. Writes are admin-gated: in local mode
# that is a no-op, on a shared server it stops a member repointing everyone's
# integration. The raw token is write-only here - reads return a fingerprint.


def _asoode_status(params, body, query):
    return asoode.status()


def _asoode_set_urls(params, body, query):
    if body.get("reset"):
        asoode.reset_endpoints()
        return asoode.status()
    asoode.set_endpoints(
        api_url=body.get("api_url"),
        app_url=body.get("app_url"),
        socket_url=body.get("socket_url"),
        derive=bool(body.get("derive", True)),
    )
    return asoode.status()


def _asoode_set_pat(params, body, query):
    asoode.set_pat(body.get("token") or "", body.get("api_url"))
    return asoode.status()


def _asoode_clear_pat(params, body, query):
    asoode.clear_pat(query.get("api_url"))
    return asoode.status()


def _asoode_boards(params, body, query):
    """Boards this credential can see - what the UI offers to attach."""
    return {"boards": container.task_bridge.boards(query.get("project_id"))}


def _asoode_link(params, body, query):
    """Create or find the asoode project + board for this memory project.

    Idempotent via externalRef, but it does create real objects on the user's
    asoode account the first time - the UI asks before calling it.
    """
    # `attach` links a board that already exists; without it this CREATES one.
    # The UI always attaches - creating from a browser click is too easy to do
    # by accident, and a stray board cannot be removed from here.
    if body.get("attach") or body.get("work_package_id") or body.get("external_ref"):
        return container.task_bridge.attach(
            params["slug"],
            work_package_id=body.get("work_package_id"),
            external_ref=body.get("external_ref"),
            label=body.get("label"),
            is_default=bool(body.get("is_default", True)),
            backfill=bool(body.get("backfill", False)),
        )
    return container.task_bridge.bootstrap(
        params["slug"],
        project_title=body.get("project_title"),
        board_title=body.get("board_title"),
        reuse_project_id=body.get("asoode_project_id"),
    )


def _asoode_push(params, body, query):
    return container.task_bridge.push(
        params["slug"], include_done=bool(body.get("include_done", True)),
    )


def _asoode_links(params, body, query):
    return {
        "slug": params["slug"],
        "links": container.task_bridge.links(params["slug"]),
    }


def _list_templates(params, body, query):
    return {"templates": [_tpl(t) for t in container.template_service.list_templates()]}


def _create_template(params, body, query):
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    template = container.template_service.create(name, body.get("description"))
    return {"status": "ok", "template": _tpl(template)}, 201


def _get_template(params, body, query):
    return {"template": _tpl(container.template_service.get(int(params["tid"])))}


def _update_template(params, body, query):
    template = container.template_service.update(
        int(params["tid"]), body.get("name"), body.get("description"),
    )
    return {"status": "ok", "template": _tpl(template)}


def _delete_template(params, body, query):
    return container.template_service.delete(int(params["tid"]))


def _add_template_item(params, body, query):
    item = container.template_service.add_item(
        int(params["tid"]),
        body.get("category", ""),
        body.get("title") or "",
        body.get("content") or "",
        body.get("priority", 0),
    )
    return {"status": "ok", "item": item.model_dump(mode="json")}, 201


def _update_template_item(params, body, query):
    item = container.template_service.update_item(
        int(params["iid"]),
        category=body.get("category"),
        title=body.get("title"),
        content=body.get("content"),
        priority=body.get("priority"),
    )
    return {"status": "ok", "item": item.model_dump(mode="json")}


def _delete_template_item(params, body, query):
    return container.template_service.delete_item(int(params["iid"]))


def _apply_template(params, body, query):
    slug = params["slug"]
    container.project_service.get(slug)
    template_id = body.get("template_id")
    if template_id is None:
        raise ValueError("template_id is required")
    result = container.template_service.apply(
        slug, int(template_id), body.get("item_ids"),
    )
    return {
        "status": "ok",
        "template": result["template"],
        "applied": result["applied"],
        "memories": [_mem(m) for m in result["memories"]],
    }


def _import_rules(params, body, query):
    slug = params["slug"]
    container.project_service.get(slug)
    source = (body.get("source_project") or "").strip()
    memory_ids = body.get("memory_ids") or []
    if not source or not memory_ids:
        raise ValueError("source_project and memory_ids are required")
    # Imports are pending unless explicitly waived: another project's specifics
    # must be rewritten for this one before they are enforced anywhere.
    pending = body.get("pending")
    pending = True if pending is None else bool(pending)
    result = container.memory_service.copy_memories(
        slug, source, memory_ids, pending=pending,
    )
    return {
        "status": "ok",
        "imported": result["imported"],
        "skipped": result["skipped"],
        "pending": result["pending"],
        "memories": [_mem(m) for m in result["memories"]],
    }


def _pending(params, body, query):
    """Imports awaiting adaptation to this project, with the agent's brief."""
    slug = params["slug"]
    container.project_service.get(slug)
    items = container.memory_service.list_pending(slug)
    return {
        "pending": [_mem(m) for m in items],
        "total": len(items),
        "instructions": adaptation_brief(slug, items),
    }


def _adapt_pending(params, body, query):
    memory = container.memory_service.adapt_pending(
        params["slug"], params["mid"],
        title=body.get("title") or "",
        content=body.get("content") or "",
        tags=body.get("tags"),
        priority=body.get("priority"),
    )
    return {"status": "ok", "memory": _mem(memory)}


def _discard_pending(params, body, query):
    return container.memory_service.discard_pending(
        params["slug"], params["mid"], body.get("reason"),
    )


# ---------- tasks ----------
#
# Queued requirements, stored in the project's own DuckDB tables. Not
# `remote_aware`: tasks have no counterpart on an org server in Phase 1, so a
# remote-bound project keeps its task list here rather than proxying to a
# server that has no /tasks route.


def _task_list(params, body, query):
    slug = params["slug"]
    container.project_service.get(slug)
    state = query.get("state")
    filters = TaskFilter(
        state=TaskState(state) if state else None,
        source=query.get("source") or None,
        parent_id=query.get("parent_id") or None,
        include_subtasks=_flag(query.get("include_subtasks")),
        include_done=_flag(query.get("include_done")),
        include_archived=_flag(query.get("include_archived")),
    )
    result = container.task_service.list_tasks(
        slug,
        filters,
        limit=int(query.get("limit") or 100),
        offset=int(query.get("offset") or 0),
    )
    return {
        "tasks": [t.model_dump(mode="json") for t in result.tasks],
        "total": result.total,
        "open": result.open_count,
        "running": result.running_ids,
        "meta": {tid: m.model_dump(mode="json") for tid, m in result.meta.items()},
    }


def _task_create(params, body, query):
    source = body.get("source") or "user"
    req = CreateTaskRequest(
        project=params["slug"],
        title=body.get("title") or "",
        description=body.get("description"),
        priority=body.get("priority") or 0,
        labels=body.get("labels") or [],
        assignee=body.get("assignee"),
        due_at=body.get("due_at"),
        estimated_minutes=body.get("estimated_minutes"),
        parent_id=body.get("parent_id"),
        source=TaskSource(source),
    )
    task = container.task_service.create(req)
    return {"status": "ok", "task": task.model_dump(mode="json")}


def _task_reorder(params, body, query):
    """Persist a drag-and-drop order. Body: {"ids": [task_id, ...]}."""
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        raise ValueError("ids must be a list of task ids")
    return {"status": "ok", "reordered": container.task_service.reorder(params["slug"], ids)}


def _task_get(params, body, query):
    detail = container.task_service.detail(params["slug"], params["tid"])
    return detail.model_dump(mode="json")


def _task_update(params, body, query):
    state = body.get("state")
    req = UpdateTaskRequest(
        project=params["slug"],
        task_id=params["tid"],
        title=body.get("title"),
        description=body.get("description"),
        state=TaskState(state) if state else None,
        priority=body.get("priority"),
        assignee=body.get("assignee"),
        labels=body.get("labels"),
        due_at=body.get("due_at"),
        begin_at=body.get("begin_at"),
        end_at=body.get("end_at"),
        estimated_minutes=body.get("estimated_minutes"),
    )
    task, changed = container.task_service.update(req)
    return {"status": "ok", "task": task.model_dump(mode="json"), "changed": changed}


def _task_comment(params, body, query):
    comment = container.task_service.comment(
        params["slug"], params["tid"],
        body.get("body") or "",
        body.get("kind") or "note",
        body.get("author"),
    )
    return {"status": "ok", "comment": comment.model_dump(mode="json")}


def _task_start(params, body, query):
    return container.task_service.start(params["slug"], params["tid"]).model_dump(mode="json")


def _task_stop(params, body, query):
    return container.task_service.stop(params["slug"], params["tid"]).model_dump(mode="json")


def _task_done(params, body, query):
    detail = container.task_service.done(
        params["slug"], params["tid"], body.get("note"),
    )
    return detail.model_dump(mode="json")


def _task_convert(params, body, query):
    """Promote a sub-task to a top-level task."""
    task = container.task_service.convert_to_task(params["slug"], params["tid"])
    return {"status": "ok", "task": task.model_dump(mode="json")}


def _task_delete(params, body, query):
    """Delete permanently. Archiving is the reversible option beside it."""
    return container.task_service.delete(params["slug"], params["tid"])


def _task_activity(params, body, query):
    """The task's audit trail, straight from the provenance table."""
    entries = container.task_service.activity(params["slug"], params["tid"])
    return {"activity": [e.model_dump(mode="json") for e in entries]}


def _task_release(params, body, query):
    """Force-release a claim from the UI.

    The operator escape hatch for a task left held by a session that never came
    back: it releases regardless of holder, unlike the MCP tool, which scopes
    the release to the calling session. Claiming itself has no route - it is a
    session concern, and the UI has no session to claim for.
    """
    task = container.task_service.release(params["slug"], params["tid"])
    return {"status": "ok", "task": task.model_dump(mode="json")}


def _task_archive(params, body, query):
    task = container.task_service.archive(params["slug"], params["tid"])
    return {"status": "ok", "task": task.model_dump(mode="json")}


# ---------- governance: users, approvals, org-wide rules (server mode) ----------


def _list_users(params, body, query):
    from memory_mcp.db.registry import list_users

    return {"users": list_users()}


def _create_user(params, body, query):
    from memory_mcp.db.registry import create_user

    username = (body.get("username") or "").strip()
    if not username:
        raise ValueError("username is required")
    role = body.get("role") or "member"
    user, token = create_user(username, body.get("display_name"), role)
    return {"status": "ok", "user": user, "token": token}, 201


def _deactivate_user(params, body, query):
    from memory_mcp.db.registry import get_user, set_user_active

    uid = params["uid"]
    if get_user(uid) is None:
        raise MemoryNotFoundError(f"User not found: {uid}")
    set_user_active(uid, False)
    return {"status": "ok", "user": get_user(uid)}


def _rotate_user_token(params, body, query):
    from memory_mcp.db.registry import get_user, rotate_token

    uid = params["uid"]
    token = rotate_token(uid)
    if token is None:
        raise MemoryNotFoundError(f"User not found: {uid}")
    return {"status": "ok", "user": get_user(uid), "token": token}


def _approve_rule(params, body, query):
    slug = params["slug"]
    _load_rule(slug, params["rid"])
    memory = container.memory_service.approve_rule(slug, params["rid"])
    return {"status": "ok", "rule": _mem(memory)}


def _revoke_rule(params, body, query):
    slug = params["slug"]
    _load_rule(slug, params["rid"])
    memory = container.memory_service.revoke_rule(slug, params["rid"])
    return {"status": "ok", "rule": _mem(memory)}


def _pending_rules(params, body, query):
    """Global moderation queue: proposed rules across all projects (incl. org)."""
    pending = []
    for p in container.project_service.list_all(include_global=True):
        for m in container.rules_service.pending_rules(p.slug):
            pending.append(
                {
                    "project": {"slug": p.slug, "display_name": p.display_name},
                    "rule": _mem(m),
                }
            )
    return {"pending": pending, "total": len(pending)}


def _list_org_rules(params, body, query):
    # All org-wide rules in every approval state, for the admin editor.
    container.project_service.ensure_global_project()
    mandatory, forbidden = container.memory_repo.get_rules(
        GLOBAL_PROJECT_SLUG, enforce_approval=False
    )
    return {
        "mandatory_rules": [_mem(m) for m in mandatory],
        "forbidden_rules": [_mem(m) for m in forbidden],
        "total": len(mandatory) + len(forbidden),
    }


def _create_org_rule(params, body, query):
    container.project_service.ensure_global_project()
    req = StoreMemoryRequest(
        project=GLOBAL_PROJECT_SLUG,
        category=rule_category(body.get("rule_type")),
        title=body.get("title") or "",
        content=body.get("content") or "",
        priority=body.get("priority", 2),
        source="user",
    )
    memory = container.memory_service.store(req)
    return {"status": "ok", "rule": _mem(memory)}, 201


def _update_org_rule(params, body, query):
    _load_rule(GLOBAL_PROJECT_SLUG, params["rid"])
    req = UpdateMemoryRequest(
        project=GLOBAL_PROJECT_SLUG, memory_id=params["rid"],
        title=body.get("title"), content=body.get("content"),
        status=body.get("status"),
    )
    memory = container.memory_service.update(req)
    return {"status": "ok", "rule": _mem(memory)}


def _delete_org_rule(params, body, query):
    _load_rule(GLOBAL_PROJECT_SLUG, params["rid"])
    hard = (query.get("hard") or "").lower() in ("1", "true", "yes")
    return container.memory_service.delete(GLOBAL_PROJECT_SLUG, params["rid"], hard=hard)


def build_routes() -> list:
    """Return the UI + JSON API routes for mounting on the daemon."""
    routes: list = [
        Route("/", _index, methods=["GET"]),
        Route("/api/health", _api(_health, public=True), methods=["GET"]),
        Route("/api/hook/rules", _hook_rules, methods=["GET"]),
        Route("/api/hook/auto-register", _hook_auto_register, methods=["GET"]),
        Route("/api/hook/claim", _hook_claim, methods=["POST"]),
        Route("/api/meta", _api(_meta, public=True), methods=["GET"]),
        # Auth (server mode): login/whoami are auth-optional; logout clears state.
        Route("/api/auth/login", _login, methods=["POST"]),
        Route("/api/auth/logout", _logout, methods=["POST"]),
        Route("/api/auth/whoami", _whoami, methods=["GET"]),
        Route("/api/projects", _api(_list_projects), methods=["GET"]),
        Route("/api/projects", _api(_create_project), methods=["POST"]),
        Route("/api/projects/load-from-folder", _api(_load_from_folder), methods=["POST"]),
        Route("/api/pick-folder", _api(_pick_folder), methods=["POST"]),
        Route("/api/active", _api(_set_active), methods=["POST"]),
        Route("/api/projects/{slug}", _api(_project_info, remote_aware=True), methods=["GET"]),
        Route("/api/projects/{slug}", _api(_update_project), methods=["PUT"]),
        Route("/api/rules/bulk", _api(_bulk_add_rule), methods=["POST"]),
        Route("/api/projects/{slug}/memories", _api(_list_memories, remote_aware=True), methods=["GET"]),
        Route("/api/projects/{slug}/memories", _api(_create_memory, remote_aware=True), methods=["POST"]),
        Route("/api/projects/{slug}/memories/{mid}", _api(_get_memory, remote_aware=True), methods=["GET"]),
        Route("/api/projects/{slug}/memories/{mid}", _api(_update_memory, remote_aware=True), methods=["PUT"]),
        Route("/api/projects/{slug}/memories/{mid}", _api(_delete_memory, remote_aware=True), methods=["DELETE"]),
        Route("/api/projects/{slug}/memories/{mid}/provenance", _api(_provenance, remote_aware=True), methods=["GET"]),
        Route("/api/projects/{slug}/rules", _api(_rules, remote_aware=True), methods=["GET"]),
        Route("/api/projects/{slug}/rules", _api(_add_rule, remote_aware=True), methods=["POST"]),
        Route("/api/projects/{slug}/rules/{rid}", _api(_update_rule, remote_aware=True), methods=["PUT"]),
        Route("/api/projects/{slug}/rules/{rid}", _api(_delete_rule, remote_aware=True), methods=["DELETE"]),
        # Rule governance (server mode, admin only)
        Route("/api/projects/{slug}/rules/{rid}/approve", _api(_approve_rule, admin=True, remote_aware=True), methods=["POST"]),
        Route("/api/projects/{slug}/rules/{rid}/revoke", _api(_revoke_rule, admin=True, remote_aware=True), methods=["POST"]),
        Route("/api/rules/pending", _api(_pending_rules, admin=True), methods=["GET"]),
        Route("/api/org/rules", _api(_list_org_rules, admin=True), methods=["GET"]),
        Route("/api/org/rules", _api(_create_org_rule, admin=True), methods=["POST"]),
        Route("/api/org/rules/{rid}", _api(_update_org_rule, admin=True), methods=["PUT"]),
        Route("/api/org/rules/{rid}", _api(_delete_org_rule, admin=True), methods=["DELETE"]),
        # User management (server mode, admin only)
        Route("/api/users", _api(_list_users, admin=True), methods=["GET"]),
        Route("/api/users", _api(_create_user, admin=True), methods=["POST"]),
        Route("/api/users/{uid}/deactivate", _api(_deactivate_user, admin=True), methods=["POST"]),
        Route("/api/users/{uid}/rotate-token", _api(_rotate_user_token, admin=True), methods=["POST"]),
        Route("/api/projects/{slug}/sessions", _api(_sessions, remote_aware=True), methods=["GET"]),
        Route("/api/projects/{slug}/import-claude-md", _api(_import_claude_md), methods=["POST"]),
        Route("/api/projects/{slug}/sync-export", _api(_sync_export), methods=["GET"]),
        Route("/api/projects/{slug}/sync-import", _api(_sync_import), methods=["POST"]),
        Route("/api/projects/{slug}/link-folder", _api(_link_folder), methods=["POST"]),
        Route("/api/projects/{slug}/bind", _api(_bind_backend), methods=["POST"]),
        Route("/api/projects/{slug}/apply-template", _api(_apply_template), methods=["POST"]),
        Route("/api/projects/{slug}/import-rules", _api(_import_rules), methods=["POST"]),
        Route("/api/projects/{slug}/pending", _api(_pending, remote_aware=True), methods=["GET"]),
        Route("/api/projects/{slug}/pending/{mid}/adapt", _api(_adapt_pending, remote_aware=True), methods=["POST"]),
        Route("/api/projects/{slug}/pending/{mid}", _api(_discard_pending, remote_aware=True), methods=["DELETE"]),
        Route("/api/projects/{slug}/tasks", _api(_task_list), methods=["GET"]),
        Route("/api/projects/{slug}/tasks", _api(_task_create), methods=["POST"]),
        Route("/api/projects/{slug}/tasks/reorder", _api(_task_reorder), methods=["POST"]),
        Route("/api/projects/{slug}/tasks/{tid}", _api(_task_get), methods=["GET"]),
        Route("/api/projects/{slug}/tasks/{tid}", _api(_task_update), methods=["PUT"]),
        Route("/api/projects/{slug}/tasks/{tid}/comments", _api(_task_comment), methods=["POST"]),
        Route("/api/projects/{slug}/tasks/{tid}/start", _api(_task_start), methods=["POST"]),
        Route("/api/projects/{slug}/tasks/{tid}/stop", _api(_task_stop), methods=["POST"]),
        Route("/api/projects/{slug}/tasks/{tid}/done", _api(_task_done), methods=["POST"]),
        Route("/api/projects/{slug}/tasks/{tid}", _api(_task_delete), methods=["DELETE"]),
        Route("/api/projects/{slug}/tasks/{tid}/convert", _api(_task_convert), methods=["POST"]),
        Route("/api/projects/{slug}/tasks/{tid}/activity", _api(_task_activity), methods=["GET"]),
        Route("/api/projects/{slug}/tasks/{tid}/release", _api(_task_release), methods=["POST"]),
        Route("/api/projects/{slug}/tasks/{tid}/archive", _api(_task_archive), methods=["POST"]),
        Route("/api/projects/{slug}/asoode/links", _api(_asoode_links), methods=["GET"]),
        Route("/api/projects/{slug}/asoode/link", _api(_asoode_link), methods=["POST"]),
        Route("/api/projects/{slug}/asoode/push", _api(_asoode_push), methods=["POST"]),
        Route("/api/asoode", _api(_asoode_status), methods=["GET"]),
        Route("/api/asoode/boards", _api(_asoode_boards), methods=["GET"]),
        Route("/api/asoode", _api(_asoode_set_urls, admin=True), methods=["PUT"]),
        Route("/api/asoode/pat", _api(_asoode_set_pat, admin=True), methods=["POST"]),
        Route("/api/asoode/pat", _api(_asoode_clear_pat, admin=True), methods=["DELETE"]),
        Route("/api/templates", _api(_list_templates), methods=["GET"]),
        Route("/api/templates", _api(_create_template), methods=["POST"]),
        Route("/api/templates/{tid}", _api(_get_template), methods=["GET"]),
        Route("/api/templates/{tid}", _api(_update_template), methods=["PUT"]),
        Route("/api/templates/{tid}", _api(_delete_template), methods=["DELETE"]),
        Route("/api/templates/{tid}/items", _api(_add_template_item), methods=["POST"]),
        Route("/api/templates/{tid}/items/{iid}", _api(_update_template_item), methods=["PUT"]),
        Route("/api/templates/{tid}/items/{iid}", _api(_delete_template_item), methods=["DELETE"]),
    ]
    assets = _DIST / "assets"
    if assets.is_dir():
        routes.append(Mount("/assets", app=StaticFiles(directory=str(assets))))
    return routes
