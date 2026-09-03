"""Endpoint resolution, the machine-wide PAT, and the REST client's envelope."""

import httpx
import pytest

from memory_mcp import asoode
from memory_mcp.asoode_client import (
    STATE_TO_ORDINAL,
    AsoodeAuthError,
    AsoodeClient,
    AsoodeError,
)
from memory_mcp.config import settings
from memory_mcp.constants import (
    ASOODE_DEFAULT_API_URL,
    ASOODE_DEFAULT_APP_URL,
    ASOODE_DEFAULT_SOCKET_URL,
)


@pytest.fixture(autouse=True)
def _no_env_override():
    """Settings is a process singleton; keep one test's override out of the next."""
    saved = (
        settings.asoode_app_url, settings.asoode_api_url, settings.asoode_socket_url,
    )
    settings.asoode_app_url = settings.asoode_api_url = settings.asoode_socket_url = ""
    yield
    (
        settings.asoode_app_url, settings.asoode_api_url, settings.asoode_socket_url,
    ) = saved


class TestDefaults:
    def test_hosted_urls_are_the_default(self):
        endpoints = asoode.get_endpoints()
        assert endpoints.app_url == ASOODE_DEFAULT_APP_URL == "https://app.asoode.com"
        assert endpoints.api_url == ASOODE_DEFAULT_API_URL == "https://api.asoode.com"
        assert endpoints.socket_url == ASOODE_DEFAULT_SOCKET_URL == "https://socket.asoode.com"
        assert endpoints.is_default is True

    def test_a_fresh_install_needs_no_configuration(self):
        """The point of the defaults: status works before anything is set."""
        st = asoode.status()
        assert st["endpoints"]["api_url"] == "https://api.asoode.com"
        assert st["pat_configured"] is False


class TestPrecedence:
    def test_stored_setting_beats_default(self):
        asoode.set_endpoints(api_url="https://api.asoode.internal", derive=False)
        endpoints = asoode.get_endpoints()
        assert endpoints.api_url == "https://api.asoode.internal"
        assert endpoints.sources["api_url"] == "setting"
        # untouched fields stay on the hosted default
        assert endpoints.app_url == ASOODE_DEFAULT_APP_URL
        assert endpoints.is_default is False

    def test_env_beats_stored_setting(self):
        asoode.set_endpoints(api_url="https://api.stored.example", derive=False)
        settings.asoode_api_url = "https://api.env.example"
        endpoints = asoode.get_endpoints()
        assert endpoints.api_url == "https://api.env.example"
        assert endpoints.sources["api_url"] == "env"

    def test_reset_returns_to_the_hosted_defaults(self):
        asoode.set_endpoints(api_url="https://api.asoode.internal", derive=False)
        endpoints = asoode.reset_endpoints()
        assert endpoints.api_url == ASOODE_DEFAULT_API_URL
        assert endpoints.is_default is True


class TestUrlHandling:
    def test_trailing_slash_is_stripped(self):
        assert asoode.normalize_url("https://api.example.com/") == "https://api.example.com"

    def test_a_path_is_allowed_for_a_reverse_proxy(self):
        assert (
            asoode.normalize_url("https://host.example/asoode/api")
            == "https://host.example/asoode/api"
        )

    @pytest.mark.parametrize("bad", ["", "   ", "api.example.com", "ftp://x.example", "https://"])
    def test_rejects_unusable_urls(self, bad):
        with pytest.raises(asoode.AsoodeConfigError):
            asoode.normalize_url(bad)

    def test_siblings_derived_from_an_api_subdomain(self):
        assert asoode.derive_siblings("https://api.example.com") == {
            "app_url": "https://app.example.com",
            "socket_url": "https://socket.example.com",
        }

    def test_no_guess_when_the_host_is_not_api_dot_something(self):
        assert asoode.derive_siblings("https://asoode.example.com/api") == {}

    def test_setting_only_the_api_fills_in_the_siblings(self):
        endpoints = asoode.set_endpoints(api_url="https://api.acme.test")
        assert endpoints.app_url == "https://app.acme.test"
        assert endpoints.socket_url == "https://socket.acme.test"

    def test_explicit_values_win_over_derivation(self):
        endpoints = asoode.set_endpoints(
            api_url="https://api.acme.test", socket_url="https://ws.acme.test",
        )
        assert endpoints.socket_url == "https://ws.acme.test"


class TestPat:
    def test_stored_once_and_shared_by_every_project(self):
        """The requirement: no per-project entry. The key is the server, not a slug."""
        asoode.set_pat("asoode_pat_" + "x" * 40)
        assert asoode.get_pat() == "asoode_pat_" + "x" * 40
        # nothing about a project is involved in reading it back
        assert asoode.get_pat("https://api.asoode.com") == asoode.get_pat()

    def test_status_reports_a_fingerprint_never_the_token(self):
        token = "asoode_pat_secretsecretsecret1234"
        asoode.set_pat(token)
        st = asoode.status()
        assert st["pat_configured"] is True
        assert st["pat"] == {"prefix": "asoode", "last4": "1234", "length": len(token)}
        assert token not in repr(st)

    def test_pat_follows_the_server_it_was_stored_for(self):
        asoode.set_pat("cloud-token")
        asoode.set_endpoints(api_url="https://api.onprem.test", derive=False)
        assert asoode.get_pat() is None          # different server, no token yet
        asoode.set_pat("onprem-token")
        assert asoode.get_pat() == "onprem-token"
        asoode.reset_endpoints()
        assert asoode.get_pat() == "cloud-token"  # the cloud one was never lost

    def test_clear(self):
        asoode.set_pat("t" * 20)
        asoode.clear_pat()
        assert asoode.get_pat() in (None, "")
        assert asoode.status()["pat_configured"] is False

    @pytest.mark.parametrize("bad", ["", "   ", "has space", "two\nlines"])
    def test_rejects_a_malformed_token(self, bad):
        with pytest.raises(asoode.AsoodeConfigError):
            asoode.set_pat(bad)

    def test_warns_when_the_pat_would_cross_the_network_in_the_clear(self):
        asoode.set_pat("t" * 20)
        asoode.set_endpoints(api_url="http://api.insecure.test", derive=False)
        assert any("unencrypted" in w for w in asoode.status()["warnings"])

    def test_no_warning_for_plain_http_to_localhost(self):
        asoode.set_endpoints(api_url="http://localhost:3000", derive=False)
        asoode.set_pat("t" * 20)
        assert not any("unencrypted" in w for w in asoode.status()["warnings"])


