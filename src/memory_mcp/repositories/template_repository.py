"""Template repository - reusable rule/memory sets in the SQLite registry.

Templates let you define a set of default rules (or other memory entries) once
and apply them when creating new projects, instead of re-typing them.
"""

from memory_mcp.db.registry import now_iso, registry_conn
from memory_mcp.exceptions import MemoryMCPError
from memory_mcp.models import Template, TemplateItem


class TemplateNotFoundError(MemoryMCPError):
    """Raised when a template id or name does not exist."""


def _item(row) -> TemplateItem:
    return TemplateItem(
        id=row["id"],
        template_id=row["template_id"],
        category=row["category"],
        title=row["title"],
        content=row["content"],
        priority=row["priority"],
    )


class TemplateRepository:
    """CRUD for templates and their items."""

    def create(self, name: str, description: str | None = None) -> Template:
        with registry_conn() as conn:
            cur = conn.execute(
                "INSERT INTO templates (name, description, created_at) VALUES (?, ?, ?)",
                (name, description, now_iso()),
            )
            template_id = cur.lastrowid
        return self.get(template_id)

    def list_all(self) -> list[Template]:
        with registry_conn() as conn:
            rows = conn.execute(
                "SELECT id, name, description, created_at FROM templates ORDER BY name"
            ).fetchall()
            items_by_tpl: dict[int, list[TemplateItem]] = {}
            for r in conn.execute(
                "SELECT id, template_id, category, title, content, priority FROM template_items"
            ).fetchall():
                items_by_tpl.setdefault(r["template_id"], []).append(_item(r))
        return [
            Template(
                id=r["id"], name=r["name"], description=r["description"],
                created_at=r["created_at"], items=items_by_tpl.get(r["id"], []),
            )
            for r in rows
        ]

    def get(self, template_id: int) -> Template:
        with registry_conn() as conn:
            row = conn.execute(
                "SELECT id, name, description, created_at FROM templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if row is None:
                raise TemplateNotFoundError(f"Template not found: {template_id}")
            items = [
                _item(r)
                for r in conn.execute(
                    "SELECT id, template_id, category, title, content, priority "
                    "FROM template_items WHERE template_id = ? ORDER BY id",
                    (template_id,),
                ).fetchall()
            ]
        return Template(
            id=row["id"], name=row["name"], description=row["description"],
            created_at=row["created_at"], items=items,
        )

    def get_by_name(self, name: str) -> Template | None:
        with registry_conn() as conn:
            row = conn.execute(
                "SELECT id FROM templates WHERE name = ?", (name,)
            ).fetchone()
        return self.get(row["id"]) if row else None

    def update(self, template_id: int, name: str | None, description: str | None) -> Template:
        self.get(template_id)  # existence check
        with registry_conn() as conn:
            if name is not None:
                conn.execute(
                    "UPDATE templates SET name = ? WHERE id = ?", (name, template_id)
                )
            if description is not None:
                conn.execute(
                    "UPDATE templates SET description = ? WHERE id = ?",
                    (description, template_id),
                )
        return self.get(template_id)

    def delete(self, template_id: int) -> None:
        with registry_conn() as conn:
            conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))

    def add_item(
        self,
        template_id: int,
        category: str,
        title: str,
        content: str,
        priority: int = 0,
    ) -> TemplateItem:
        self.get(template_id)  # existence check
        with registry_conn() as conn:
            cur = conn.execute(
                "INSERT INTO template_items (template_id, category, title, content, priority) "
                "VALUES (?, ?, ?, ?, ?)",
                (template_id, category, title, content, priority),
            )
            item_id = cur.lastrowid
            row = conn.execute(
                "SELECT id, template_id, category, title, content, priority "
                "FROM template_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        return _item(row)

    def update_item(
        self,
        item_id: int,
        category: str | None = None,
        title: str | None = None,
        content: str | None = None,
        priority: int | None = None,
    ) -> TemplateItem:
        sets: list[str] = []
        values: list = []
        for column, value in (
            ("category", category), ("title", title),
            ("content", content), ("priority", priority),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                values.append(value)
        with registry_conn() as conn:
            if sets:
                values.append(item_id)
                conn.execute(
                    f"UPDATE template_items SET {', '.join(sets)} WHERE id = ?", values
                )
            row = conn.execute(
                "SELECT id, template_id, category, title, content, priority "
                "FROM template_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            raise TemplateNotFoundError(f"Template item not found: {item_id}")
        return _item(row)

    def delete_item(self, item_id: int) -> None:
        with registry_conn() as conn:
            conn.execute("DELETE FROM template_items WHERE id = ?", (item_id,))
