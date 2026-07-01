"""HTTP client used by the local daemon to serve remote-bound projects.

When a project's backend is 'remote', its data lives on an org server, not on
this machine. The MCP tools call these methods (mapping to the org server's JSON
API) instead of the local repositories, so a remote project's memories/rules are
never read from or written to local storage. The JSON API's own auth applies via
the stored bearer token.
"""

import httpx

from memory_mcp.db.registry import get_credential
from memory_mcp.models import ProjectInfo


class RemoteError(Exception):
    """A remote org-server call failed (unreachable or non-2xx)."""


class RemoteBackend:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 20.0):
        self._base = base_url.rstrip("/")
        self._headers = {"Accept": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._timeout = timeout

    # ---------- transport ----------

    def _req(self, method: str, path: str, *, params=None, json=None) -> dict:
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.request(
                    method, self._base + path,
                    params=params, json=json, headers=self._headers,
                )
        except httpx.HTTPError as e:
            raise RemoteError(f"remote server unreachable: {e}") from e
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("error")
            except Exception:  # noqa: BLE001
                msg = resp.text
            raise RemoteError(msg or f"remote error {resp.status_code}")
        return resp.json() if resp.content else {}

    def _p(self, slug: str) -> str:
        from urllib.parse import quote

        return f"/api/projects/{quote(slug, safe='')}"

    # ---------- memories ----------

    def store(self, slug, category, title, content, tags=None, priority=0,
              metadata=None, source="assistant") -> dict:
        return self._req("POST", f"{self._p(slug)}/memories", json={
            "category": category, "title": title, "content": content,
            "tags": tags or [], "priority": priority, "metadata": metadata,
            "source": source,
        })

    def list(self, slug, *, q=None, category=None, status="active",
             limit=50, offset=0) -> dict:
        params = {"status": status, "limit": limit, "offset": offset}
        if q:
            params["q"] = q
        if category:
            params["category"] = category
        return self._req("GET", f"{self._p(slug)}/memories", params=params)

    def get_memory(self, slug, memory_id) -> dict:
        return self._req("GET", f"{self._p(slug)}/memories/{memory_id}")

    def update_memory(self, slug, memory_id, fields: dict) -> dict:
        return self._req("PUT", f"{self._p(slug)}/memories/{memory_id}", json=fields)

    def delete_memory(self, slug, memory_id, *, hard=False) -> dict:
        return self._req(
            "DELETE", f"{self._p(slug)}/memories/{memory_id}",
            params={"hard": "true"} if hard else None,
        )

    # ---------- rules ----------

    def get_rules(self, slug) -> dict:
        return self._req("GET", f"{self._p(slug)}/rules")

    def add_rule(self, slug, rule_type, title, content, priority=2) -> dict:
        return self._req("POST", f"{self._p(slug)}/rules", json={
            "rule_type": rule_type, "title": title, "content": content,
            "priority": priority,
        })

    def update_rule(self, slug, rid, *, title=None, content=None) -> dict:
        return self._req("PUT", f"{self._p(slug)}/rules/{rid}", json={
            "title": title, "content": content,
        })

    def delete_rule(self, slug, rid, *, hard=False) -> dict:
        return self._req(
            "DELETE", f"{self._p(slug)}/rules/{rid}",
            params={"hard": "true"} if hard else None,
        )

    def approve_rule(self, slug, rid) -> dict:
        return self._req("POST", f"{self._p(slug)}/rules/{rid}/approve")

    def revoke_rule(self, slug, rid) -> dict:
        return self._req("POST", f"{self._p(slug)}/rules/{rid}/revoke")

    # ---------- project ----------

    def project_info(self, slug) -> dict:
        return self._req("GET", self._p(slug))


def for_project(project: ProjectInfo) -> RemoteBackend:
    """Build a backend client for a remote-bound project using its stored token."""
    return RemoteBackend(project.remote_url, get_credential(project.remote_url))
