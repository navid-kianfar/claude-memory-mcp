"""The UI's Clear button: archive many tasks at once.

ARCHIVE, NOT DELETE. TaskService.delete's own docstring says archiving is the
reversible option and stays the default in the UI - a button that empties a list
in one click is the last place to make an exception.

It takes explicit IDS rather than a filter, so the caller clears exactly what it
showed the user. A task created between rendering the list and pressing the
button must not be swept up in a clear nobody saw.
"""

import pytest

from memory_mcp.models import CreateTaskRequest
from memory_mcp.web import routes


@pytest.fixture
def project():
    from memory_mcp.container import container
    from memory_mcp.db.connection import get_connection

    slug = "t-bulk-archive"
    container.project_repo.register(slug, slug)
    get_connection(slug).close()
    return slug


def _make(project, title):
    from memory_mcp.container import container

    return container.task_service.create(
        CreateTaskRequest(project=project, title=title)
    )


def _open_ids(project):
    from memory_mcp.container import container
    from memory_mcp.models import TaskFilter

    result = container.task_service.list_tasks(project, TaskFilter(), limit=100)
    return {t.id for t in result.tasks}


class TestBulkArchive:
    def test_it_archives_exactly_the_ids_given(self, project):
        keep = _make(project, "Keep me")
        a, b = _make(project, "Clear a"), _make(project, "Clear b")

        result = routes._task_archive_bulk({"slug": project}, {"ids": [a.id, b.id]}, {})

        assert result["archived"] == 2
        assert _open_ids(project) == {keep.id}, "an id not passed must survive"

    def test_it_archives_rather_than_deletes(self, project):
        """The whole safety property: a mis-click must be recoverable."""
        from memory_mcp.container import container

        task = _make(project, "Clear me")

        routes._task_archive_bulk({"slug": project}, {"ids": [task.id]}, {})

        still_there = container.task_service.get(project, task.id)
        assert still_there is not None, "archive must not delete"
        assert still_there.archived_at is not None

    def test_one_bad_id_does_not_lose_the_rest(self, project):
        """With 40 tasks, one failure must not undo the other 39."""
        good = _make(project, "Good")

        result = routes._task_archive_bulk(
            {"slug": project}, {"ids": [good.id, "no-such-task"]}, {}
        )

        assert result["archived"] == 1
        assert "no-such-task" in result["failed"]
        assert _open_ids(project) == set()

    def test_an_empty_list_is_refused(self, project):
        """A clear that clears nothing is a mistake, not a no-op worth doing."""
        with pytest.raises(ValueError):
            routes._task_archive_bulk({"slug": project}, {"ids": []}, {})

    def test_a_missing_ids_field_is_refused(self, project):
        with pytest.raises(ValueError):
            routes._task_archive_bulk({"slug": project}, {}, {})

    def test_archiving_queues_the_board_mirror(self, project):
        """Clearing locally must reach the board, or the two sides drift."""
        from memory_mcp.container import container

        task = _make(project, "Clear me")
        routes._task_archive_bulk({"slug": project}, {"ids": [task.id]}, {})

        ops = [r["op"] for r in container.outbox_repo.pending(project, 50)]
        assert "archive" in ops
