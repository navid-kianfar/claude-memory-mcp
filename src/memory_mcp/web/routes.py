"""Starlette routes for the management UI: a JSON API + the single-page app.

Handlers are plain sync functions wrapped by `_api`, which reads the request
body, runs the handler in a worker thread (DuckDB calls are blocking), and
serializes the result. The daemon owns the only writable DB connections, so
the UI and the Claude clients never contend for locks.
"""

from pathlib import Path

from anyio import to_thread
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from memory_mcp import __version__
from memory_mcp.container import container
from memory_mcp.context import get_active_project, set_active_project
from memory_mcp.exceptions import (
    MemoryMCPError, MemoryNotFoundError, ProjectNotFoundError,
)
from memory_mcp.models import (
    MemoryCategory, MemoryFilter, Pagination, SearchRequest,
    StoreMemoryRequest, UpdateMemoryRequest,
)

_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"

_PLACEHOLDER = """<!doctype html>
<html><head><meta charset="utf-8"><title>Memory MCP</title></head>
<body style="font-family:system-ui,sans-serif;background:#09090b;color:#e4e4e7;padding:48px;line-height:1.6">
<h1 style="color:#fafafa">Memory MCP - UI not built</h1>
<p>The React management UI has not been built yet. Build it with:</p>
<pre style="background:#18181b;padding:14px;border-radius:8px">cd frontend &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>Or run the Docker image, which builds the UI automatically.</p>
<p>The MCP server and JSON API are unaffected by this.</p>
</body></html>"""


def _mem(memory) -> dict:
    """Serialize a Memory without the bulky embedding vector."""
    data = memory.model_dump(mode="json")
    data.pop("embedding", None)
    return data


def _api(fn):
    """Wrap a sync handler `fn(params, body, query) -> data | (data, status)`."""

    async def handler(request):
        body: dict = {}
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                body = await request.json()
            except Exception:
                body = {}
        params = dict(request.path_params)
        query = dict(request.query_params)
        try:
            result = await to_thread.run_sync(lambda: fn(params, body, query))
        except (ProjectNotFoundError, MemoryNotFoundError) as e:
            return JSONResponse({"error": str(e), "type": type(e).__name__}, status_code=404)
        except (MemoryMCPError, ValueError) as e:
            return JSONResponse({"error": str(e), "type": type(e).__name__}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e), "type": type(e).__name__}, status_code=500)
        data, status = result if isinstance(result, tuple) else (result, 200)
        return JSONResponse(data, status_code=status)

    return handler


# ---------- Handlers ----------


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


def _health(params, body, query):
    return {"status": "ok", "version": __version__}


def _meta(params, body, query):
    from memory_mcp.config import settings

    return {
        "version": __version__,
        "categories": [c.value for c in MemoryCategory],
        "rule_categories": ["mandatory_rules", "forbidden_rules"],
        "active_project": get_active_project(),
        "model": settings.embedding_model,
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
    )
    return {"status": "ok", "project": project.model_dump(mode="json")}, 201


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
    response = container.rules_service.get_rules(params["slug"])
    return {
        "mandatory_rules": [_mem(m) for m in response.mandatory_rules],
        "forbidden_rules": [_mem(m) for m in response.forbidden_rules],
        "total": response.total,
    }


def _sessions(params, body, query):
    sessions = container.session_repo.list_all(params["slug"], limit=50)
    return {"sessions": [s.model_dump(mode="json") for s in sessions]}


def _provenance(params, body, query):
    entries = container.provenance_repo.for_memory(params["slug"], params["mid"])
    return {
        "memory_id": params["mid"],
        "provenance": [e.model_dump(mode="json") for e in entries],
    }


def build_routes() -> list:
    """Return the UI + JSON API routes for mounting on the daemon."""
    routes: list = [
        Route("/", _index, methods=["GET"]),
        Route("/api/health", _api(_health), methods=["GET"]),
        Route("/api/hook/rules", _hook_rules, methods=["GET"]),
        Route("/api/meta", _api(_meta), methods=["GET"]),
        Route("/api/projects", _api(_list_projects), methods=["GET"]),
        Route("/api/projects", _api(_create_project), methods=["POST"]),
        Route("/api/active", _api(_set_active), methods=["POST"]),
        Route("/api/projects/{slug}", _api(_project_info), methods=["GET"]),
        Route("/api/projects/{slug}/memories", _api(_list_memories), methods=["GET"]),
        Route("/api/projects/{slug}/memories", _api(_create_memory), methods=["POST"]),
        Route("/api/projects/{slug}/memories/{mid}", _api(_update_memory), methods=["PUT"]),
        Route("/api/projects/{slug}/memories/{mid}", _api(_delete_memory), methods=["DELETE"]),
        Route("/api/projects/{slug}/memories/{mid}/provenance", _api(_provenance), methods=["GET"]),
        Route("/api/projects/{slug}/rules", _api(_rules), methods=["GET"]),
        Route("/api/projects/{slug}/sessions", _api(_sessions), methods=["GET"]),
        Route("/api/projects/{slug}/import-claude-md", _api(_import_claude_md), methods=["POST"]),
    ]
    assets = _DIST / "assets"
    if assets.is_dir():
        routes.append(Mount("/assets", app=StaticFiles(directory=str(assets))))
    return routes
