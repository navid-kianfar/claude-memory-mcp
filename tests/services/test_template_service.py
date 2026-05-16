"""Unit tests for TemplateService and cross-project memory copying."""

import pytest

from memory_mcp.container import Container
from memory_mcp.db.connection import get_connection
from memory_mcp.models import MemoryCategory, StoreMemoryRequest
from memory_mcp.repositories import TemplateNotFoundError


@pytest.fixture
def container():
    return Container()


def _project(container, slug):
    container.project_repo.register(slug, slug)
    get_connection(slug).close()
    return slug


class TestTemplates:
    def test_create_and_list(self, container):
        container.template_service.create("Defaults", "my defaults")
        templates = container.template_service.list_templates()
        assert len(templates) == 1
        assert templates[0].name == "Defaults"

    def test_add_items_and_get(self, container):
        t = container.template_service.create("T")
        container.template_service.add_item(
            t.id, "mandatory_rules", "Use shadcn", "Use shadcn for the UI."
        )
        container.template_service.add_item(
            t.id, "forbidden_rules", "No native els", "No native form elements."
        )
        full = container.template_service.get(t.id)
        assert len(full.items) == 2

    def test_add_item_rejects_bad_category(self, container):
        t = container.template_service.create("T")
        with pytest.raises(ValueError):
            container.template_service.add_item(t.id, "bogus", "x", "y")

    def test_apply_template_creates_memories(self, container):
        slug = _project(container, "proj-a")
        t = container.template_service.create("T")
        container.template_service.add_item(t.id, "mandatory_rules", "R1", "rule one")
        container.template_service.add_item(t.id, "forbidden_rules", "R2", "rule two")
        result = container.template_service.apply(slug, t.id)
        assert result["applied"] == 2
        assert container.rules_service.get_rules(slug).total == 2

    def test_apply_subset_by_item_ids(self, container):
        slug = _project(container, "proj-b")
        t = container.template_service.create("T")
        item1 = container.template_service.add_item(t.id, "mandatory_rules", "R1", "one")
        container.template_service.add_item(t.id, "mandatory_rules", "R2", "two")
        result = container.template_service.apply(slug, t.id, item_ids=[item1.id])
        assert result["applied"] == 1

    def test_delete_item(self, container):
        t = container.template_service.create("T")
        item = container.template_service.add_item(t.id, "mandatory_rules", "R", "x")
        container.template_service.delete_item(item.id)
        assert container.template_service.get(t.id).items == []

    def test_delete_template_cascades_items(self, container):
        t = container.template_service.create("T")
        container.template_service.add_item(t.id, "mandatory_rules", "R", "x")
        container.template_service.delete(t.id)
        assert container.template_service.list_templates() == []

    def test_get_missing_raises(self, container):
        with pytest.raises(TemplateNotFoundError):
            container.template_service.get(9999)


class TestCopyMemories:
    def test_copy_rules_between_projects(self, container):
        src = _project(container, "src")
        dst = _project(container, "dst")
        memory = container.memory_service.store(
            StoreMemoryRequest(
                project=src, category=MemoryCategory.MANDATORY_RULES,
                title="Shared rule", content="A rule shared across projects.",
            )
        )
        result = container.memory_service.copy_memories(dst, src, [memory.id])
        assert result["imported"] == 1
        assert container.rules_service.get_rules(dst).total == 1

    def test_copy_skips_missing_ids(self, container):
        src = _project(container, "src2")
        dst = _project(container, "dst2")
        result = container.memory_service.copy_memories(dst, src, ["does-not-exist"])
        assert result["imported"] == 0
        assert result["skipped"] == ["does-not-exist"]
