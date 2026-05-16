"""Project repository - manages the SQLite registry's `projects` table."""

from memory_mcp.config import settings
from memory_mcp.db.registry import now_iso, registry_conn
from memory_mcp.models import ProjectInfo

_COLUMNS = "slug, display_name, description, created_at, last_accessed, db_path"


def _to_info(row) -> ProjectInfo:
    return ProjectInfo(
        slug=row["slug"],
        display_name=row["display_name"],
        description=row["description"],
        created_at=row["created_at"],
        last_accessed=row["last_accessed"],
        db_path=row["db_path"],
    )


class ProjectRepository:
    """Registry CRUD - per-operation SQLite connection, no locks held."""

    def register(
        self,
        slug: str,
        display_name: str,
        description: str | None = None,
        db_path: str | None = None,
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
            else:
                ts = now_iso()
                conn.execute(
                    f"INSERT INTO projects ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                    (slug, display_name, description, ts, ts, db_path),
                )

        result = self.get(slug)
        if result is None:
            raise RuntimeError(f"Failed to register project '{slug}'")
        return result

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

    def update_db_path(self, slug: str, db_path: str) -> None:
        with registry_conn() as conn:
            conn.execute(
                "UPDATE projects SET db_path = ? WHERE slug = ?", (db_path, slug)
            )

    def delete(self, slug: str) -> None:
        with registry_conn() as conn:
            conn.execute("DELETE FROM projects WHERE slug = ?", (slug,))
