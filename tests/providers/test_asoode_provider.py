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
from memory_mcp.providers.asoode import _CAPABILITIES
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

    def create_label(self, package_id, title, color="#6366f1"):
        lid = self._next("lbl")
        self.boards[package_id].setdefault("labels", []).append(
            {"id": lid, "title": title}
        )
        return {"id": lid, "title": title}

    def add_task_label(self, task_id, label_id):
        self.labels_added.append((task_id, label_id))

    def remove_task_label(self, task_id, label_id):
        self.labels_removed.append((task_id, label_id))

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
