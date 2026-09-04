"""The registry: which implementation a link talks to.

`project_links.provider` existed from the start, always defaulted to 'asoode',
and was never read - every call went to the one hardcoded implementation. These
tests are what make that column mean something.
"""

import pytest

from memory_mcp.container import container
from memory_mcp.db.registry import upsert_project_link
from memory_mcp.models import CreateTaskRequest
from memory_mcp.providers import (
    ProviderError,
    available,
    get_provider,
    provider_for_link,
    register,
    reset_cache,
    unregister,
)
from memory_mcp.services.task_bridge import TaskBridge
from tests.providers.fakes import FakeProvider


class Named(FakeProvider):
    """A fake that answers to a chosen name, so a test can tell two apart."""

    def __init__(self, name):
        super().__init__()
        self._name = name

    @property
    def name(self):
        return self._name


@pytest.fixture
def two_platforms():
    """Two registered platforms, torn down so one test cannot leak into another."""
    alpha, beta = Named("alpha"), Named("beta")
    register("alpha", lambda: alpha)
    register("beta", lambda: beta)
    yield alpha, beta
    unregister("alpha")
    unregister("beta")
    reset_cache()


class TestLookup:
    def test_asoode_ships_registered(self):
        assert "asoode" in available()
        assert get_provider("asoode").name == "asoode"

    def test_an_empty_name_means_the_default(self):
        """Links written before the column was read carry '' or nothing."""
        assert get_provider(None).name == "asoode"
        assert get_provider("").name == "asoode"

    def test_a_link_routes_by_its_provider_column(self, two_platforms):
        assert provider_for_link({"provider": "beta"}).name == "beta"

    def test_a_link_with_no_provider_falls_back(self, two_platforms):
        assert provider_for_link({}).name == "asoode"

    def test_an_unknown_name_lists_what_exists(self):
        with pytest.raises(ProviderError) as e:
            get_provider("jira")
        assert "asoode" in str(e.value)

    def test_instances_are_reused(self, two_platforms):
        """A provider holds a client, which holds a pool and a resolved
        credential - rebuilding one per outbox row would re-read the store."""
        assert get_provider("alpha") is get_provider("alpha")

    def test_re_registering_replaces_the_cached_instance(self):
        first = Named("swap")
        register("swap", lambda: first)
        assert get_provider("swap") is first

        second = Named("swap")
        register("swap", lambda: second)
        assert get_provider("swap") is second
        unregister("swap")

    def test_names_are_case_insensitive(self, two_platforms):
        assert get_provider("ALPHA").name == "alpha"


