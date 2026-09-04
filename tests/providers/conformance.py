"""The contract test every TaskProvider must pass.

Subclass it in a provider's own test module and set `provider` — that provider is
then held to the same promises as every other, which is the only thing making the
abstraction worth having:

    class TestAsoodeConformance(ProviderConformance):
        @pytest.fixture
        def provider(self):
            return AsoodeProvider(FakeTransport())

Nothing here asserts HOW a platform does something, only that the promises the
bridge relies on hold. A provider that cannot pass one of these needs a
capability flag, not an exemption.
"""

import pytest

from memory_mcp.providers import (
    Capabilities,
    Container,
    ContainerRef,
    ProviderError,
    RemoteTask,
    SpaceRef,
    TaskProvider,
)

LOCAL_STATES = {
    "todo", "in_progress", "done", "paused", "blocked",
    "cancelled", "duplicate", "incomplete", "blocker",
}


class SpaceConformance:
    """The space level: what `bootstrap` needs to create a board somewhere.

    Folded into ProviderConformance below, kept as its own class so a provider
    with no space level can see exactly which promises that costs it.
    """

    def test_listing_spaces_is_always_answerable(self, provider):
        """The level is always REACHABLE, which is the property shared code needs.

        Not "always non-empty": a real account can legitimately have no projects
        yet, and asserting otherwise made asoode fail a promise the interface
        never needed. A platform with no space level returns one synthetic entry;
        a platform with the level returns however many exist, including none.
        """
        spaces = provider.list_spaces()
        assert isinstance(spaces, list)
        assert all(isinstance(s, SpaceRef) for s in spaces)

    def test_a_created_space_then_appears(self, provider):
        created = provider.create_space("Listed Space")
        assert created.id in {s.id for s in provider.list_spaces()}

    def test_a_created_space_is_findable_by_title(self, provider):
        created = provider.create_space("Conformance Space")
        found = provider.find_space("Conformance Space")
        assert found is not None and found.id == created.id

    def test_find_space_is_case_insensitive(self, provider):
        provider.create_space("Mixed Case Space")
        assert provider.find_space("mixed case space") is not None

    def test_find_space_returns_none_when_absent(self, provider):
        assert provider.find_space("no space has this title") is None

    def test_a_container_can_be_created_inside_a_space(self, provider):
        space = provider.create_space("Holder")
        container = provider.create_container("Inside", space_id=space.id)
        assert container.id
        assert container.id in {c.id for c in provider.list_containers(space.id)}


