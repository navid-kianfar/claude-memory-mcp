"""Project repository - manages the SQLite registry's `projects` table."""

from memory_mcp.config import settings
from memory_mcp.db.registry import new_project_uid, now_iso, registry_conn
from memory_mcp.models import ProjectInfo

_COLUMNS = (
    "slug, display_name, description, created_at, last_accessed, db_path, "
    "project_path, owner, backend, remote_url, project_uid"
)


def _to_info(row) -> ProjectInfo:
    keys = row.keys()
    return ProjectInfo(
        slug=row["slug"],
        project_uid=row["project_uid"] if "project_uid" in keys else None,
        display_name=row["display_name"],
        description=row["description"],
        created_at=row["created_at"],
        last_accessed=row["last_accessed"],
        db_path=row["db_path"],
        project_path=row["project_path"],
        owner=row["owner"] if "owner" in keys else None,
        backend=(row["backend"] if "backend" in keys and row["backend"] else "local"),
        remote_url=row["remote_url"] if "remote_url" in keys else None,
    )


class ProjectRepository:
    """Registry CRUD - per-operation SQLite connection, no locks held."""

    def register(
        self,
        slug: str,
        display_name: str,
        description: str | None = None,
        db_path: str | None = None,
        project_path: str | None = None,
        owner: str | None = None,
        project_uid: str | None = None,
    ) -> ProjectInfo:
        if db_path is None:
            db_path = str(settings.projects_dir / f"{slug}.duckdb")

        with registry_conn() as conn:
            existing = conn.execute(
                "SELECT slug FROM projects WHERE slug = ?", (slug,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE projects SET display_name = ?, description = ?, "
                    "last_accessed = ? WHERE slug = ?",
                    (display_name, description, now_iso(), slug),
                )
                if project_path is not None:
                    conn.execute(
                        "UPDATE projects SET project_path = ? WHERE slug = ?",
                        (project_path, slug),
                    )
                # Owner is set once (at creation / first claim); never cleared by
                # a re-register that omits it.
                if owner is not None:
                    conn.execute(
                        "UPDATE projects SET owner = ? WHERE slug = ?",
                        (owner, slug),
                    )
            else:
                ts = now_iso()
                conn.execute(
                    f"INSERT INTO projects ({_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'local', NULL, ?)",
                    (slug, display_name, description, ts, ts, db_path, project_path,
                     owner, project_uid or new_project_uid()),
                )

        result = self.get(slug)
        if result is None:
            raise RuntimeError(f"Failed to register project '{slug}'")
        return result

    def get_by_uid(self, project_uid: str) -> ProjectInfo | None:
        """Find a project by its stable uid, whatever its slug or folder is now."""
        if not project_uid:
            return None
        with registry_conn() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM projects WHERE project_uid = ?",
                (project_uid,),
            ).fetchone()
        return _to_info(row) if row else None

    def set_uid(self, slug: str, project_uid: str) -> None:
        """Adopt a uid onto a project that does not have one yet."""
        with registry_conn() as conn:
            conn.execute(
                "UPDATE projects SET project_uid = ? WHERE slug = ?",
                (project_uid, slug),
            )

    def get(self, slug: str) -> ProjectInfo | None:
        with registry_conn() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM projects WHERE slug = ?", (slug,)
            ).fetchone()
        return _to_info(row) if row else None

    def list_all(self) -> list[ProjectInfo]:
        with registry_conn() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM projects ORDER BY last_accessed DESC"
            ).fetchall()
        return [_to_info(r) for r in rows]

    def touch(self, slug: str) -> None:
        with registry_conn() as conn:
            conn.execute(
                "UPDATE projects SET last_accessed = ? WHERE slug = ?",
                (now_iso(), slug),
            )

    def update_meta(
        self,
        slug: str,
        display_name: str | None = None,
        description: str | None = None,
    ) -> None:
        """Update a project's display name and/or description (rename)."""
        sets: list[str] = []
        values: list = []
        if display_name is not None:
            sets.append("display_name = ?")
            values.append(display_name)
        if description is not None:
            sets.append("description = ?")
            values.append(description)
        if not sets:
            return
        values.append(slug)
        with registry_conn() as conn:
            conn.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE slug = ?", values
            )

    def update_db_path(self, slug: str, db_path: str) -> None:
        with registry_conn() as conn:
            conn.execute(
                "UPDATE projects SET db_path = ? WHERE slug = ?", (db_path, slug)
            )

    def update_project_path(self, slug: str, project_path: str) -> None:
        with registry_conn() as conn:
            conn.execute(
                "UPDATE projects SET project_path = ? WHERE slug = ?",
                (project_path, slug),
            )

    def set_owner(self, slug: str, owner: str) -> None:
        with registry_conn() as conn:
            conn.execute(
                "UPDATE projects SET owner = ? WHERE slug = ?", (owner, slug)
            )

    def set_backend(
        self, slug: str, backend: str, remote_url: str | None = None
    ) -> None:
        """Route a project to 'local' (private, this machine) or 'remote' (org
        server). remote_url is required for 'remote'."""
        with registry_conn() as conn:
            conn.execute(
                "UPDATE projects SET backend = ?, remote_url = ? WHERE slug = ?",
                (backend, remote_url, slug),
            )

    def delete(self, slug: str) -> None:
        with registry_conn() as conn:
            conn.execute("DELETE FROM projects WHERE slug = ?", (slug,))
