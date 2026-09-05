"""asoode held to the same contract as every other platform.

The fake below mimics asoode's SHAPES, not its HTTP: work packages holding lists
holding tasks, state as an integer ordinal, externalRef unique per parent
returning the existing row. The provider's job is translating exactly those into
the shared vocabulary, so a fake at this level tests the translation and nothing
else.
"""

import pytest

from memory_mcp.asoode_client import AsoodeError
from memory_mcp.providers import AsoodeProvider, ProviderError
from memory_mcp.providers.asoode import (
    _CAPABILITIES, LABEL_PALETTE, ROLE_COLORS, _utc_iso, role_color,
)
from tests.providers.conformance import ProviderConformance


class FakeAsoodeClient:
    """asoode's data model in dictionaries, ordinals and all."""

    def __init__(self):
        self.projects: list[dict] = []
        self.boards: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.repositions: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.spent: list[tuple[str, str, str | None]] = []
        self.attached: list[tuple[str, str, bytes]] = []
        self.archived: list[tuple[str, bool]] = []
        self.lists_archived: list[str] = []
        self.change_queries: list[tuple] = []
        self.labels_added: list[tuple] = []
        self.labels_removed: list[tuple] = []
        self.labels_created: list[tuple] = []
        self.lists_created: list[tuple] = []
        self.lists_edited: list[tuple] = []
        self._n = 0

    def _next(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    # -- projects --
    def list_projects(self):
        return [
            {**p, "workPackages": [b for b in self.boards.values()
                                   if b["projectId"] == p["id"]]}
            for p in self.projects
        ]

    def create_project(self, title, description="", **kw):
        project = {"id": self._next("p"), "title": title}
        self.projects.append(project)
        return project

    # -- work packages --
    def list_work_packages(self, project_id=None):
        return [
            {"id": b["id"], "title": b["title"], "external_ref": b.get("externalRef"),
             "project_id": b["projectId"], "project_title": "Fake"}
            for b in self.boards.values()
            if project_id is None or b["projectId"] == project_id
        ]

    def find_work_package(self, external_ref):
        for b in self.boards.values():
            if b.get("externalRef") and b["externalRef"] == external_ref:
                return b
        return None

    def fetch_work_package(self, package_id):
        return self.boards.get(package_id)

    def create_work_package(self, project_id, title, *, description="",
                            external_ref=None, board_template=5):
        if external_ref:
            existing = self.find_work_package(external_ref)
            if existing:
                return existing
        bid = self._next("wp")
        self.boards[bid] = {
            "id": bid, "title": title, "projectId": project_id,
            "externalRef": external_ref,
            "lists": [
                {"id": f"{bid}-l1", "title": "To Do", "tasks": []},
                {"id": f"{bid}-l2", "title": "In Progress", "tasks": []},
                {"id": f"{bid}-l3", "title": "Done", "tasks": []},
            ],
        }
        return self.boards[bid]

    # -- lists --
    def create_list(self, package_id, title, color="", dark_color=False):
        board = self.boards[package_id]
        row = {"id": self._next("l"), "title": title, "color": color, "tasks": []}
        board["lists"].append(row)
        self.lists_created.append((package_id, title, color))
        return row

    def edit_list(self, list_id, *, title=None, color=None, dark_color=None):
        _, lst = self._list_of(list_id)
        if title is not None:
            lst["title"] = title
        if color is not None:
            lst["color"] = color
        self.lists_edited.append((list_id, title, color))
        return lst

    # -- tasks --
    def _list_of(self, list_id):
        for board in self.boards.values():
            for lst in board["lists"]:
                if lst["id"] == list_id:
                    return board, lst
        raise AsoodeError(f"no list {list_id}")

    def create_task(self, list_id, title, *, description="", external_ref=None, **kw):
        board, lst = self._list_of(list_id)
        if external_ref:
            for t in self.tasks.values():
                if t.get("externalRef") == external_ref and t["boardId"] == board["id"]:
                    return t
        tid = self._next("t")
        task = {"id": tid, "title": title, "description": description, "state": 1,
                "externalRef": external_ref, "boardId": board["id"], "listId": list_id}
        self.tasks[tid] = task
        lst["tasks"].append(task)
        return task

    def change_title(self, task_id, title):
        if task_id not in self.tasks:
            raise AsoodeError(f"no task {task_id}")
        self.tasks[task_id]["title"] = title

    def change_description(self, task_id, description):
        if task_id not in self.tasks:
            raise AsoodeError(f"no task {task_id}")
        self.tasks[task_id]["description"] = description

    def change_state(self, task_id, state):
        from memory_mcp.asoode_client import STATE_TO_ORDINAL

        if task_id not in self.tasks:
            raise AsoodeError(f"no task {task_id}")
        self.tasks[task_id]["state"] = STATE_TO_ORDINAL[state]

    def reposition(self, task_id, list_id, order=0):
        if task_id not in self.tasks:
            raise AsoodeError(f"no task {task_id}")
        self.repositions.append((task_id, list_id))

    def comment(self, task_id, message, private=False):
        if task_id not in self.tasks:
            raise AsoodeError(f"no task {task_id}")
        self.comments.append((task_id, message))

    def attach(self, task_id, filename, content, content_type=None):
        if task_id not in self.tasks:
            raise AsoodeError(f"no task {task_id}")
        self.attached.append((task_id, filename, content))

    def create_label(self, package_id, title, color="#9e9e9e"):
        lid = self._next("lbl")
        self.boards[package_id].setdefault("labels", []).append(
            {"id": lid, "title": title, "color": color}
        )
        self.labels_created.append((package_id, title, color))
        return {"id": lid, "title": title, "color": color}

    def task_detail(self, task_id):
        return self.tasks[task_id]

    def add_task_label(self, task_id, label_id):
        # Actually ATTACH it. The fake used to only record the call, so a
        # provider that re-added a label it had already attached looked fine
        # here and returned "already exists" against the real asoode.
        self.labels_added.append((task_id, label_id))
        labels = self.tasks[task_id].setdefault("labels", [])
        if any(l["id"] == label_id for l in labels):
            raise AsoodeError("already exists")
        board = self.boards[self.tasks[task_id]["boardId"]]
        title = next(
            (l["title"] for l in board.get("labels") or [] if l["id"] == label_id), "",
        )
        labels.append({"id": label_id, "title": title})

    def remove_task_label(self, task_id, label_id):
        self.labels_removed.append((task_id, label_id))
        labels = self.tasks[task_id].setdefault("labels", [])
        self.tasks[task_id]["labels"] = [l for l in labels if l["id"] != label_id]

    def task_changes(self, since, cursor=None, take=None, package_id=None):
        """One page, no cursor - the end. Records what was asked."""
        self.change_queries.append((since, cursor, package_id))
        return {
            "changes": [
                {"packageId": t.get("packageId") or t.get("workPackageId"), "id": tid}
                for tid, t in self.tasks.items()
            ],
            "syncedAt": "2026-01-02T00:00:00Z",
        }

    def archive_task(self, task_id, archived=True):
        self.tasks[task_id]["archivedAt"] = "now" if archived else None
        self.archived.append((task_id, archived))

    def archive_list_tasks(self, list_id):
        """Mirrors the server: updateMany over one list, in a single call."""
        for tid, t in self.tasks.items():
            if t.get("listId") == list_id and not t.get("archivedAt"):
                t["archivedAt"] = "now"
                self.archived.append((tid, True))
        self.lists_archived.append(list_id)

    def spend_time(self, task_id, begin, end=None):
        if task_id not in self.tasks:
            raise AsoodeError(f"no task {task_id}")
        self.spent.append((task_id, begin, end))


class TestAsoodeConformance(ProviderConformance):
    """asoode against the shared contract."""

    @pytest.fixture
    def provider(self):
        return AsoodeProvider(FakeAsoodeClient())

    @pytest.fixture
    def container(self, provider):
        space = provider.create_space("Conformance Space")
        return provider.create_container(
            "Conformance", external_ref="conformance-1", space_id=space.id)


class TestAsoodeSpecificTranslation:
    """The four translations that earn the adapter."""

    @pytest.fixture
    def provider(self):
        return AsoodeProvider(FakeAsoodeClient())

    @pytest.fixture
    def board(self, provider):
        space = provider.create_space("S")
        return provider.create_container("B", space_id=space.id)

    def test_ordinals_become_local_state_names(self, provider, board):
        """asoode stores 1-9; nothing above the adapter may see a number."""
        task = provider.create_task(board.id, board.groups[0].id, "X")
        provider.set_state(task.id, "blocker")
        fetched = provider.fetch_container(board.id, with_tasks=True)
        assert fetched.tasks[0].state == "blocker"
        assert provider._client.tasks[task.id]["state"] == 9, "stored as the ordinal"

    def test_an_unknown_state_is_rejected_not_defaulted(self, provider, board):
        task = provider.create_task(board.id, board.groups[0].id, "X")
        with pytest.raises(ProviderError, match="unknown task state"):
            provider.set_state(task.id, "almost-done")

    def test_lists_become_groups(self, provider, board):
        assert [g.title for g in board.groups] == ["To Do", "In Progress", "Done"]

    def test_a_task_with_no_group_lands_in_the_first_list(self, provider, board):
        """POST /tasks/:listId/create is the only route - a board is not a target."""
        task = provider.create_task(board.id, None, "Unplaced")
        assert task.group_id == board.groups[0].id

    def test_a_work_package_with_no_lists_cannot_take_a_task(self, provider, board):
        provider._client.boards[board.id]["lists"] = []
        with pytest.raises(ProviderError, match="no lists"):
            provider.create_task(board.id, None, "Nowhere")

    def test_a_board_outside_a_project_is_refused(self, provider):
        """asoode cannot hold a work package outside a project."""
        with pytest.raises(ProviderError, match="pass space_id"):
            provider.create_container("Orphan")

    def test_create_space_reuses_a_project_of_the_same_title(self, provider):
        """asoode projects carry no externalRef, so the title is the only handle -
        creating a second 'AchaSoft' is worse than reusing the one that exists."""
        first = provider.create_space("AchaSoft")
        second = provider.create_space("AchaSoft")
        assert first.id == second.id
        assert len(provider._client.projects) == 1

    def test_external_ref_is_carried_back_out(self, provider, board):
        provider.create_task(board.id, board.groups[0].id, "R", external_ref="ref-9")
        fetched = provider.fetch_container(board.id, with_tasks=True)
        assert fetched.tasks[0].external_ref == "ref-9"

    def test_time_is_sent_as_iso_instants(self, provider, board):
        """SpendTimeDto takes {begin, end} as dates; the local store holds
        datetimes, so the adapter is what serialises them."""
        from datetime import datetime, timedelta, timezone

        task = provider.create_task(board.id, board.groups[0].id, "Timed")
        end = datetime.now(timezone.utc)
        provider.log_time(task.id, end - timedelta(minutes=45), end)

        sent_task, sent_begin, sent_end = provider._client.spent[0]
        assert sent_task == task.id
        assert isinstance(sent_begin, str) and "T" in sent_begin
        assert isinstance(sent_end, str)

    def test_a_description_goes_up_as_html(self, provider, board):
        """asoode renders it with dangerouslySetInnerHTML from a TipTap editor."""
        provider.create_task(board.id, None, "T", description="## Plan\n\n- one\n- two")
        stored = list(provider.client.tasks.values())[-1]["description"]
        assert stored == "<h2>Plan</h2><ul><li>one</li><li>two</li></ul>"

    def test_a_description_edit_goes_up_as_html(self, provider, board):
        task = provider.create_task(board.id, None, "T")
        provider.update_fields(task.id, {"description": "**bold**"})
        assert provider.client.tasks[task.id]["description"] == "<p><strong>bold</strong></p>"

    def test_a_comment_goes_up_as_html_without_headings(self, provider, board):
        """The comment box is the same editor in compact mode - no heading node."""
        task = provider.create_task(board.id, None, "T")
        provider.comment(task.id, "## Finding\n\n- one")
        assert provider.client.comments[-1][1] == (
            "<p><strong>Finding</strong></p><ul><li>one</li></ul>"
        )

    def test_html_from_the_board_comes_back_as_markdown(self, provider, board):
        """RemoteTask.description is defined as being in OUR vocabulary."""
        task = provider.create_task(board.id, None, "T")
        provider.client.tasks[task.id]["description"] = "<h2>Edited</h2><ul><li>by a human</li></ul>"
        fetched = provider.fetch_container(board.id, with_tasks=True)
        assert fetched.tasks[0].description == "## Edited\n\n- by a human"

    def test_a_plain_description_is_not_rewritten(self, provider, board):
        """The store is full of plain rows written before any of this existed."""
        plain = "asoode has no optimistic concurrency (no version/etag)."
        task = provider.create_task(board.id, None, "T", description=plain)
        assert task.description == plain

    def test_capabilities_match_what_asoode_actually_does(self):
        assert _CAPABILITIES.supports_external_ref is True
        assert _CAPABILITIES.supports_independent_state is True, (
            "asoode keeps state and listId separate - that is why set_state also moves"
        )
        assert len(_CAPABILITIES.states) == 9
        assert _CAPABILITIES.supports_time_tracking is True, (
            "asoode has POST /tasks/:id/spend-time - time must be mirrored, not dropped"
        )

    def test_asoode_errors_are_provider_errors(self):
        """So the flusher's retry logic never learns one platform's error shape."""
        assert issubclass(AsoodeError, ProviderError)


class TestEnsureGroup:
    """Columns a board we build should have, coloured from asoode's own palette.

    Asked for by the user on 2026-09-05: backlog gray, todo yellow, in progress
    blue, done green, cancelled red - "use the color from the pre selected list
    we have", i.e. AVAILABLE_COLORS in asoode's own WpBoardColumn.tsx.
    """

    @pytest.fixture
    def provider(self):
        return AsoodeProvider(FakeAsoodeClient())

    @pytest.fixture
    def board(self, provider):
        space = provider.create_space("S")
        return provider.create_container("B", space_id=space.id)

    def test_creates_a_missing_column_with_its_colour(self, provider, board):
        gid = provider.ensure_group(board.id, "Cancelled", "#f44336")
        assert gid is not None
        titles = [g.title for g in provider.fetch_container(board.id).groups]
        assert "Cancelled" in titles
        assert provider._client.lists_created == [(board.id, "Cancelled", "#f44336")]

    def test_colours_an_existing_column_that_has_none(self, provider, board):
        provider.ensure_group(board.id, "To Do", "#fbb900")
        assert provider._client.lists_created == [], "must not duplicate the column"
        assert provider._client.lists_edited[0][2] == "#fbb900"

    def test_never_repaints_a_colour_someone_chose(self, provider, board):
        """A board is a shared artefact. A set colour is somebody's decision."""
        provider._client.boards[board.id]["lists"][0]["color"] = "#9c27b0"
        provider.ensure_group(board.id, "To Do", "#fbb900")
        assert provider._client.lists_edited == []
        groups = provider.fetch_container(board.id).groups
        assert groups[0].color == "#9c27b0"

    def test_is_idempotent(self, provider, board):
        for _ in range(3):
            provider.ensure_group(board.id, "Cancelled", "#f44336")
        titles = [g.title for g in provider.fetch_container(board.id).groups]
        assert titles.count("Cancelled") == 1

    def test_matches_the_column_title_case_insensitively(self, provider, board):
        provider.ensure_group(board.id, "to do", "#fbb900")
        assert provider._client.lists_created == []

    def test_the_capability_is_declared(self):
        assert _CAPABILITIES.supports_group_style is True


class TestRoleLabelColours:
    """One fixed colour per agent, kept across boards and across restarts.

    Asked for by the user on 2026-09-05: "use different colors for different
    agents; but keep a convension. so ex: agent backend is always red; agent
    dotnet is always blue".
    """

    @pytest.fixture
    def provider(self):
        return AsoodeProvider(FakeAsoodeClient())

    @pytest.fixture
    def board(self, provider):
        space = provider.create_space("S")
        return provider.create_container("B", space_id=space.id)

    def test_the_two_the_user_named(self):
        assert role_color("backend") == "#f44336", "backend is always red"
        assert role_color("dotnet") == "#2196f3", "dotnet is always blue"

    def test_every_agent_has_its_own_colour(self):
        assert len(set(ROLE_COLORS.values())) == len(ROLE_COLORS)

    def test_every_colour_is_one_asoode_offers(self):
        """A hex outside the picker's swatches never matches a human's label."""
        assert all(c in LABEL_PALETTE for c in ROLE_COLORS.values())

    def test_an_unknown_agent_still_gets_a_stable_colour(self):
        """The agent set grows; the convention must not need maintaining."""
        first = role_color("quantum")
        assert first in LABEL_PALETTE
        assert role_color("quantum") == first

    def test_the_stable_colour_survives_a_restart(self):
        """md5, not hash(): PYTHONHASHSEED would repaint every label on restart."""
        import subprocess
        import sys

        code = (
            "from memory_mcp.providers.asoode import role_color;"
            "print(role_color('quantum'))"
        )
        runs = {
            subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True,
                env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            ).stdout.strip()
            for seed in ("0", "1", "12345")
        }
        assert len(runs) == 1, f"colour changed with the hash seed: {runs}"

    def test_the_label_is_created_with_its_colour(self, provider, board):
        task = provider.create_task(board.id, board.groups[0].id, "X")
        provider.set_role_label(task.id, board.id, "backend")
        assert provider._client.labels_created[-1][2] == "#f44336"

    def test_case_and_padding_do_not_change_the_colour(self):
        assert role_color("  BackEnd ") == role_color("backend")


