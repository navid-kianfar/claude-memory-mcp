"""A rule imported from another project is inert until adapted to this one.

The failure this prevents: rules copied from another project arrive full of
that project's specifics - its component names, its paths, its stack - and get
injected into this project's rule block, where the agent follows them
confidently and wrongly. So an import lands `pending`: stored and visible, but
absent from the rule block, search, session context and the git snapshot until
an agent has rewritten it for this codebase.
"""

import pytest

from memory_mcp.constants import SYNC_CATEGORIES
from memory_mcp.container import Container
from memory_mcp.db.connection import get_connection
from memory_mcp.models import (
    MemoryCategory, MemoryFilter, Pagination, SearchRequest, StoreMemoryRequest,
)


def _search(container, project, query):
    return container.search_service.search(
        SearchRequest(project=project, query=query, min_similarity=0.0)
    )


@pytest.fixture
def container():
    return Container()


def _project(container, slug):
    container.project_repo.register(slug, slug)
    get_connection(slug).close()
    return slug


@pytest.fixture
def imported(container):
    """A frontend rule copied from `donor` into `target`, still pending."""
    donor = _project(container, "donor")
    target = _project(container, "target")
    source = container.memory_service.store(
        StoreMemoryRequest(
            project=donor, category=MemoryCategory.MANDATORY_RULES,
            title="Use AchaButton for all buttons",
            content="Import AchaButton from @acha/ui and never use a raw <button>.",
        )
    )
    result = container.memory_service.copy_memories(target, donor, [source.id])
    return target, donor, source, result["memories"][0]


class TestPendingIsInert:
    def test_not_in_the_enforced_rules(self, container, imported):
        target, *_ = imported
        assert container.rules_service.get_rules(target).total == 0

    def test_not_in_search_results(self, container, imported):
        target, *_ = imported
        assert _search(container, target, "buttons").total == 0

    def test_not_in_the_session_rules(self, container, imported):
        target, *_ = imported
        ctx = container.session_service.start(target)
        assert ctx.mandatory_rules == []
        assert len(ctx.pending_adaptations) == 1
        assert "memory_adapt_pending" in ctx.pending_instructions

    def test_not_in_the_git_snapshot(self, container, imported):
        """Committing another project's wording would push it to every teammate."""
        target, *_ = imported
        snapshot = container.sync_service.build_snapshot(target)
        assert snapshot == {}

    def test_hidden_from_the_default_listing(self, container, imported):
        target, *_ = imported
        memories, total = container.memory_repo.list(
            target, MemoryFilter(), Pagination(),
        )
        assert total == 0
        assert memories == []


class TestPendingIsVisible:
    def test_listed_as_pending(self, container, imported):
        target, *_ = imported
        pending = container.memory_service.list_pending(target)
        assert len(pending) == 1
        assert pending[0].pending is True

    def test_carries_its_origin(self, container, imported):
        target, donor, source, _ = imported
        origin = container.memory_service.list_pending(target)[0].metadata["imported_from"]
        assert origin["project"] == donor
        assert origin["memory_id"] == source.id
        assert origin["content"] == source.content  # the original, verbatim

    def test_listable_by_filter(self, container, imported):
        target, *_ = imported
        _, total = container.memory_repo.list(
            target, MemoryFilter(pending=True), Pagination(),
        )
        assert total == 1


