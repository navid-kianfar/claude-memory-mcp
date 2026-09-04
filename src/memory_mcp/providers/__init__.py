"""Task platforms this server can mirror a project's task list onto.

`base` defines the contract every platform implements; each module beside it is
one platform. Nothing outside this package may import a platform module directly -
the bridge holds a `TaskProvider`, never an `AsoodeProvider`.
"""

from memory_mcp.providers.base import (
    Capabilities,
    Container,
    ContainerRef,
    Group,
    ProviderError,
    ProviderAuthError,
    RemoteTask,
    SpaceRef,
    TaskProvider,
)

from memory_mcp.providers.registry import (
    DEFAULT_PROVIDER,
    available,
    get_provider,
    provider_for_link,
    register,
    reset_cache,
    unregister,
)

__all__ = [
    "AsoodeProvider",
    "DEFAULT_PROVIDER",
    "available",
    "get_provider",
    "provider_for_link",
    "register",
    "reset_cache",
    "unregister",
    "Capabilities",
    "Container",
    "ContainerRef",
    "Group",
    "ProviderError",
    "ProviderAuthError",
    "RemoteTask",
    "SpaceRef",
    "TaskProvider",
]


def __getattr__(name: str):
    """Platform modules are resolved lazily.

    `asoode_client` imports ProviderError from `providers.base`, so importing a
    platform module from this __init__ eagerly makes a cycle: client -> base ->
    __init__ -> asoode -> client. PEP 562 lazy attribute access keeps the tidy
    `from memory_mcp.providers import AsoodeProvider` surface without it, and the
    registry that replaces this will resolve by name anyway.
    """
    if name == "AsoodeProvider":
        from memory_mcp.providers.asoode import AsoodeProvider

        return AsoodeProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
