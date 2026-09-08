"""The standard column and role-label scheme, across EVERY linked board.

The bug these exist for: `column_plan` and `ensure_board_columns` both called
`get_default_project_link`, so on a project with a board per app they styled
one board and reported success. The asoode project itself had eight boards
with unstyled columns and no Cancelled while the tool said it was done.
"""

import pytest

from memory_mcp.container import container
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.providers import Capabilities, Group, ProviderError
from memory_mcp.services.task_bridge import BOARD_COLUMNS, TaskBridge
from tests.providers.fakes import STATES, FakeProvider


class StyleableProvider(FakeProvider):
    """A FakeProvider that admits to supporting column styling and role labels.

    The base fake leaves `supports_group_style` at its default False, which
    makes `_ensure_columns` a no-op — a test using it would pass without
    exercising anything.
    """

    def __init__(self, *, unreachable: set[str] | None = None):
        super().__init__()
        self.unreachable = unreachable or set()
        self.role_labels: dict[str, dict[str, str]] = {}
        self.recoloured: list[tuple[str, str, str]] = []

    @property
    def capabilities(self):
        return Capabilities(
            supports_external_ref=True, supports_comments=True,
            supports_groups=True, supports_group_style=True,
            supports_independent_state=True, supports_time_tracking=True,
            supports_attachments=True, supports_archive=True,
            supports_change_feed=True, supports_labels=True,
            supports_fields=True, supports_assignees=True,
            supports_subtasks=True, states=STATES,
        )

    def fetch_container(self, container_id):
        if container_id in self.unreachable:
            raise ProviderError(f"board {container_id} is gone")
        return super().fetch_container(container_id)

    # -- role labels, mirroring AsoodeProvider's two methods ----------------
    def role_label_plan(self, container_id):
        from memory_mcp.providers.asoode import role_color

        out = []
        for title, colour in (self.role_labels.get(container_id) or {}).items():
            want = role_color(title.removeprefix("agent:"))
            if (colour or "").lower() != want.lower():
                out.append({"id": f"{container_id}:{title}", "title": title,
                            "from": colour or None, "to": want})
        return out

    def ensure_role_label_colors(self, container_id):
        fixed = self.role_label_plan(container_id)
        for item in fixed:
            self.role_labels[container_id][item["title"]] = item["to"]
            self.recoloured.append((container_id, item["title"], item["to"]))
        return fixed


def _seed(provider, container_id, groups):
    provider.seed(container_id=container_id, title=container_id,
                  space_id="p1", groups=groups)


def _link(slug, wp, label, is_default=False):
    return upsert_project_link(
        slug, base_url="https://api.asoode.com", remote_project_id="p1",
        remote_work_package_id=wp, label=label, is_default=is_default,
        default_list_id="l-todo",
        state_list_map={"todo": "l-todo", "done": "l-done"},
    )


FULL = (("l-backlog", "Backlog"), ("l-todo", "To Do"),
        ("l-doing", "In Progress"), ("l-done", "Done"))


@pytest.fixture
def slug():
    s = "columns-test"
    container.project_service.init_project(s, "Columns Test")
    return s


def _bridge(provider):
    return TaskBridge(container.project_service, container.task_service, provider)


class TestEveryLinkedBoard:
    """The regression: one board styled, the rest silently skipped."""

    def test_plan_covers_all_boards_not_just_the_default(self, slug):
        p = StyleableProvider()
        _seed(p, "wp-a", FULL)
        _seed(p, "wp-b", FULL)
        _link(slug, "wp-a", "alpha", is_default=True)
        _link(slug, "wp-b", "beta")

        plan = _bridge(p).column_plan(slug)

        assert {row["board"] for row in plan} == {"alpha", "beta"}

    def test_apply_covers_all_boards(self, slug):
        p = StyleableProvider()
        _seed(p, "wp-a", FULL)
        _seed(p, "wp-b", FULL)
        _link(slug, "wp-a", "alpha", is_default=True)
        _link(slug, "wp-b", "beta")

        result = _bridge(p).ensure_board_columns(slug)

        assert [b["board"] for b in result["boards"]] == ["alpha", "beta"]
        for board in result["boards"]:
            assert board["added"] == ["Cancelled"]

    def test_the_non_default_board_really_gets_its_colours(self, slug):
        """Not just reported — the columns on the second board are painted."""
        p = StyleableProvider()
        _seed(p, "wp-b", FULL)
        _link(slug, "wp-a-missing", "alpha", is_default=True)
        _seed(p, "wp-a-missing", FULL)
        _link(slug, "wp-b", "beta")

        _bridge(p).ensure_board_columns(slug)

        painted = {g.title: g.color for g in p.fetch_container("wp-b").groups}
        assert painted == dict(BOARD_COLUMNS)

    def test_an_unlinked_project_is_rejected(self, slug):
        with pytest.raises(ProviderError, match="not linked"):
            _bridge(StyleableProvider()).column_plan(slug)


