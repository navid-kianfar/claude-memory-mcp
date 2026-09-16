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

    def test_a_sub_task_cannot_carry_sub_tasks_of_its_own(self, planner, project):
        """One level only, checked before anything is created so the message
        can name the offending item by its position in the plan."""
        with pytest.raises(PlanError, match="Sub-tasks cannot have sub-tasks"):
            planner.plan(project, REQUEST, [
                {"title": "Parent", "description": "x"},
                {"title": "Child", "description": "x", "parent_index": 0},
                {"title": "Grandchild", "description": "x", "parent_index": 1},
            ])
        assert container.task_service.list_tasks(project, limit=50).total == 0

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

    def create_routed(self, request):
        self.creates += 1
        if self.creates == self._fail_on:
            raise RuntimeError("provider blew up")
        return self._inner.create_routed(request)

    def create(self, request):
        return self.create_routed(request)[0]

    def __getattr__(self, name):
        # Everything but the create goes to the real service unchanged.
        return getattr(self._inner, name)


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


def ids_of(result: dict) -> list[str]:
    return [task["id"] for task in result["tasks"]]


class TestAPlanIsSafeToRetry:
    """OBSERVED 2026-09-04, and again 2026-09-15: a plan did all its work, its
    response was lost to a daemon restart, the caller sent it again, and a
    second identical set of tasks and board cards was created."""

    def test_the_same_plan_twice_returns_the_first_set(self, planner, project):
        first = planner.plan(project, REQUEST, ITEMS)
        again = planner.plan(project, REQUEST, ITEMS)

        assert first["deduplicated"] is False
        assert again["deduplicated"] is True
        assert ids_of(again) == ids_of(first), "same ids, in plan order"
        assert again["count"] == 3
        assert "NOTHING new was created" in again["note"]
        assert container.task_service.list_tasks(project, limit=50).total == 3

    def test_a_retry_queues_nothing_for_the_board(self, planner, project):
        planner.plan(project, REQUEST, ITEMS)
        outbox_after_first = count(project, "task_outbox")
        comments_after_first = count(project, "task_comments")
        assert outbox_after_first > 0, "the first plan must have queued its creates"

        planner.plan(project, REQUEST, ITEMS)

        assert count(project, "task_outbox") == outbox_after_first
        assert count(project, "task_comments") == comments_after_first

    def test_a_retry_that_rewords_a_description_is_still_the_same_plan(self, planner, project):
        first = planner.plan(project, REQUEST, ITEMS)
        reworded = [{**ITEMS[0], "description": "Reworded on the retry."}, *ITEMS[1:]]

        again = planner.plan(project, REQUEST, reworded)

        assert again["deduplicated"] is True
        assert ids_of(again) == ids_of(first)
        assert again["tasks"][0]["description"] == ITEMS[0]["description"], (
            "the earlier task is returned as it is; the repeat is not applied"
        )

    def test_whitespace_at_the_ends_does_not_make_a_new_plan(self, planner, project):
        first = planner.plan(project, REQUEST, ITEMS)
        padded = [{**item, "title": f"  {item['title']} "} for item in ITEMS]

        again = planner.plan(project, f"\n{REQUEST}  ", padded)

        assert ids_of(again) == ids_of(first)

    def test_a_different_title_is_a_new_plan(self, planner, project):
        first = planner.plan(project, REQUEST, ITEMS)
        retitled = [*ITEMS[:2], {**ITEMS[2], "title": "Write the changelog"}]

        second = planner.plan(project, REQUEST, retitled)

        assert second["deduplicated"] is False
        assert set(ids_of(second)).isdisjoint(ids_of(first))
        assert container.task_service.list_tasks(project, limit=50).total == 6

    def test_the_same_titles_in_another_order_are_a_new_plan(self, planner, project):
        first = planner.plan(project, REQUEST, ITEMS)

        second = planner.plan(project, REQUEST, list(reversed(ITEMS)))

        assert second["deduplicated"] is False
        assert set(ids_of(second)).isdisjoint(ids_of(first))

    def test_a_different_request_is_a_new_plan(self, planner, project):
        first = planner.plan(project, REQUEST, ITEMS)

        second = planner.plan(project, f"{REQUEST}, please", ITEMS)

        assert second["deduplicated"] is False
        assert set(ids_of(second)).isdisjoint(ids_of(first))

    def test_a_plan_is_never_deduplicated_across_projects(self, planner, project):
        container.project_service.init_project("planner-other", "Planner Other")
        first = planner.plan(project, REQUEST, ITEMS)

        other = planner.plan("planner-other", REQUEST, ITEMS)

        assert other["deduplicated"] is False
        assert set(ids_of(other)).isdisjoint(ids_of(first))
        assert container.task_service.list_tasks("planner-other", limit=50).total == 3

    def test_an_earlier_plan_with_a_deleted_task_does_not_count(self, planner, project):
        first = planner.plan(project, REQUEST, ITEMS)
        container.task_service.delete(project, ids_of(first)[1])

        second = planner.plan(project, REQUEST, ITEMS)

        assert second["deduplicated"] is False
        assert second["count"] == 3
        assert set(ids_of(second)).isdisjoint(ids_of(first))

    def test_an_earlier_plan_with_an_archived_task_does_not_count(self, planner, project):
        first = planner.plan(project, REQUEST, ITEMS)
        container.task_service.archive(project, ids_of(first)[0])

        second = planner.plan(project, REQUEST, ITEMS)

        assert second["deduplicated"] is False
        assert set(ids_of(second)).isdisjoint(ids_of(first))

    def test_finished_work_still_counts_as_the_plan(self, planner, project):
        first = planner.plan(project, REQUEST, ITEMS)
        container.task_service.done(project, ids_of(first)[0])

        again = planner.plan(project, REQUEST, ITEMS)

        assert ids_of(again) == ids_of(first)
        assert again["tasks"][0]["state"] == "done", "returned as it is now"

    def test_once_the_window_has_passed_the_plan_is_created_again(
        self, planner, project, monkeypatch,
    ):
        from memory_mcp.services import task_planner

        first = planner.plan(project, REQUEST, ITEMS)
        monkeypatch.setattr(task_planner, "PLAN_RETRY_WINDOW_SECONDS", 0)

        second = planner.plan(project, REQUEST, ITEMS)

        assert second["deduplicated"] is False
        assert set(ids_of(second)).isdisjoint(ids_of(first))

    def test_a_plan_record_past_the_window_is_pruned_by_the_next_plan(
        self, planner, project,
    ):
        """A record that can no longer match is dead weight; the table must not
        grow by one row per plan for the life of the project."""
        planner.plan(project, REQUEST, ITEMS)
        with connect(project) as conn:
            conn.execute(
                "UPDATE task_plans SET created_at = created_at - INTERVAL 31 MINUTE"
            )
        other_items = [{**item, "title": item["title"] + " again"} for item in ITEMS]

        planner.plan(project, REQUEST, other_items)

        assert count(project, "task_plans") == 1

    def test_a_retry_after_a_rolled_back_plan_is_not_a_retry(self, project):
        """The record rolls back with the plan: a retry must create the set,
        not answer with the ids of tasks that were never committed."""
        service = FailsOnTask(container.task_service, fail_on=2)
        planner = TaskPlanner(service)
        with pytest.raises(PlanError):
            planner.plan(project, REQUEST, ITEMS)
        service._fail_on = 0

        result = planner.plan(project, REQUEST, ITEMS)

        assert result["deduplicated"] is False
        assert count(project, "tasks") == 3

    def test_sub_tasks_come_back_in_plan_order(self, planner, project):
        items = [*ITEMS, {"title": "Add a test", "description": "Cover it.",
                          "parent_index": 0}]
        first = planner.plan(project, REQUEST, items)

        again = planner.plan(project, REQUEST, items)

        assert ids_of(again) == ids_of(first)
        assert again["tasks"][3]["parent_id"] == ids_of(first)[0]

    def test_a_bound_retry_drains_the_queue_but_creates_nothing(self, project):
        class Bridge:
            flushes = 0

            def links(self, slug):
                return [{"id": 1}]

            def flush(self, slug):
                Bridge.flushes += 1
                return {"flushed": 0, "failed": 0, "remaining": 0}

        planner = TaskPlanner(container.task_service, Bridge())
        first = planner.plan(project, REQUEST, ITEMS)
        outbox_after_first = count(project, "task_outbox")

        again = planner.plan(project, REQUEST, ITEMS)

        assert again["deduplicated"] is True and again["mirrored"] is True
        assert ids_of(again) == ids_of(first)
        assert Bridge.flushes == 2
        assert count(project, "task_outbox") == outbox_after_first

    def test_the_tool_reports_the_retry(self, project):
        from memory_mcp import server

        tasks = [dict(item) for item in ITEMS]
        first = server.memory_task_plan(request=REQUEST, tasks=tasks, project=project)
        again = server.memory_task_plan(request=REQUEST, tasks=tasks, project=project)

        assert first["deduplicated"] is False
        assert again["deduplicated"] is True
        assert ids_of(again) == ids_of(first)


class TestAnExistingDatabaseUpgradesIntoIt:
    """The migration run against a database that already holds work, through
    the ordinary open path - not a fixture built at the new version."""

    def test_a_v15_project_gains_retry_safe_plans(self, planner, project):
        import memory_mcp.db.connection as connection_module
        from memory_mcp.models import CreateTaskRequest

        kept = container.task_service.create(CreateTaskRequest(
            project=project, title="Made before the upgrade", description="x",
        ))
        with connect(project) as conn:
            conn.execute("DROP TABLE task_plans")
            conn.execute("DELETE FROM schema_version WHERE version >= 16")
            conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (15)")
        # The next open is a first open in this process, so it migrates.
        connection_module._initialized_dbs.clear()

        first = planner.plan(project, REQUEST, ITEMS)
        again = planner.plan(project, REQUEST, ITEMS)

        from memory_mcp.db.schema import CURRENT_SCHEMA_VERSION

        with connect(project) as conn:
            version = conn.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        assert version == CURRENT_SCHEMA_VERSION
        assert ids_of(again) == ids_of(first)
        assert container.task_service.get(project, kept.id).title == "Made before the upgrade"
        assert container.task_service.list_tasks(project, limit=50).total == 4
