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
    TaskProvider,
)

__all__ = [
    "Capabilities",
    "Container",
    "ContainerRef",
    "Group",
    "ProviderError",
    "ProviderAuthError",
    "RemoteTask",
    "TaskProvider",
]