class TestRoleLabelIsIdempotent:
    """Found live by the test agent on 2026-09-06.

    `set_role_label` iterated the BOARD's labels, so it removed `agent:*`
    labels the card never carried, and re-added one it already had - which
    asoode answers with "already exists". That failure lands in the outbox and
    burns retry attempts, and became visible noise the moment tool responses
    started reporting `last_error`.
    """

    @pytest.fixture
    def provider(self):
        return AsoodeProvider(FakeAsoodeClient())

    @pytest.fixture
    def board(self, provider):
        space = provider.create_space("S")
        return provider.create_container("B", space_id=space.id)

    def test_setting_the_same_role_twice_is_a_no_op(self, provider, board):
        task = provider.create_task(board.id, board.groups[0].id, "X")
        provider.set_role_label(task.id, board.id, "backend")
        first = len(provider._client.labels_added)
        provider.set_role_label(task.id, board.id, "backend")
        assert len(provider._client.labels_added) == first, "re-added an existing label"
        assert provider._client.labels_removed == [], "removed something it should not"

    def test_does_not_remove_role_labels_the_task_never_had(self, provider, board):
        """The board's other agent:* labels are not this card's problem."""
        other = provider.create_task(board.id, board.groups[0].id, "Other")
        provider.set_role_label(other.id, board.id, "frontend")
        provider.set_role_label(other.id, board.id, "docs")   # frontend -> docs
        task = provider.create_task(board.id, board.groups[0].id, "Mine")
        provider._client.labels_removed.clear()
        provider.set_role_label(task.id, board.id, "backend")
        assert provider._client.labels_removed == [], (
            "removed labels belonging to another card"
        )

    def test_changing_the_role_swaps_only_this_task_s_label(self, provider, board):
        task = provider.create_task(board.id, board.groups[0].id, "X")
        provider.set_role_label(task.id, board.id, "backend")
        provider._client.labels_removed.clear()
        provider.set_role_label(task.id, board.id, "frontend")
        titles = {l["title"] for l in provider._client.tasks[task.id]["labels"]}
        assert titles == {"agent:frontend"}
        assert len(provider._client.labels_removed) == 1


