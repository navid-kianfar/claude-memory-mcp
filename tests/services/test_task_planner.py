"""Decomposing one request into an ordered plan.

The boundary chosen with the user: multi-part requests only, one task per
SEPARABLE deliverable. Judgement about that lives in the model, but the parts
that can be enforced structurally are enforced here - a plan of one is not a
plan, and a description is not optional.
"""

import pytest

from memory_mcp.container import container
from memory_mcp.db.connection import connect
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

            def flush(self, slug):  # pragma: no cover - must not be reached
                raise AssertionError("must not mirror an unbound project")

        planner = TaskPlanner(container.task_service, Bridge())
        assert planner.plan(project, REQUEST, ITEMS)["mirrored"] is False

    def test_a_bound_project_goes_straight_to_the_board(self, project):
        upsert_project_link(
            project, base_url="https://api.asoode.com", remote_project_id="p1",
            remote_work_package_id="wp1",
        )
        flushed = {}

        class Bridge:
            def links(self, slug):
                return [{"id": 1}]

            def flush(self, slug):
                # The OUTBOX carries the plan: each create queued its own row
                # with every field. A full push re-POSTed the whole project.
                flushed["slug"] = slug
                return {"flushed": 3, "failed": 0, "remaining": 0}

            def push(self, slug):  # pragma: no cover - must not be reached
                raise AssertionError("a plan drains the outbox, never pushes")

        planner = TaskPlanner(container.task_service, Bridge())
        result = planner.plan(project, REQUEST, ITEMS)
        assert result["mirrored"] is True
        assert flushed["slug"] == project
        assert result["mirror_counts"] == {"flushed": 3, "failed": 0, "remaining": 0}

    def test_a_failed_mirror_never_loses_the_plan(self, project):
        class Bridge:
            def links(self, slug):
                return [{"id": 1}]

            def flush(self, slug):
                raise RuntimeError("asoode down")

        planner = TaskPlanner(container.task_service, Bridge())
        result = planner.plan(project, REQUEST, ITEMS)
        assert result["count"] == 3, "the local queue is the record"
        assert "asoode down" in result["mirror_error"]
        assert container.task_service.list_tasks(project, limit=50).total == 3


class FailsOnTask:
    """The real task service, but the Nth create blows up.

    Stands in for anything that can fail partway through a plan now that the
    original trigger is fixed: a provider error, a lock, a validation failure on
    task 7 of 9.
    """

    def __init__(self, inner, fail_on: int):
        self._inner = inner
        self._fail_on = fail_on
        self.creates = 0

    def create(self, request):
        self.creates += 1
        if self.creates == self._fail_on:
            raise RuntimeError("provider blew up")
        return self._inner.create(request)

    def comment(self, *args, **kwargs):
        return self._inner.comment(*args, **kwargs)


def count(project: str, table: str) -> int:
    with connect(project) as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


class TestAPlanIsAllOrNothing:
    """OBSERVED 2026-09-04: a plan created its first task, crashed on the second,
    and left the parent in the queue looking like a considered decomposition.
    Re-running made a SECOND parent instead of resuming."""

    def test_a_failure_partway_leaves_no_tasks_behind(self, project):
        planner = TaskPlanner(FailsOnTask(container.task_service, fail_on=3))
        with pytest.raises(PlanError):
            planner.plan(project, REQUEST, ITEMS)
        assert count(project, "tasks") == 0, "two tasks were created before the failure"

    def test_it_leaves_no_comments_or_outbox_rows_either(self, project):
        """Every repository the create path touches must roll back, not just tasks."""
        planner = TaskPlanner(FailsOnTask(container.task_service, fail_on=3))
        with pytest.raises(PlanError):
            planner.plan(project, REQUEST, ITEMS)
        assert count(project, "task_comments") == 0
        assert count(project, "task_outbox") == 0, (
            "an outbox row for a task that no longer exists would mirror a ghost"
        )
        assert count(project, "provenance") == 0

    def test_no_orphaned_parent_survives(self, project):
        """The specific damage: a parent whose steps never got created."""
        items = [*ITEMS, {"title": "Add a test", "description": "Cover it.",
                          "parent_index": 0}]
        planner = TaskPlanner(FailsOnTask(container.task_service, fail_on=4))
        with pytest.raises(PlanError):
            planner.plan(project, REQUEST, items)
        assert count(project, "tasks") == 0

    def test_the_error_says_the_plan_was_rolled_back(self, project):
        planner = TaskPlanner(FailsOnTask(container.task_service, fail_on=3))
        with pytest.raises(PlanError) as exc:
            planner.plan(project, REQUEST, ITEMS)
        message = str(exc.value)
        assert "ROLLED BACK" in message
        assert "no tasks were created" in message, (
            "'which tasks got created' must have a stated answer"
        )
        assert "task 3 of 3" in message and "Write the docs" in message
        assert "provider blew up" in message, "the underlying cause is still there"
        assert isinstance(exc.value.__cause__, RuntimeError)

    def test_a_retry_after_a_failure_creates_one_set_not_two(self, project):
        service = FailsOnTask(container.task_service, fail_on=3)
        planner = TaskPlanner(service)
        with pytest.raises(PlanError):
            planner.plan(project, REQUEST, ITEMS)
        service._fail_on = 0
        assert planner.plan(project, REQUEST, ITEMS)["count"] == 3
        assert count(project, "tasks") == 3, "re-running resumed, it did not duplicate"

    def test_positions_are_still_distinct_inside_the_transaction(self, project):
        """next_position reads uncommitted siblings on the shared connection. On a
        separate one it would be blind to them and give every task position 0."""
        planner = TaskPlanner(container.task_service)
        result = planner.plan(project, REQUEST, ITEMS)
        positions = [t["position"] for t in result["tasks"]]
        assert len(set(positions)) == 3, positions
        assert positions == sorted(positions)


class TestTheMirrorWaitsForTheCommit:
    """A ROLLBACK undoes local rows. It cannot un-POST to asoode - which is the
    shape that produced 54 duplicate cards once already."""

    @pytest.fixture
    def nudges(self, monkeypatch, project):
        seen = []
        monkeypatch.setattr(
            container.task_service, "_mirror",
            lambda slug: seen.append(count(slug, "tasks")),
        )
        return seen

    def test_it_is_not_nudged_mid_plan(self, nudges, project):
        TaskPlanner(container.task_service).plan(project, REQUEST, ITEMS)
        assert nudges == [3], (
            "one nudge, after the commit, seeing all three rows - not three "
            "nudges mid-transaction against rows that might still vanish"
        )

    def test_a_rolled_back_plan_never_nudges(self, nudges, project):
        planner = TaskPlanner(FailsOnTask(container.task_service, fail_on=3))
        with pytest.raises(PlanError):
            planner.plan(project, REQUEST, ITEMS)
        assert nudges == [], "nothing may be pushed for tasks that do not exist"

    def test_an_ordinary_create_still_nudges_immediately(self, nudges, project):
        """Outside a transaction nothing changes - this is the common path."""
        from memory_mcp.models import CreateTaskRequest

        container.task_service.create(CreateTaskRequest(
            project=project, title="One off", description="x",
        ))
        assert nudges == [1]