class TestItNeverRepaintsAColumn:
    def test_a_chosen_colour_survives(self, slug):
        p = StyleableProvider()
        _seed(p, "wp-a", FULL)
        p._containers["wp-a"]["groups"] = [
            Group(id="l-done", title="Done", color="#123456")
            if g.id == "l-done" else g
            for g in p._containers["wp-a"]["groups"]
        ]
        _link(slug, "wp-a", "alpha", is_default=True)

        _bridge(p).ensure_board_columns(slug)

        done = next(g for g in p.fetch_container("wp-a").groups if g.title == "Done")
        assert done.color == "#123456"

    def test_the_plan_says_so_before_applying(self, slug):
        p = StyleableProvider()
        _seed(p, "wp-a", FULL)
        p._containers["wp-a"]["groups"] = [
            Group(id="l-done", title="Done", color="#123456")
            if g.id == "l-done" else g
            for g in p._containers["wp-a"]["groups"]
        ]
        _link(slug, "wp-a", "alpha", is_default=True)

        plan = _bridge(p).column_plan(slug)

        done = next(r for r in plan if r.get("column") == "Done")
        assert done["action"] == "keep" and done["color"] == "#123456"


class TestOneBadBoardDoesNotStopTheRest:
    def test_apply_reports_the_error_and_still_does_the_others(self, slug):
        p = StyleableProvider(unreachable={"wp-gone"})
        _seed(p, "wp-ok", FULL)
        _link(slug, "wp-gone", "gone", is_default=True)
        _link(slug, "wp-ok", "ok")

        result = _bridge(p).ensure_board_columns(slug)

        by_board = {b["board"]: b for b in result["boards"]}
        assert "error" in by_board["gone"]
        assert by_board["ok"]["added"] == ["Cancelled"]

    def test_plan_reports_the_error_and_still_plans_the_others(self, slug):
        p = StyleableProvider(unreachable={"wp-gone"})
        _seed(p, "wp-ok", FULL)
        _link(slug, "wp-gone", "gone", is_default=True)
        _link(slug, "wp-ok", "ok")

        plan = _bridge(p).column_plan(slug)

        assert any(r["board"] == "gone" and "error" in r for r in plan)
        assert any(r["board"] == "ok" and r.get("column") == "Cancelled" for r in plan)


class TestRoleLabels:
    """`agent:backend` is the same red on every board — that is a convention,
    unlike an ordinary label's colour, which is somebody's choice."""

    def test_an_off_convention_role_label_is_repainted(self, slug):
        p = StyleableProvider()
        _seed(p, "wp-a", FULL)
        p.role_labels["wp-a"] = {"agent:backend": "#6366f1"}
        _link(slug, "wp-a", "alpha", is_default=True)

        result = _bridge(p).ensure_board_columns(slug)

        assert p.role_labels["wp-a"]["agent:backend"] == "#f44336"
        assert result["boards"][0]["labels_recoloured"] == [
            {"title": "agent:backend", "from": "#6366f1", "to": "#f44336"},
        ]

    def test_a_correct_role_label_is_left_alone(self, slug):
        p = StyleableProvider()
        _seed(p, "wp-a", FULL)
        p.role_labels["wp-a"] = {"agent:backend": "#f44336"}
        _link(slug, "wp-a", "alpha", is_default=True)

        _bridge(p).ensure_board_columns(slug)

        assert p.recoloured == []

    def test_the_plan_lists_role_label_changes(self, slug):
        p = StyleableProvider()
        _seed(p, "wp-a", FULL)
        p.role_labels["wp-a"] = {"agent:frontend": "#6366f1"}
        _link(slug, "wp-a", "alpha", is_default=True)

        plan = _bridge(p).column_plan(slug)

        row = next(r for r in plan if r.get("label") == "agent:frontend")
        assert row["action"] == "recolour" and row["color"] == "#ff9800"

    def test_a_provider_without_role_label_support_is_skipped(self, slug):
        """The base fake has neither method; the bridge must not blow up."""
        p = FakeProvider()
        _seed(p, "wp-a", FULL)
        _link(slug, "wp-a", "alpha", is_default=True)

        result = _bridge(p).ensure_board_columns(slug)

        assert result["boards"][0]["labels_recoloured"] == []


