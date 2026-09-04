"""Archiving a task must reach the board.

TaskService.archive() used to stop the clock, release the claim, set archived_at
and enqueue NOTHING, while the TaskProvider protocol had no archive method at
all. Because the local list HIDES archived tasks, the two sides diverged exactly
where nobody would look: this project had 45 done tasks sitting on its board and
the only reason anyone noticed was that someone went and counted.

Third time the "mirror everything the provider can hold" rule has had to be
applied, after time tracking and attachments.
"""

import pytest

from memory_mcp.models import CreateTaskRequest
from memory_mcp.providers.base import Capabilities


class TestArchiveCapability:
    def test_asoode_declares_it(self):
        from memory_mcp.providers.asoode import AsoodeProvider

        assert AsoodeProvider().capabilities.supports_archive is True

    def test_it_defaults_to_off_for_a_new_provider(self):
        """A platform without it must opt in, not inherit a promise it cannot keep."""
        assert Capabilities().supports_archive is False

    def test_the_protocol_takes_a_boolean_not_a_one_way_call(self):
        """The local store can un-archive; a one-way call makes that unmirrorable."""
        import inspect

        from memory_mcp.providers.base import TaskProvider

        sig = inspect.signature(TaskProvider.archive)
        assert "archived" in sig.parameters
        assert sig.parameters["archived"].default is True

    def test_a_bulk_group_archive_exists(self):
        """asoode has a real bulk route; looping 45 single calls is the wrong shape."""
        from memory_mcp.providers.asoode import AsoodeProvider

        assert hasattr(AsoodeProvider, "archive_group")


class TestArchiveEnqueues:
    @pytest.fixture
    def container(self):
        from memory_mcp.container import Container

        return Container()

    def _project(self, container, slug):
        from memory_mcp.db.connection import get_connection

        container.project_repo.register(slug, slug)
        get_connection(slug).close()
        return slug

    def test_archiving_queues_a_mirror(self, container):
        slug = self._project(container, "t-archive-mirror")
        task = container.task_service.create(
            CreateTaskRequest(project=slug, title="Finished thing")
        )
        before = container.outbox_repo.depth(slug)

        container.task_service.archive(slug, task.id)

        assert container.outbox_repo.depth(slug) > before, (
            "archive must enqueue, or the board keeps showing the task forever"
        )

    def test_the_queued_op_says_archive_true(self, container):
        slug = self._project(container, "t-archive-payload")
        task = container.task_service.create(
            CreateTaskRequest(project=slug, title="Finished thing")
        )
        container.task_service.archive(slug, task.id)

        rows = container.outbox_repo.pending(slug, 50)
        archive_rows = [r for r in rows if r["op"] == "archive"]
        assert archive_rows, f"no archive op queued; got {[r['op'] for r in rows]}"
        assert (archive_rows[-1].get("payload") or {}).get("archived") is True