class ProviderConformance(SpaceConformance):
    """Promises the bridge is built on. Subclass and provide `provider`."""

    @pytest.fixture
    def provider(self):
        raise NotImplementedError("a conformance subclass must supply `provider`")

    @pytest.fixture
    def container(self, provider):
        """A container to work in, created through the provider's own API."""
        return provider.create_container("Conformance", external_ref="conformance-1")

    # ---------- shape ----------

    def test_satisfies_the_protocol(self, provider):
        assert isinstance(provider, TaskProvider)

    def test_has_a_stable_name(self, provider):
        assert isinstance(provider.name, str) and provider.name.strip()
        assert provider.name == provider.name, "name must not change between reads"

    def test_declares_capabilities(self, provider):
        caps = provider.capabilities
        assert isinstance(caps, Capabilities)

    def test_declared_states_are_local_vocabulary(self, provider):
        """A provider translates; it never invents a state shared code cannot read."""
        assert set(provider.capabilities.states) <= LOCAL_STATES

    # ---------- discovery ----------

    def test_created_container_is_listed(self, provider, container):
        ids = {c.id for c in provider.list_containers()}
        assert container.id in ids

    def test_list_returns_container_refs(self, provider, container):
        assert all(isinstance(c, ContainerRef) for c in provider.list_containers())

    def test_fetch_returns_a_container_with_groups(self, provider, container):
        fetched = provider.fetch_container(container.id)
        assert isinstance(fetched, Container)
        assert fetched.id == container.id
        if provider.capabilities.supports_groups:
            assert fetched.groups, "a platform with groups must expose them"

    def test_fetching_an_unknown_container_raises(self, provider):
        with pytest.raises(ProviderError):
            provider.fetch_container("no-such-container")

    def test_find_container_by_ref(self, provider, container):
        if not provider.capabilities.supports_external_ref:
            pytest.skip("provider has no external ref")
        found = provider.find_container("conformance-1")
        assert found is not None and found.id == container.id

    def test_find_container_returns_none_when_absent(self, provider, container):
        if not provider.capabilities.supports_external_ref:
            pytest.skip("provider has no external ref")
        assert provider.find_container("nothing-has-this-ref") is None

    # ---------- creating tasks ----------

    def _group(self, provider, container):
        fetched = provider.fetch_container(container.id)
        return fetched.groups[0].id if fetched.groups else None

    def test_create_returns_a_remote_task_with_an_id(self, provider, container):
        task = provider.create_task(
            container.id, self._group(provider, container), "First task")
        assert isinstance(task, RemoteTask)
        assert task.id and task.title == "First task"

    def test_a_created_task_is_in_the_container(self, provider, container):
        task = provider.create_task(
            container.id, self._group(provider, container), "Findable")
        fetched = provider.fetch_container(container.id, with_tasks=True)
        assert task.id in {t.id for t in fetched.tasks}

    def test_description_is_stored(self, provider, container):
        provider.create_task(
            container.id, self._group(provider, container), "With body",
            description="the body")
        fetched = provider.fetch_container(container.id, with_tasks=True)
        task = next(t for t in fetched.tasks if t.title == "With body")
        assert task.description == "the body"

    def test_the_same_external_ref_returns_the_same_task(self, provider, container):
        """The promise the flusher's retry is built on: a repeated create is not
        a duplicate. Without it, one timeout doubles a board."""
        if not provider.capabilities.supports_external_ref:
            pytest.skip("provider has no external ref")
        group = self._group(provider, container)
        first = provider.create_task(container.id, group, "Once", external_ref="ref-1")
        second = provider.create_task(container.id, group, "Once", external_ref="ref-1")
        assert first.id == second.id

        fetched = provider.fetch_container(container.id, with_tasks=True)
        assert sum(1 for t in fetched.tasks if t.title == "Once") == 1

    def test_creating_in_an_unknown_container_raises(self, provider):
        with pytest.raises(ProviderError):
            provider.create_task("no-such-container", None, "Orphan")

    # ---------- mutating tasks ----------

    def test_state_round_trips_in_local_vocabulary(self, provider, container):
        task = provider.create_task(
            container.id, self._group(provider, container), "Stateful")
        provider.set_state(task.id, "done")
        fetched = provider.fetch_container(container.id, with_tasks=True)
        assert next(t for t in fetched.tasks if t.id == task.id).state == "done"

    def test_every_declared_state_can_be_set(self, provider, container):
        task = provider.create_task(
            container.id, self._group(provider, container), "Every state")
        for state in provider.capabilities.states:
            provider.set_state(task.id, state)

    def test_setting_an_unknown_state_raises(self, provider, container):
        task = provider.create_task(
            container.id, self._group(provider, container), "Bad state")
        with pytest.raises(ProviderError):
            provider.set_state(task.id, "not-a-state")

    def test_setting_state_on_an_unknown_task_raises(self, provider):
        with pytest.raises(ProviderError):
            provider.set_state("no-such-task", "done")

    def test_move_never_fails_on_a_real_group(self, provider, container):
        """Presentation, not truth. A provider whose group IS its state may make
        this a no-op, but must not raise."""
        if not provider.capabilities.supports_groups:
            pytest.skip("provider has no groups")
        fetched = provider.fetch_container(container.id)
        task = provider.create_task(container.id, fetched.groups[0].id, "Movable")
        provider.move(task.id, fetched.groups[-1].id)

    def test_comment_is_accepted_when_declared(self, provider, container):
        if not provider.capabilities.supports_comments:
            pytest.skip("provider has no comments")
        task = provider.create_task(
            container.id, self._group(provider, container), "Commentable")
        provider.comment(task.id, "a note")

    def test_commenting_on_an_unknown_task_raises(self, provider):
        if not provider.capabilities.supports_comments:
            pytest.skip("provider has no comments")
        with pytest.raises(ProviderError):
            provider.comment("no-such-task", "a note")

    # ---------- the container is never optional ----------

    def test_a_task_always_has_a_container_to_live_in(self, provider):
        """The rule the whole interface is shaped around: space -> container ->
        task, with the container required.

        The space is passed explicitly because a real platform may require it -
        asoode cannot hold a work package outside a project and rightly refuses.
        A platform with no space level ignores the argument; one with it uses it.
        Either way shared code follows one path, which is the point.
        """
        space = provider.create_space("Container Holder")
        container = provider.create_container("Holds a task", space_id=space.id)
        assert container.id

        fetched = provider.fetch_container(container.id)
        group_id = fetched.groups[0].id if fetched.groups else None
        task = provider.create_task(container.id, group_id, "Inside a container")
        assert task.id
