"""The protocol, validated against a reference implementation.

An interface with no implementation is a guess. This in-memory provider is the
cheapest possible one, and running the conformance suite against it proves two
things before any real platform is touched: the protocol IS implementable, and
the suite itself is coherent.

It is also the answer to "what does a provider have to do?" - deliberately kept
small enough to read in one go.
"""

import pytest

from memory_mcp.providers import Capabilities, Container, Group, ProviderError, RemoteTask
from tests.providers.conformance import ProviderConformance
from tests.providers.fakes import FakeProvider

class TestFakeConformance(ProviderConformance):
    @pytest.fixture
    def provider(self):
        return FakeProvider()


class TestTheInterfaceItself:
    """Properties of the contract, not of any one implementation."""

    def test_capabilities_is_immutable(self):
        caps = Capabilities()
        with pytest.raises(Exception):
            caps.supports_comments = True

    def test_defaults_are_the_conservative_reading(self):
        """An unset flag must mean 'cannot', so a provider has to opt in to a
        promise rather than inherit one it does not keep."""
        caps = Capabilities()
        assert caps.supports_external_ref is False
        assert caps.supports_comments is False
        assert caps.states == ()

    def test_auth_failure_is_distinguishable_from_a_normal_failure(self):
        """The flusher retries a ProviderError and surfaces an auth one - retrying
        a revoked token forever is the failure mode this prevents."""
        from memory_mcp.providers import ProviderAuthError

        assert issubclass(ProviderAuthError, ProviderError)
        assert not issubclass(ProviderError, ProviderAuthError)

    def test_remote_task_speaks_local_state_vocabulary(self):
        """No platform ordinal or status name may reach shared code."""
        from memory_mcp.models import TaskState

        assert RemoteTask(id="1", title="x").state in {s.value for s in TaskState}

    def test_a_container_carries_its_groups_not_a_lookup(self):
        c = Container(id="c1", title="Board", groups=(Group(id="g1", title="To Do"),))
        assert c.groups[0].title == "To Do"