class TestStateMapRefresh:
    """A column added AFTER bootstrap must reach the state -> column map.

    The map was built once by `build_state_list_map` during bootstrap and never
    refreshed. On the real board that meant a Cancelled column created on
    2026-09-05 sat there unused: `_apply_state` looked up `cancelled`, found no
    target list, and left the card in whatever column it was already in - which
    is step 10 of the user's process silently not happening.
    """

    def test_a_column_added_later_lands_in_the_map(self, slug):
        provider = StyleableProvider()
        _seed(provider, "wp1", FULL)
        link = _link(slug, "wp1", "Board", is_default=True)
        bridge = _bridge(provider)

        assert "cancelled" not in (link["state_list_map"] or {})

        provider.ensure_group("wp1", "Cancelled", "#f44336")
        result = bridge.refresh_state_map(slug)

        assert result["changed"] is True
        assert "cancelled" in result["added"]
        assert result["state_list_map"]["cancelled"]

    def test_it_is_persisted_not_just_returned(self, slug):
        """The next session reads the link, not this return value."""
        from memory_mcp.db.registry import get_default_project_link

        provider = StyleableProvider()
        _seed(provider, "wp1", FULL)
        _link(slug, "wp1", "Board", is_default=True)
        bridge = _bridge(provider)

        provider.ensure_group("wp1", "Cancelled", "#f44336")
        bridge.refresh_state_map(slug)

        stored = get_default_project_link(slug)["state_list_map"]
        assert "cancelled" in stored

    def test_no_change_reports_no_change(self, slug):
        """So a caller can tell a refresh that mattered from one that did not."""
        provider = StyleableProvider()
        _seed(provider, "wp1", FULL)
        _link(slug, "wp1", "Board", is_default=True)
        bridge = _bridge(provider)

        bridge.refresh_state_map(slug)          # settle whatever FULL implies
        again = bridge.refresh_state_map(slug)
        assert again["changed"] is False

    def test_applying_the_column_scheme_refreshes_the_map(self, slug):
        """The operation that CREATES the staleness must not leave it behind."""
        provider = StyleableProvider()
        _seed(provider, "wp1", FULL)          # no Cancelled column
        _link(slug, "wp1", "Board", is_default=True)
        bridge = _bridge(provider)

        result = bridge.ensure_board_columns(slug)

        board = result["boards"][0]
        assert "Cancelled" in board["added"]
        assert board["state_map_refreshed"] is True
        assert "cancelled" in board["state_list_map"], (
            "a Cancelled column was created and the map still cannot find it"
        )

    def test_a_cancelled_task_now_has_a_column_to_move_to(self, slug):
        """The end the whole task is for, checked at the map level."""
        provider = StyleableProvider()
        _seed(provider, "wp1", FULL)
        _link(slug, "wp1", "Board", is_default=True)
        bridge = _bridge(provider)

        bridge.ensure_board_columns(slug)

        from memory_mcp.db.registry import get_default_project_link

        target = get_default_project_link(slug)["state_list_map"].get("cancelled")
        assert target, "cancelled has no column"
        titles = {g.id: g.title for g in provider.fetch_container("wp1").groups}
        assert titles[target] == "Cancelled"


class TestDeleteClosesTheCard:
    """A deleted task's card must not stay archived in a working state.

    Found by auditing the live board on 2026-09-08: 22 archived cards were
    sitting at todo or in_progress. asoode has no delete route, so a local
    delete archives the card - and archiving alone left the state untouched, so
    every task deleted mid-flight became a card that claimed forever that it was
    still waiting to be worked.

    `cancelled`, not `done`: it was deleted, not finished. Tidying the counts by
    recording something untrue is worse than the stuck state it replaces.
    """

    def _deleted(self, slug, provider, title="Scratch"):
        """Create a task, mirror it, then delete it - returning the remote id."""
        from memory_mcp.db.registry import get_default_project_link
        from memory_mcp.models import CreateTaskRequest

        # The shared _bridge() helper passes no outbox, so nothing would record
        # the local -> remote mapping and the delete would have nothing to close.
        bridge = TaskBridge(
            container.project_service, container.task_service, provider,
            outbox_repo=container.outbox_repo,
        )
        task = container.task_service.create(
            CreateTaskRequest(project=slug, title=title)
        )
        bridge.push(slug)
        link_id = get_default_project_link(slug)["id"]
        remote = container.outbox_repo.remote_id(slug, task.id, link_id)
        container.task_service.delete(slug, task.id)
        bridge.flush(slug)
        return remote

    def test_the_card_is_cancelled_not_left_in_progress(self, slug):
        from memory_mcp.models import TaskState

        provider = StyleableProvider()
        _seed(provider, "wp1", FULL)
        _link(slug, "wp1", "Board", is_default=True)

        remote = self._deleted(slug, provider)
        assert remote, "the task never reached the board, so this proves nothing"
        assert provider._tasks[remote]["state"] == TaskState.CANCELLED.value, (
            f"archived card left at {provider._tasks[remote]['state']!r}"
        )

    def test_it_is_still_archived(self, slug):
        """Closing must not replace archiving - the re-import guard needs both."""
        provider = StyleableProvider()
        _seed(provider, "wp1", FULL)
        _link(slug, "wp1", "Board", is_default=True)

        remote = self._deleted(slug, provider)
        assert provider._tasks[remote]["archived"] is True
