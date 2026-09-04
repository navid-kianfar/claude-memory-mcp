"""Name -> implementation, so a link decides which platform it talks to.

`project_links.provider` has existed since the table was created, has always
defaulted to 'asoode', and was never read - every call went to the one hardcoded
implementation. This is what reads it.

WHY A REGISTRY AND NOT AN IF: one memory project must be able to hold links to
DIFFERENT platforms at once. A repo whose backlog is in Jira while its design
board lives in Trello is the normal case, not an edge one, so "which provider"
is a per-LINK question and cannot be answered by configuration at the project or
machine level.

Instances are cached per name because a provider holds a client, and a client
holds a connection pool and a resolved credential; building one per outbox row
would re-read the credential store on every mirrored task.
"""

import threading
from typing import Callable

from memory_mcp.providers.base import ProviderError, TaskProvider

DEFAULT_PROVIDER = "asoode"

_factories: dict[str, Callable[[], TaskProvider]] = {}
_instances: dict[str, TaskProvider] = {}
_lock = threading.Lock()


def register(name: str, factory: Callable[[], TaskProvider]) -> None:
    """Make a platform available under a name. Idempotent - re-registering
    replaces the factory and drops any cached instance, which is what a test
    swapping in a fake needs."""
    with _lock:
        _factories[name] = factory
        _instances.pop(name, None)


def unregister(name: str) -> None:
    with _lock:
        _factories.pop(name, None)
        _instances.pop(name, None)


def available() -> list[str]:
    """Every registered platform name, sorted."""
    _ensure_builtins()
    with _lock:
        return sorted(_factories)


def get_provider(name: str | None = None) -> TaskProvider:
    """The implementation for a name, built once and reused.

    An empty name means the default rather than an error: every link written
    before this existed carries 'asoode' from the column default, and a link
    written by an older build might carry nothing at all.
    """
    _ensure_builtins()
    key = (name or DEFAULT_PROVIDER).strip().lower()
    with _lock:
        cached = _instances.get(key)
        if cached is not None:
            return cached
        factory = _factories.get(key)
    if factory is None:
        raise ProviderError(
            f"unknown task provider {key!r}. Registered: {', '.join(available())}"
        )
    provider = factory()
    with _lock:
        # Another thread may have built one first; keep whichever landed, so a
        # provider is never held twice with two connection pools.
        return _instances.setdefault(key, provider)


def provider_for_link(link: dict | None) -> TaskProvider:
    """The provider a stored link routes to."""
    return get_provider((link or {}).get("provider"))


def reset_cache() -> None:
    """Drop cached instances, keeping registrations. For tests, and for after a
    credential changes - an instance holds the token it was built with."""
    with _lock:
        _instances.clear()


def _ensure_builtins() -> None:
    """Register the platforms that ship with the server, once.

    Lazy on purpose: importing a platform module at import time recreates the
    cycle providers/__init__ already avoids, and a platform whose dependencies
    are missing should fail when it is USED, not when the server starts.
    """
    with _lock:
        if DEFAULT_PROVIDER in _factories:
            return

    def _asoode() -> TaskProvider:
        from memory_mcp.providers.asoode import AsoodeProvider

        return AsoodeProvider()

    def _trello() -> TaskProvider:
        from memory_mcp.providers.trello import TrelloProvider

        return TrelloProvider()

    def _asana() -> TaskProvider:
        from memory_mcp.providers.asana import AsanaProvider

        return AsanaProvider()

    register(DEFAULT_PROVIDER, _asoode)
    register("trello", _trello)
    register("asana", _asana)
