"""Project service - initialize and describe projects."""

from memory_mcp.config import settings
from memory_mcp.db.connection import get_connection
from memory_mcp.exceptions import ProjectNotFoundError
from memory_mcp.models import GLOBAL_PROJECT_SLUG, ProjectInfo
from memory_mcp.repositories import ProjectRepository
from memory_mcp.utils.text import slugify, validate_slug


class ProjectService:
    """Project lifecycle operations."""

    def __init__(self, project_repo: ProjectRepository):
        self._repo = project_repo

    def init_project(
        self,
        slug: str,
        display_name: str,
        description: str | None = None,
        project_path: str | None = None,
        owner: str | None = None,
    ) -> ProjectInfo:
        if not validate_slug(slug):
            slug = slugify(slug)

        # In server mode the creator owns the project (drives per-scope approval:
        # the owner's own-project rules are auto-approved). The reserved global
        # project stays system-owned (None).
        if owner is None and settings.server_mode and slug != GLOBAL_PROJECT_SLUG:
            from memory_mcp.context import current_user
            owner = current_user().id

        settings.ensure_dirs()
        project = self._repo.register(
            slug, display_name, description, project_path=project_path, owner=owner
        )

        # Ensure DB schema exists by opening + closing a connection
        conn = get_connection(slug)
        conn.close()

        return project

    def ensure_global_project(self) -> ProjectInfo:
        """Idempotently register the reserved org-wide rules project and create
        its DB. Used by the org-rules admin flow (server mode)."""
        existing = self._repo.get(GLOBAL_PROJECT_SLUG)
        if existing is not None:
            return existing
        return self.init_project(
            GLOBAL_PROJECT_SLUG, "Org-wide rules",
            description="Reserved project holding rules injected into every project.",
        )

    def bind_backend(
        self,
        slug: str,
        backend: str,
        remote_url: str | None = None,
        token: str | None = None,
    ) -> ProjectInfo:
        """Route a project to a backend. 'local' keeps it private on this machine;
        'remote' points it at an org server (its data lives there, never locally).
        Binding is always explicit - projects default to 'local' and are never
        auto-bound to remote, so private projects can't leak."""
        if backend not in ("local", "remote"):
            raise ValueError("backend must be 'local' or 'remote'")
        self.get(slug)  # raises if missing
        if backend == "remote":
            url = (remote_url or "").strip().rstrip("/")
            if not url:
                raise ValueError("remote_url is required to bind to a remote server")
            if token:
                from memory_mcp.db.registry import set_credential

                set_credential(url, token)
            self._repo.set_backend(slug, "remote", url)
        else:
            self._repo.set_backend(slug, "local", None)
        return self.get(slug)

    def link_folder(self, slug: str, project_path: str) -> ProjectInfo:
        """Bind an existing project to a source folder for git-synced memory."""
        self.get(slug)  # raises if missing
        self._repo.update_project_path(slug, project_path)
        return self.get(slug)

    def update_project(
        self,
        slug: str,
        display_name: str | None = None,
        description: str | None = None,
    ) -> ProjectInfo:
        """Rename a project / change its description."""
        self.get(slug)  # raises if missing
        self._repo.update_meta(slug, display_name, description)
        return self.get(slug)

    def list_all(self, include_global: bool = False) -> list[ProjectInfo]:
        """List real projects. The reserved __global__ project is hidden from the
        normal project list (it has its own org-rules admin view)."""
        projects = self._repo.list_all()
        if include_global:
            return projects
        return [p for p in projects if p.slug != GLOBAL_PROJECT_SLUG]

    def get(self, slug: str) -> ProjectInfo:
        project = self._repo.get(slug)
        if project is None:
            raise ProjectNotFoundError(f"Project '{slug}' not found")
        return project
