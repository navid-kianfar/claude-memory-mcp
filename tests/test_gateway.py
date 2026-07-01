"""Per-project backend routing: local projects are served locally; remote-bound
projects resolve to a RemoteBackend (the gateway). End-to-end forwarding is
covered by the manual two-daemon check; here we assert the routing decision."""

from memory_mcp import server
from memory_mcp.container import container
from memory_mcp.remote_backend import RemoteBackend, for_project


def test_local_project_does_not_route(project_slug):
    container.project_service.init_project(project_slug, "Local")
    assert server._remote(project_slug) is None


def test_unknown_project_does_not_route():
    assert server._remote("no-such-project") is None


def test_remote_bound_project_routes_to_backend():
    container.project_service.init_project("work", "Work")
    container.project_service.bind_backend(
        "work", "remote", "https://org.example.com", "mmcp_secret"
    )
    backend = server._remote("work")
    assert isinstance(backend, RemoteBackend)


def test_for_project_uses_stored_credential():
    container.project_service.init_project("work2", "Work2")
    info = container.project_service.bind_backend(
        "work2", "remote", "https://org2.example.com", "mmcp_tok2"
    )
    backend = for_project(info)
    # The bearer header carries the stored token.
    assert backend._headers.get("Authorization") == "Bearer mmcp_tok2"
    # Rebinding to local stops routing.
    container.project_service.bind_backend("work2", "local")
    assert server._remote("work2") is None
