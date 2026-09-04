"""Attachments: stored locally, mirrored once, never leaking a path to a provider.

The point of the feature: a screenshot proving a fix, a failing log, a generated
report. Prose describing a file is not the file, and a file that exists only
locally is invisible to everyone looking at the board.
"""

import pytest

from memory_mcp.container import container
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.exceptions import MemoryMCPError
from memory_mcp.models import CreateTaskRequest
from memory_mcp.providers import Capabilities, ProviderError
from memory_mcp.repositories import AttachmentRepository, OutboxRepository
from memory_mcp.services.asoode_bridge import AsoodeBridge
from memory_mcp.services.task_service import TaskService
from tests.providers.fakes import FakeProvider


@pytest.fixture
def project():
    slug = "attach-test"
    container.project_service.init_project(slug, "Attach Test")
    upsert_project_link(
        slug, base_url="https://api.test", remote_project_id="p1",
        remote_work_package_id="wp1", label="board", is_default=True,
        default_list_id="l-todo", state_list_map={"todo": "l-todo"},
    )
    return slug


@pytest.fixture
def stack(project):
    provider = FakeProvider()
    provider.seed(container_id="wp1", groups=(("l-todo", "To Do"),))
    outbox, attachments = OutboxRepository(), AttachmentRepository()
    tasks = TaskService(
        container.task_repo, container.provenance_repo, container.project_repo,
        container.session_repo, outbox_repo=outbox, attachment_repo=attachments,
    )
    bridge = AsoodeBridge(
        container.project_service, tasks, provider,
        outbox_repo=outbox, attachment_repo=attachments,
    )
    return tasks, bridge, provider


@pytest.fixture
def a_file(tmp_path):
    path = tmp_path / "proof.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"pretend image bytes" * 10)
    return path


class TestStoring:
    def test_a_file_is_attached_to_a_task(self, stack, project, a_file):
        tasks, _, _ = stack
        task = tasks.create(CreateTaskRequest(project=project, title="With proof"))
        attachment = tasks.attach(project, task.id, str(a_file))

        assert attachment.filename == "proof.png"
        assert attachment.size_bytes == a_file.stat().st_size
        assert attachment.content_type == "image/png"
        assert [a.id for a in tasks.attachments(project, task.id)] == [attachment.id]

    def test_the_bytes_are_copied_not_referenced(self, stack, project, a_file):
        """The source is usually a scratch file that will be gone later."""
        tasks, _, _ = stack
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        tasks.attach(project, task.id, str(a_file))
        a_file.unlink()

        stored = container.attachment_repo.list_for(project, task.id)
        assert stored, "the attachment survives its source being deleted"

    def test_the_same_file_twice_is_stored_once(self, stack, project, a_file, tmp_path):
        """Content-addressed: two tasks sharing a screenshot is one blob."""
        tasks, _, _ = stack
        first = tasks.create(CreateTaskRequest(project=project, title="A"))
        second = tasks.create(CreateTaskRequest(project=project, title="B"))
        a = tasks.attach(project, first.id, str(a_file))
        b = tasks.attach(project, second.id, str(a_file))
        assert a.sha256 == b.sha256
        assert a.id != b.id, "two rows, one blob"

    def test_removing_one_does_not_delete_a_shared_blob(self, stack, project, a_file):
        tasks, _, _ = stack
        first = tasks.create(CreateTaskRequest(project=project, title="A"))
        second = tasks.create(CreateTaskRequest(project=project, title="B"))
        a = tasks.attach(project, first.id, str(a_file))
        tasks.attach(project, second.id, str(a_file))

        tasks.detach(project, a.id)
        remaining = container.attachment_repo.list_for(project, second.id)
        assert remaining, "the other task still has it"
        found = container.attachment_repo.get(project, remaining[0].id)
        from pathlib import Path

        assert Path(found[1]).is_file(), "the shared blob must survive"

    def test_a_missing_file_is_refused(self, stack, project):
        tasks, _, _ = stack
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        with pytest.raises(MemoryMCPError, match="no file at"):
            tasks.attach(project, task.id, "/nope/does-not-exist.png")

    def test_an_empty_file_is_refused(self, stack, project, tmp_path):
        tasks, _, _ = stack
        empty = tmp_path / "empty.txt"
        empty.write_text("")
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        with pytest.raises(MemoryMCPError, match="empty"):
            tasks.attach(project, task.id, str(empty))

    def test_an_oversized_file_is_refused_with_the_size_named(self, stack, project, tmp_path, monkeypatch):
        tasks, _, _ = stack
        monkeypatch.setattr(TaskService, "MAX_ATTACHMENT_BYTES", 100)
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 500)
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        with pytest.raises(MemoryMCPError, match="over the"):
            tasks.attach(project, task.id, str(big))

    def test_attaching_to_an_unknown_task_is_refused(self, stack, project, a_file):
        tasks, _, _ = stack
        with pytest.raises(Exception):
            tasks.attach(project, "no-such-task", str(a_file))