class TestOneProjectTwoPlatforms:
    """The reason the registry exists: a repo whose backlog is in one platform
    and whose design board is in another is normal, not an edge case."""

    @pytest.fixture
    def project(self, two_platforms):
        alpha, beta = two_platforms
        slug = "multi-platform"
        container.project_service.init_project(slug, "Multi Platform")
        alpha.seed(container_id="a-board", title="Alpha Board", space_id="a-space",
                   groups=(("a-todo", "To Do"), ("a-done", "Done")))
        beta.seed(container_id="b-board", title="Beta Board", space_id="b-space",
                  groups=(("b-todo", "To Do"), ("b-done", "Done")))
        upsert_project_link(
            slug, base_url="https://alpha.test", remote_project_id="a-space",
            remote_work_package_id="a-board", label="alpha-board", is_default=True,
            provider="alpha", default_list_id="a-todo",
            state_list_map={"todo": "a-todo", "done": "a-done"},
        )
        upsert_project_link(
            slug, base_url="https://beta.test", remote_project_id="b-space",
            remote_work_package_id="b-board", label="beta-board", is_default=False,
            provider="beta", default_list_id="b-todo",
            state_list_map={"todo": "b-todo", "done": "b-done"},
        )
        return slug

    def _bridge(self):
        # No injected provider: the registry must decide, per link.
        return TaskBridge(
            container.project_service, container.task_service,
            outbox_repo=container.outbox_repo,
        )

    def test_each_link_routes_to_its_own_platform(self, project, two_platforms):
        from memory_mcp.db.registry import get_project_links

        bridge = self._bridge()
        by_label = {l["label"]: l for l in get_project_links(project)}
        assert bridge.provider_for(by_label["alpha-board"]).name == "alpha"
        assert bridge.provider_for(by_label["beta-board"]).name == "beta"

    def test_a_task_mirrors_to_the_platform_it_targets(self, project, two_platforms):
        alpha, beta = two_platforms
        bridge = self._bridge()
        container.task_service.create(CreateTaskRequest(
            project=project, title="Belongs to beta", target="beta-board"))
        bridge.flush(project)

        assert [t["title"] for t in beta.created_tasks] == ["Belongs to beta"]
        assert alpha.created_tasks == [], "must not reach the other platform"

    def test_an_untargeted_task_goes_to_the_default_platform(self, project, two_platforms):
        alpha, beta = two_platforms
        bridge = self._bridge()
        container.task_service.create(
            CreateTaskRequest(project=project, title="No target"))
        bridge.flush(project)

        assert [t["title"] for t in alpha.created_tasks] == ["No target"]
        assert beta.created_tasks == []

    def test_both_platforms_receive_their_own_tasks_in_one_flush(self, project, two_platforms):
        alpha, beta = two_platforms
        bridge = self._bridge()
        container.task_service.create(CreateTaskRequest(
            project=project, title="For alpha", target="alpha-board"))
        container.task_service.create(CreateTaskRequest(
            project=project, title="For beta", target="beta-board"))
        bridge.flush(project)

        assert [t["title"] for t in alpha.created_tasks] == ["For alpha"]
        assert [t["title"] for t in beta.created_tasks] == ["For beta"]

    def test_one_platform_being_down_does_not_block_the_other(self, project, two_platforms):
        """The flusher stops at the first failure, so the queue must not wedge
        behind a dead platform forever - the working one drains on the retry."""
        alpha, beta = two_platforms
        bridge = self._bridge()
        container.task_service.create(CreateTaskRequest(
            project=project, title="To the dead one", target="alpha-board"))
        container.task_service.create(CreateTaskRequest(
            project=project, title="To the live one", target="beta-board"))

        alpha.fail = ProviderError("alpha is down")
        bridge.flush(project)
        assert beta.created_tasks == [], "flush stops at the first failure"

        alpha.fail = None
        bridge.flush(project)
        assert [t["title"] for t in alpha.created_tasks] == ["To the dead one"]
        assert [t["title"] for t in beta.created_tasks] == ["To the live one"]

    def test_attach_records_the_platform_on_the_link(self, project, two_platforms):
        bridge = self._bridge()
        result = bridge.attach(
            project, work_package_id="b-board", label="beta-again",
            is_default=False, provider="beta",
        )
        assert result["link"]["provider"] == "beta"


class TestCredentialsArePerPlatform:
    def test_stored_and_read_back_by_platform(self):
        from memory_mcp.providers import credentials

        credentials.set_credential("trello", "key-token-pair")
        assert credentials.get_credential("trello") == "key-token-pair"
        assert credentials.get_credential("monday") is None

    def test_the_same_platform_can_hold_one_per_account(self):
        """Jira is per-site, so one token is not enough."""
        from memory_mcp.providers import credentials

        credentials.set_credential("jira", "tok-a", account="https://a.atlassian.net")
        credentials.set_credential("jira", "tok-b", account="https://b.atlassian.net")
        assert credentials.get_credential("jira", "https://a.atlassian.net") == "tok-a"
        assert credentials.get_credential("jira", "https://b.atlassian.net") == "tok-b"

    def test_asoode_still_reads_its_existing_url_keyed_entry(self):
        """The PAT is already on the machine; asking for it twice would be a
        regression the user feels immediately."""
        from memory_mcp import asoode
        from memory_mcp.providers import credentials

        asoode.set_pat("asoode_pat_" + "q" * 30)
        assert credentials.get_credential("asoode") == "asoode_pat_" + "q" * 30

    def test_a_fingerprint_never_contains_the_secret(self):
        from memory_mcp.providers import credentials

        token = "secrettoken1234"
        fp = credentials.fingerprint(token)
        assert token not in repr(fp)
        assert fp["last4"] == "1234"

    def test_clearing_removes_it(self):
        from memory_mcp.providers import credentials

        credentials.set_credential("trello", "gone-soon")
        credentials.clear_credential("trello")
        assert credentials.get_credential("trello") in (None, "")