class TestInstantsAreSentAsUTC:
    """Found live by the test agent on 2026-09-06.

    The local store's timestamps are naive LOCAL (DuckDB `current_timestamp`).
    asoode stores what it is sent and returns it with a `Z`, so sending the
    naive value put every mirrored stretch on the board at the wrong clock
    time - three hours out on this UTC+03:00 machine. Duration was unaffected,
    which is why it went unnoticed.
    """

    def test_a_naive_local_instant_is_converted(self):
        from datetime import datetime, timezone

        naive = datetime(2026, 9, 6, 0, 9, 36, 796790)
        expected = (
            naive.astimezone().astimezone(timezone.utc)
            .isoformat().replace("+00:00", "Z")
        )
        assert _utc_iso(naive) == expected
        assert _utc_iso(naive).endswith("Z")

    def test_an_aware_instant_is_normalised_not_shifted(self):
        from datetime import datetime, timezone

        aware = datetime(2026, 9, 5, 21, 9, 36, tzinfo=timezone.utc)
        assert _utc_iso(aware) == "2026-09-05T21:09:36Z"

    def test_a_string_passes_through(self):
        assert _utc_iso("2026-09-05T21:09:36Z") == "2026-09-05T21:09:36Z"

    def test_log_time_sends_utc(self):
        from datetime import datetime, timezone

        provider = AsoodeProvider(FakeAsoodeClient())
        space = provider.create_space("S")
        board = provider.create_container("B", space_id=space.id)
        task = provider.create_task(board.id, board.groups[0].id, "X")
        begin = datetime(2026, 9, 6, 0, 9, 36)
        provider.log_time(task.id, begin, datetime(2026, 9, 6, 0, 14, 28))
        sent_begin = provider._client.spent[-1][1]
        assert sent_begin.endswith("Z")
        assert sent_begin == begin.astimezone().astimezone(timezone.utc).isoformat(
        ).replace("+00:00", "Z")
