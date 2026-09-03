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

    def claim_folder(
        self,
        cwd: str,
        project_uid: str | None = None,
        slug_hint: str | None = None,
        display_name: str | None = None,
    ) -> dict:
        """Resolve which project owns `cwd`, keyed on its committed uid.

        A project's identity is the uid in its `.claude-memory/manifest.json`,
        not its path or folder name - so moving or renaming the folder rebinds
        the existing project instead of registering a second one, and a
        teammate who clones the repo gets the same identity we do.

        `action` explains what happened, for the caller's log line:
          matched   - the uid already points here; nothing changed
          rebound   - same project, new location; project_path updated
          adopted   - a project existed under this slug with no uid; it took it
          created   - the uid is new to this machine (a fresh clone)
          unclaimed - no uid to go on; fall back to path/name detection
        """
        from pathlib import Path

        folder = Path(cwd).resolve()

        if not project_uid:
            from memory_mcp.context import detect_project_from_cwd

            return {"slug": detect_project_from_cwd(str(folder)), "action": "unclaimed"}

        existing = self._repo.get_by_uid(project_uid)
        if existing is not None:
            bound = (
                Path(existing.project_path).resolve()
                if existing.project_path else None
            )
            if bound == folder:
                return {"slug": existing.slug, "action": "matched"}
            self._repo.update_project_path(existing.slug, str(folder))
            return {
                "slug": existing.slug,
                "action": "rebound",
                "previous_path": str(bound) if bound else None,
            }

        # The uid is unknown here, so this machine has never seen the snapshot.
        # A local project under the same slug is the same project when it is
        # bound to this very folder (a teammate who registered it locally before
        # pulling, or a project registered before uids existed) - the committed
        # uid then wins, because it is the identity the repository carries.
        hint = slugify(slug_hint) if slug_hint else slugify(folder.name)
        candidate = self._repo.get(hint) if hint else None
        if candidate is not None:
            bound = (
                Path(candidate.project_path).resolve()
                if candidate.project_path else None
            )
            if bound is None or bound == folder or not candidate.project_uid:
                self._repo.set_uid(candidate.slug, project_uid)
                self._repo.update_project_path(candidate.slug, str(folder))
                return {"slug": candidate.slug, "action": "adopted"}

        # The slug belongs to a different project living somewhere else, so this
        # one needs a free slug of its own.
        slug = self._free_slug(hint or "project")
        self.init_project(
            slug, display_name or folder.name, project_path=str(folder),
        )
        self._repo.set_uid(slug, project_uid)
        return {"slug": slug, "action": "created"}

    def _free_slug(self, base: str) -> str:
        """`base`, or the first `base-2`, `base-3`, ... that is unregistered."""
        if self._repo.get(base) is None:
            return base
        for n in range(2, 100):
            candidate = f"{base}-{n}"
            if self._repo.get(candidate) is None:
                return candidate
        raise ValueError(f"No free slug for '{base}'")

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
