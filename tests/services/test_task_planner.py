"""Decomposing one request into an ordered plan.

The boundary chosen with the user: multi-part requests only, one task per
SEPARABLE deliverable. Judgement about that lives in the model, but the parts
that can be enforced structurally are enforced here - a plan of one is not a
plan, and a description is not optional.
"""

import pytest

from memory_mcp.container import container
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.models import TaskFilter
from memory_mcp.services.task_planner import MAX_TASKS, PlanError, TaskPlanner

REQUEST = "add the endpoint, wire the UI, and write the docs"
ITEMS = [
    {"title": "Add the endpoint", "description": "POST /api/thing in web/routes.py.",
     "priority": 3},
    {"title": "Wire the UI", "description": "Call it from the Things tab.",
     "priority": 2, "labels": ["ui"]},
    {"title": "Write the docs", "description": "README section for the endpoint."},
]


@pytest.fixture
def project():
    slug = "planner-test"
    container.project_service.init_project(slug, "Planner Test")
    return slug


@pytest.fixture
def planner():
    return TaskPlanner(container.task_service)


class TestPlan:
    def test_creates_one_task_per_deliverable(self, planner, project):
        result = planner.plan(project, REQUEST, ITEMS)
        assert result["count"] == 3
        titles = [t["title"] for t in result["tasks"]]
        assert titles == ["Add the endpoint", "Wire the UI", "Write the docs"]

    def test_keeps_dependency_order(self, planner, project):
        planner.plan(project, REQUEST, ITEMS)
        tasks = container.task_service.list_tasks(project, limit=50).tasks
        by_position = sorted(tasks, key=lambda t: t.position)
        assert [t.title for t in by_position][:3] == [
            "Add the endpoint", "Wire the UI", "Write the docs",
        ]

    def test_every_task_records_the_request_verbatim(self, planner, project):
        result = planner.plan(project, REQUEST, ITEMS)
        for task in result["tasks"]:
            detail = container.task_service.detail(project, task["id"])
            assert any(REQUEST in c.body for c in detail.comments), (
                "the original wording must survive a later edit to the title"
            )

    def test_priority_and_labels_carry_through(self, planner, project):
        result = planner.plan(project, REQUEST, ITEMS)
        assert result["tasks"][0]["priority"] == 3
        assert result["tasks"][1]["labels"] == ["ui"]

    def test_tasks_are_attributed_to_claude(self, planner, project):
        result = planner.plan(project, REQUEST, ITEMS)
        assert {t["source"] for t in result["tasks"]} == {"claude"}

    def test_steps_hang_off_a_deliverable(self, planner, project):
        result = planner.plan(project, REQUEST, [
            *ITEMS,
            {"title": "Add a test", "description": "Cover the new route.",
             "parent_index": 0},
        ])
        parent_id = result["tasks"][0]["id"]
        assert result["tasks"][3]["parent_id"] == parent_id

    def test_subtasks_do_not_clutter_the_top_level(self, planner, project):
        planner.plan(project, REQUEST, [
            *ITEMS,
            {"title": "Add a test", "description": "Cover it.", "parent_index": 0},
        ])
        top = container.task_service.list_tasks(project, TaskFilter(), limit=50).tasks
        assert "Add a test" not in [t.title for t in top]


class TestTheBoundaryIsEnforced:
    """What can be checked in code is checked in code, not left to prose."""

    def test_a_single_deliverable_is_not_a_plan(self, planner, project):
        with pytest.raises(PlanError, match="memory_task_add"):
            planner.plan(project, "just do the one thing", [ITEMS[0]])

    def test_runaway_decomposition_is_capped(self, planner, project):
        many = [
            {"title": f"Step {i}", "description": "x"} for i in range(MAX_TASKS + 1)
        ]
        with pytest.raises(PlanError, match="by deliverable, not by step"):
            planner.plan(project, REQUEST, many)

    def test_a_task_without_a_description_is_rejected(self, planner, project):
        with pytest.raises(PlanError, match="without this"):
            planner.plan(project, REQUEST, [
                ITEMS[0], {"title": "Bare title", "description": ""},
            ])

    def test_a_task_without_a_title_is_rejected(self, planner, project):
        with pytest.raises(PlanError, match="no title"):
            planner.plan(project, REQUEST, [ITEMS[0], {"title": "  ", "description": "x"}])

    def test_an_empty_request_is_rejected(self, planner, project):
        with pytest.raises(PlanError, match="must not be empty"):
            planner.plan(project, "   ", ITEMS)

    def test_a_forward_parent_reference_cannot_make_a_cycle(self, planner, project):
        with pytest.raises(PlanError, match="EARLIER"):
            planner.plan(project, REQUEST, [
                {"title": "A", "description": "x", "parent_index": 1},
                {"title": "B", "description": "x"},
            ])

    def test_nothing_is_created_when_validation_fails(self, planner, project):
        with pytest.raises(PlanError):
            planner.plan(project, REQUEST, [ITEMS[0], {"title": "No desc", "description": ""}])
        assert container.task_service.list_tasks(project, limit=50).total == 0


class TestMirroring:
    def test_an_unbound_project_is_not_mirrored(self, project):
        class Bridge:
            def links(self, slug):
                return []

            def push(self, slug):  # pragma: no cover - must not be reached
                raise AssertionError("must not push an unbound project")

        planner = TaskPlanner(container.task_service, Bridge())
        assert planner.plan(project, REQUEST, ITEMS)["mirrored"] is False

    def test_a_bound_project_goes_straight_to_the_board(self, project):
        upsert_project_link(
            project, base_url="https://api.asoode.com", remote_project_id="p1",
            remote_work_package_id="wp1",
        )
        pushed = {}

        class Bridge:
            def links(self, slug):
                return [{"id": 1}]

            def push(self, slug):
                pushed["slug"] = slug
                return {"counts": {"pushed": 3, "failed": 0, "considered": 3}}

        planner = TaskPlanner(container.task_service, Bridge())
        result = planner.plan(project, REQUEST, ITEMS)
        assert result["mirrored"] is True
        assert pushed["slug"] == project

    def test_a_failed_mirror_never_loses_the_plan(self, project):
        class Bridge:
            def links(self, slug):
                return [{"id": 1}]

            def push(self, slug):
                raise RuntimeError("asoode down")

        planner = TaskPlanner(container.task_service, Bridge())
        result = planner.plan(project, REQUEST, ITEMS)
        assert result["count"] == 3, "the local queue is the record"
        assert "asoode down" in result["mirror_error"]
        assert container.task_service.list_tasks(project, limit=50).total == 3