class TestMirroring:
    def test_an_attachment_reaches_the_provider(self, stack, project, a_file):
        tasks, bridge, provider = stack
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        tasks.attach(project, task.id, str(a_file))
        bridge.flush(project)

        assert provider.attachments_sent, "the file must be uploaded"
        sent_task, filename, content, content_type = provider.attachments_sent[0]
        assert filename == "proof.png"
        assert content == a_file.read_bytes(), "the real bytes, not a path"
        assert content_type == "image/png"

    def test_the_provider_never_sees_a_filesystem_path(self, stack, project, a_file):
        """A provider must not have to know this server's storage layout."""
        tasks, bridge, provider = stack
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        tasks.attach(project, task.id, str(a_file))
        bridge.flush(project)
        _, _, content, _ = provider.attachments_sent[0]
        assert isinstance(content, bytes)

    def test_it_is_uploaded_once(self, stack, project, a_file):
        """A repeated 5 MB screenshot costs storage on both sides, not just noise."""
        tasks, bridge, provider = stack
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        tasks.attach(project, task.id, str(a_file))
        bridge.flush(project)
        container.outbox_repo.enqueue(project, task.id, "attachment", {})
        bridge.flush(project)
        assert len(provider.attachments_sent) == 1

    def test_a_second_attachment_still_goes(self, stack, project, a_file, tmp_path):
        tasks, bridge, provider = stack
        other = tmp_path / "log.txt"
        other.write_text("a failing log")
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        tasks.attach(project, task.id, str(a_file))
        bridge.flush(project)
        tasks.attach(project, task.id, str(other))
        bridge.flush(project)
        assert [f for _, f, _, _ in provider.attachments_sent] == ["proof.png", "log.txt"]

    def test_a_vanished_blob_is_marked_rather_than_retried_forever(self, stack, project, a_file):
        tasks, bridge, provider = stack
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        attachment = tasks.attach(project, task.id, str(a_file))
        found = container.attachment_repo.get(project, attachment.id)
        from pathlib import Path

        Path(found[1]).unlink()

        bridge.flush(project)
        assert provider.attachments_sent == []
        assert container.attachment_repo.unmirrored(project, task.id) == [], (
            "a blob that will not return must not queue forever"
        )

    def test_a_provider_without_attachments_keeps_them_unsent(self, stack, project, a_file):
        tasks, bridge, provider = stack
        original = type(provider).capabilities
        type(provider).capabilities = property(
            lambda self: Capabilities(supports_comments=True, states=("todo",))
        )
        try:
            task = tasks.create(CreateTaskRequest(project=project, title="X"))
            tasks.attach(project, task.id, str(a_file))
            bridge.flush(project)
            assert provider.attachments_sent == []
            assert container.attachment_repo.unmirrored(project, task.id), (
                "still there to send if the platform ever gains the capability"
            )
        finally:
            type(provider).capabilities = original

    def test_an_upload_failure_leaves_it_to_retry(self, stack, project, a_file):
        tasks, bridge, provider = stack
        task = tasks.create(CreateTaskRequest(project=project, title="X"))
        tasks.attach(project, task.id, str(a_file))

        provider.fail = ProviderError("upload rejected")
        bridge.flush(project)
        assert container.attachment_repo.unmirrored(project, task.id)

        provider.fail = None
        bridge.flush(project)
        assert len(provider.attachments_sent) == 1


class TestEveryProviderDeclaresIt:
    def test_all_three_platforms_support_attachments(self):
        """asoode, Trello and Asana all have an upload endpoint, so none of them
        should be silently dropping evidence."""
        from memory_mcp.providers import available, get_provider

        for name in available():
            assert get_provider(name).capabilities.supports_attachments, name