class TestClientEnvelope:
    """asoode answers 2xx with a status envelope; a non-2 status is still failure."""

    def _client_with(self, handler, monkeypatch):
        client = AsoodeClient("https://api.test", "asoode_pat_test")
        transport = httpx.MockTransport(handler)
        real_init = httpx.Client.__init__

        def patched(self, *a, **kw):
            kw["transport"] = transport
            real_init(self, *a, **kw)

        monkeypatch.setattr(httpx.Client, "__init__", patched)
        return client

    def test_success_unwraps_data(self, monkeypatch):
        client = self._client_with(
            lambda r: httpx.Response(201, json={"status": 2, "data": [{"id": "p1"}]}),
            monkeypatch,
        )
        assert client.list_projects() == [{"id": "p1"}]

    def test_non_success_status_is_an_error_despite_http_200(self, monkeypatch):
        client = self._client_with(
            lambda r: httpx.Response(200, json={"status": 7, "message": "title required"}),
            monkeypatch,
        )
        with pytest.raises(AsoodeError, match="validation failed"):
            client.list_projects()

    def test_401_is_an_auth_error_with_the_remedy(self, monkeypatch):
        client = self._client_with(lambda r: httpx.Response(401, text="nope"), monkeypatch)
        with pytest.raises(AsoodeAuthError, match="set-pat"):
            client.list_projects()

    def test_unreachable_names_the_server(self, monkeypatch):
        def boom(request):
            raise httpx.ConnectError("refused")

        client = self._client_with(boom, monkeypatch)
        with pytest.raises(AsoodeError, match="unreachable"):
            client.list_projects()

    def test_create_task_assigns_self_so_it_shows_in_my_tasks(self, monkeypatch):
        seen = {}

        def handler(request):
            import json as _json

            seen.update(_json.loads(request.content))
            return httpx.Response(201, json={"status": 2, "data": {"id": "t1"}})

        client = self._client_with(handler, monkeypatch)
        client.create_task("list-1", "Title", description="Body", external_ref="ref-1")
        assert seen["assignSelf"] is True
        assert seen["externalRef"] == "ref-1"
        assert seen["description"] == "Body"

    def test_from_settings_refuses_without_a_pat(self):
        with pytest.raises(AsoodeAuthError, match="set-pat"):
            AsoodeClient.from_settings()


class TestStateVocabulary:
    def test_local_states_map_onto_asoode_ordinals_exactly(self):
        from memory_mcp.models import TaskState

        assert set(STATE_TO_ORDINAL) == {s.value for s in TaskState}
        assert STATE_TO_ORDINAL["todo"] == 1
        assert STATE_TO_ORDINAL["done"] == 3
        assert STATE_TO_ORDINAL["blocker"] == 9


class TestSignedInDeepLink:
    """asoode's /auth/token#t=… flow, built so a tool can open a signed-in board.

    The security property under test is WHERE the token goes: a fragment is never
    sent to the server, never written to an access log, and never leaks through a
    Referer header. A query string is all three, so a regression here is not
    cosmetic.
    """

    TOKEN = "asoode_pat_" + "z" * 40

    def test_token_rides_in_the_fragment_never_the_query(self):
        asoode.set_pat(self.TOKEN)
        url = asoode.signin_url("/projects/abc")
        head, _, fragment = url.partition("#")
        assert self.TOKEN in fragment
        assert self.TOKEN not in head, "the token must never be in the query string"
        assert head.endswith("?returnUrl=%2Fprojects%2Fabc")

    def test_no_return_path_still_signs_in(self):
        asoode.set_pat(self.TOKEN)
        url = asoode.signin_url()
        assert url.startswith("https://app.asoode.com/auth/token#t=")

    def test_follows_the_configured_app_url(self):
        asoode.set_pat(self.TOKEN)
        asoode.set_endpoints(api_url="https://api.acme.test")
        asoode.set_pat(self.TOKEN)  # new server, new entry
        assert asoode.signin_url().startswith("https://app.acme.test/auth/token")

    @pytest.mark.parametrize(
        "bad", ["https://evil.test/x", "//evil.test/x", "projects/abc"]
    )
    def test_rejects_a_return_path_asoode_would_drop(self, bad):
        """Mirrors asoode's safeReturnUrl: same-app paths only."""
        asoode.set_pat(self.TOKEN)
        with pytest.raises(asoode.AsoodeConfigError):
            asoode.signin_url(bad)

    def test_without_a_pat_it_says_how_to_fix_it(self):
        with pytest.raises(asoode.AsoodeNotAuthenticated, match="set-pat"):
            asoode.signin_url()

    def test_redacted_is_safe_to_print(self):
        asoode.set_pat(self.TOKEN)
        url = asoode.signin_url("/projects/abc")
        safe = asoode.redacted(url)
        assert self.TOKEN not in safe
        assert safe.endswith("#t=<PAT>")
        assert "returnUrl=%2Fprojects%2Fabc" in safe, "only the token is removed"
