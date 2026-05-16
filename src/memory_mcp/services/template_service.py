"""Template service - manage reusable rule/memory sets and apply them.

A template is a named set of items (rules, architecture notes, etc.). Applying
a template to a project stores each selected item as a memory, so common
defaults do not have to be re-typed for every new project.
"""

from memory_mcp.models import MemoryCategory, StoreMemoryRequest, Template
from memory_mcp.repositories import TemplateRepository
from memory_mcp.services.memory_service import MemoryService


class TemplateService:
    """CRUD for templates plus applying them to projects."""

    def __init__(self, template_repo: TemplateRepository, memory_service: MemoryService):
        self._repo = template_repo
        self._memory_service = memory_service

    # ---------- template CRUD ----------

    def list_templates(self) -> list[Template]:
        return self._repo.list_all()

    def get(self, template_id: int) -> Template:
        return self._repo.get(template_id)

    def create(self, name: str, description: str | None = None) -> Template:
        return self._repo.create(name, description)

    def update(
        self, template_id: int, name: str | None = None, description: str | None = None,
    ) -> Template:
        return self._repo.update(template_id, name, description)

    def delete(self, template_id: int) -> dict:
        self._repo.get(template_id)  # raises if missing
        self._repo.delete(template_id)
        return {"status": "ok", "deleted": template_id}

    def add_item(
        self,
        template_id: int,
        category: str,
        title: str,
        content: str,
        priority: int = 0,
    ):
        # Validate the category is a real MemoryCategory.
        MemoryCategory(category)
        priority = max(0, min(3, priority))
        return self._repo.add_item(template_id, category, title, content, priority)

    def update_item(
        self,
        item_id: int,
        category: str | None = None,
        title: str | None = None,
        content: str | None = None,
        priority: int | None = None,
    ):
        if category is not None:
            MemoryCategory(category)
        if priority is not None:
            priority = max(0, min(3, priority))
        return self._repo.update_item(item_id, category, title, content, priority)

    def delete_item(self, item_id: int) -> dict:
        self._repo.delete_item(item_id)
        return {"status": "ok", "deleted_item": item_id}

    # ---------- apply ----------

    def apply(
        self, target_project: str, template_id: int, item_ids: list[int] | None = None,
    ) -> dict:
        """Store a template's items as memories in the target project.

        When item_ids is given, only those items are applied (checkbox-style
        partial import); otherwise the whole template is applied.
        """
        template = self._repo.get(template_id)
        items = template.items
        if item_ids is not None:
            wanted = set(item_ids)
            items = [it for it in items if it.id in wanted]

        created = []
        for item in items:
            memory = self._memory_service.store(
                StoreMemoryRequest(
                    project=target_project,
                    category=item.category,
                    title=item.title,
                    content=item.content,
                    priority=item.priority,
                    source="template",
                )
            )
            created.append(memory)

        return {
            "status": "ok",
            "template": template.name,
            "applied": len(created),
            "memories": created,
        }
