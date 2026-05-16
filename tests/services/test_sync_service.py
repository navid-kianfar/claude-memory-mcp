"""Unit tests for SyncService - snapshot build and reconcile (import)."""

import pytest

from memory_mcp.container import Container
from memory_mcp.db.connection import get_connection
from memory_mcp.models import MemoryCategory, StoreMemoryRequest


@pytest.fixture
def container():
    return Container()


def _project(container, slug):
    container.project_repo.register(slug, slug)
    get_connection(slug).close()
    return slug


def _store(container, project, category, title, content):
    return container.memory_service.store(
        StoreMemoryRequest(
            project=project, category=MemoryCategory(category),
            title=title, content=content,
        )
    )


class TestBuildSnapshot:
    def test_groups_by_category_without_embedding(self, container):
        p = _project(container, "p1")
        _store(container, p, "mandatory_rules", "R1", "rule one")
        _store(container, p, "decision", "D1", "decision one")

        snap = container.sync_service.build_snapshot(p)
        assert set(snap.keys()) == {"mandatory_rules", "decision"}
        assert snap["mandatory_rules"][0]["title"] == "R1"
        assert "embedding" not in snap["mandatory_rules"][0]

    def test_session_category_excluded(self, container):
        p = _project(container, "p2")
        _store(container, p, "session", "S1", "session note")
        _store(container, p, "decision", "D1", "d")

        snap = container.sync_service.build_snapshot(p)
        assert "session" not in snap
        assert "decision" in snap


class TestApplySnapshot:
    def test_round_trip_into_fresh_project(self, container):
        src = _project(container, "src")
        _store(container, src, "mandatory_rules", "R1", "rule one")
        _store(container, src, "architecture", "A1", "arch note")
        snap = container.sync_service.build_snapshot(src)

        dst = _project(container, "dst")
        result = container.sync_service.apply_snapshot(dst, snap, list(snap.keys()))
        assert result["added"] == 2
        assert container.rules_service.get_rules(dst).total == 1

    def test_updates_changed_memory(self, container):
        p = _project(container, "u1")
        memory = _store(container, p, "decision", "D1", "original")
        snap = container.sync_service.build_snapshot(p)
        snap["decision"][0]["content"] = "changed content"

        result = container.sync_service.apply_snapshot(p, snap, ["decision"])
        assert result["updated"] == 1
        assert container.memory_repo.get_by_id(p, memory.id).content == "changed content"

    def test_deletes_memory_absent_from_snapshot(self, container):
        p = _project(container, "d1")
        memory = _store(container, p, "decision", "D1", "to be removed")

        result = container.sync_service.apply_snapshot(p, {}, ["decision"])
        assert result["deleted"] == 1
        assert container.memory_repo.get_by_id(p, memory.id) is None

    def test_unreconciled_category_is_left_untouched(self, container):
        p = _project(container, "x1")
        memory = _store(container, p, "decision", "D1", "keep me")

        # Only mandatory_rules is reconciled; the decision must survive.
        container.sync_service.apply_snapshot(p, {}, ["mandatory_rules"])
        assert container.memory_repo.get_by_id(p, memory.id) is not None