class TestAdapting:
    def test_adapted_rule_takes_effect(self, container, imported):
        target, _, _, memory = imported
        adapted = container.memory_service.adapt_pending(
            target, memory.id,
            title="Use the shared Button component",
            content="Use this project's Button from src/components; no raw <button>.",
        )

        assert adapted.pending is False
        rules = container.rules_service.get_rules(target)
        assert rules.total == 1
        assert rules.mandatory_rules[0].content.startswith("Use this project's Button")

    def test_adapted_rule_joins_the_snapshot(self, container, imported):
        target, _, _, memory = imported
        container.memory_service.adapt_pending(
            target, memory.id, title="Local button rule", content="Use src/components/Button.",
        )
        snapshot = container.sync_service.build_snapshot(target)
        assert [m["title"] for m in snapshot["mandatory_rules"]] == ["Local button rule"]

    def test_adapted_rule_is_searchable(self, container, imported):
        target, _, _, memory = imported
        container.memory_service.adapt_pending(
            target, memory.id,
            title="Local button rule",
            content="Use the Button component from src/components everywhere.",
        )
        assert _search(container, target, "button component").total == 1

    def test_adapting_records_provenance(self, container, imported):
        target, donor, _, memory = imported
        container.memory_service.adapt_pending(
            target, memory.id, title="Local button rule", content="Use src/components/Button.",
        )
        ops = container.provenance_repo.for_memory(target, memory.id)
        adapt = [e for e in ops if e.operation == "adapt"]
        assert len(adapt) == 1
        assert adapt[0].details["from_project"] == donor

    def test_empty_text_is_rejected(self, container, imported):
        target, _, _, memory = imported
        with pytest.raises(ValueError):
            container.memory_service.adapt_pending(target, memory.id, title="x", content="  ")

    def test_adapting_a_normal_memory_is_rejected(self, container):
        """memory_update is the tool for editing a live memory."""
        project = _project(container, "plain")
        memory = container.memory_service.store(
            StoreMemoryRequest(
                project=project, category=MemoryCategory.DECISION,
                title="A decision", content="Something we decided here.",
            )
        )
        with pytest.raises(ValueError, match="not pending"):
            container.memory_service.adapt_pending(
                project, memory.id, title="t", content="c",
            )


class TestDiscarding:
    def test_discard_removes_it_from_pending(self, container, imported):
        target, _, _, memory = imported
        container.memory_service.discard_pending(target, memory.id, "not our stack")

        assert container.memory_service.list_pending(target) == []
        assert container.rules_service.get_rules(target).total == 0

    def test_discard_records_the_reason(self, container, imported):
        target, _, _, memory = imported
        container.memory_service.discard_pending(target, memory.id, "not our stack")
        ops = container.provenance_repo.for_memory(target, memory.id)
        discarded = [e for e in ops if e.operation == "discard_import"]
        assert discarded[0].details["reason"] == "not our stack"


def test_sync_categories_unchanged_by_pending():
    """Pending is a row-level gate, not a category - the snapshot shape is the same."""
    assert "session" not in SYNC_CATEGORIES
    assert "mandatory_rules" in SYNC_CATEGORIES


def test_snapshot_entries_carry_no_pending_flag(container):
    """Pending is device-local staging state; it has no place in a committed file."""
    project = _project(container, "snapshot-shape")
    container.memory_service.store(
        StoreMemoryRequest(
            project=project, category=MemoryCategory.MANDATORY_RULES,
            title="A local rule", content="Written here, for here.",
        )
    )
    entry = container.sync_service.build_snapshot(project)["mandatory_rules"][0]
    assert "pending" not in entry


class TestSessionStartLoadsEverything:
    """Session context must load completely - a top-N sample drops sprint goals
    and decisions with nothing downstream able to tell they are missing."""

    def test_all_sprint_items_load(self, container):
        project = _project(container, "many-sprints")
        for i in range(25):
            container.memory_service.store(
                StoreMemoryRequest(
                    project=project, category=MemoryCategory.SPRINT,
                    title=f"Sprint goal {i}", content=f"Goal number {i}.",
                )
            )
        assert len(container.session_service.start(project).active_sprint) == 25

    def test_all_recent_decisions_load(self, container):
        project = _project(container, "many-decisions")
        for i in range(30):
            container.memory_service.store(
                StoreMemoryRequest(
                    project=project, category=MemoryCategory.DECISION,
                    title=f"Decision {i}", content=f"We decided thing {i}.",
                )
            )
        assert len(container.session_service.start(project).recent_decisions) == 30
