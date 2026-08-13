"""Tests for catapa_mcp.oauth.

Condition:
    A CatapaConfig-backed Settings configured for OAuth mode, with the on-disk token cache and
    outbound HTTP calls (`requests.post`, the browser open, and the real CATAPA login page)
    replaced by fakes/mocks so no real login or network access is required.

Expected:
    Cached, valid tokens are reused without any HTTP calls; expired tokens are refreshed via the
    refresh_token grant; a missing/unrefreshable token triggers the interactive loopback-server
    login flow, which correctly exchanges the authorization code for a token.
"""

import threading
import time
import urllib.parse
from dataclasses import replace

import pytest
import requests

from catapa_mcp import oauth
from catapa_mcp.config import Settings


def _make_settings(**overrides) -> Settings:
    base = Settings(
        public_enabled=True,
        public_tenant=None,
        public_base_url="https://api.catapa.com",
        public_access_token=None,
        public_client_id="client-id",
        public_client_secret="client-secret",
        private_enabled=True,
        private_tenant=None,
        private_base_url="https://api.catapa.com",
        private_access_token=None,
        private_username=None,
        private_password=None,
        oauth_enabled=True,
        authorization_url="https://accounts.catapa.com/oauth2/authorize",
        include=None,
        exclude=None,
    )
    return replace(base, **overrides)


@pytest.fixture(autouse=True)
def _isolated_token_cache(tmp_path, monkeypatch):
    """Point the token cache at a scratch file so tests never touch a real user's cache."""
    monkeypatch.setenv("CATAPA_MCP_TOKEN_CACHE", str(tmp_path / "oauth-token.json"))


def test_token_set_is_valid_reflects_expiry():
    """(OAuthTokenSet.is_valid)

    Condition:
        One token expires in the future, one already expired.

    Expected:
        is_valid() is True for the future token and False for the expired one.
    """
    future = oauth.OAuthTokenSet(access_token="a", refresh_token="r", tenant="t", expires_at=time.time() + 3600)
    past = oauth.OAuthTokenSet(access_token="a", refresh_token="r", tenant="t", expires_at=time.time() - 10)

    assert future.is_valid() is True
    assert past.is_valid() is False


def test_get_access_token_reuses_a_valid_cached_token(monkeypatch):
    """(A valid, non-expired token is already cached)

    Condition:
        The on-disk cache holds a token that expires well in the future.

    Expected:
        get_access_token returns it directly without calling refresh or the interactive login.
    """
    token_set = oauth.OAuthTokenSet(
        access_token="cached-token", refresh_token="cached-refresh", tenant="demo", expires_at=time.time() + 3600
    )
    oauth._save_cached_token(token_set)

    def _fail(*args, **kwargs):
        raise AssertionError("should not be called when a valid token is cached")

    monkeypatch.setattr(oauth, "run_interactive_login", _fail)
    monkeypatch.setattr(oauth, "_refresh_token", _fail)

    access_token, tenant = oauth.get_access_token(_make_settings())

    assert access_token == "cached-token"
    assert tenant == "demo"


def test_get_access_token_refreshes_an_expired_token(monkeypatch):
    """(An expired but refreshable token is cached)

    Condition:
        The cached token is expired but has a refresh_token.

    Expected:
        get_access_token calls the refresh grant and returns the refreshed token, without
        triggering an interactive login.
    """
    expired = oauth.OAuthTokenSet(
        access_token="stale-token", refresh_token="my-refresh-token", tenant="demo", expires_at=time.time() - 10
    )
    oauth._save_cached_token(expired)

    captured = {}

    def _fake_refresh(settings, refresh_token, tenant):
        captured["refresh_token"] = refresh_token
        captured["tenant"] = tenant
        return oauth.OAuthTokenSet(
            access_token="refreshed-token",
            refresh_token="new-refresh-token",
            tenant=tenant,
            expires_at=time.time() + 3600,
        )

    def _fail(*args, **kwargs):
        raise AssertionError("should not fall back to interactive login when refresh succeeds")

    monkeypatch.setattr(oauth, "_refresh_token", _fake_refresh)
    monkeypatch.setattr(oauth, "run_interactive_login", _fail)

    access_token, tenant = oauth.get_access_token(_make_settings())

    assert access_token == "refreshed-token"
    assert tenant == "demo"
    assert captured == {"refresh_token": "my-refresh-token", "tenant": "demo"}


def test_get_access_token_falls_back_to_login_when_refresh_fails(monkeypatch):
    """(A cached refresh token that the server rejects)

    Condition:
        The cached token is expired and _refresh_token raises a RequestException.

    Expected:
        get_access_token falls back to the interactive login flow.
    """
    expired = oauth.OAuthTokenSet(
        access_token="stale-token", refresh_token="dead-refresh-token", tenant="demo", expires_at=time.time() - 10
    )
    oauth._save_cached_token(expired)

    def _raise(*args, **kwargs):
        raise requests.exceptions.RequestException("refresh token revoked")

    def _fake_login(settings):
        return oauth.OAuthTokenSet(
            access_token="fresh-login-token",
            refresh_token="fresh-refresh",
            tenant="demo",
            expires_at=time.time() + 3600,
        )

    monkeypatch.setattr(oauth, "_refresh_token", _raise)
    monkeypatch.setattr(oauth, "run_interactive_login", _fake_login)

    access_token, tenant = oauth.get_access_token(_make_settings())

    assert access_token == "fresh-login-token"
    assert tenant == "demo"


def test_get_access_token_logs_in_when_nothing_is_cached(monkeypatch):
    """(No cached token at all)

    Condition:
        The token cache file doesn't exist.

    Expected:
        get_access_token goes straight to the interactive login flow.
    """

    def _fake_login(settings):
        return oauth.OAuthTokenSet(
            access_token="first-login-token", refresh_token="r", tenant="demo", expires_at=time.time() + 3600
        )

    monkeypatch.setattr(oauth, "run_interactive_login", _fake_login)

    access_token, tenant = oauth.get_access_token(_make_settings())

    assert access_token == "first-login-token"
    assert tenant == "demo"


def test_run_interactive_login_completes_the_loopback_redirect(monkeypatch):
    """(A simulated browser redirect hitting the loopback callback server)

    Condition:
        webbrowser.open is replaced with a fake that, instead of opening a real browser,
        extracts the redirect_uri/state from the authorization URL and fires a GET request at
        the callback in a background thread -- simulating what CATAPA's login page would do
        after a successful login. The token exchange HTTP call is mocked.

    Expected:
        run_interactive_login parses the callback's code/state/tenant, verifies state, exchanges
        the code for a token via the mocked exchange, and caches the result.
    """
    captured_exchange = {}

    def _fake_exchange(settings, code, redirect_uri, tenant):
        captured_exchange.update(code=code, redirect_uri=redirect_uri, tenant=tenant)
        return oauth.OAuthTokenSet(
            access_token="exchanged-token", refresh_token="r", tenant=tenant, expires_at=time.time() + 3600
        )

    monkeypatch.setattr(oauth, "_exchange_code_for_token", _fake_exchange)

    def _fake_browser_open(authorization_url: str) -> bool:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(authorization_url).query)
        redirect_uri = query["redirect_uri"][0]
        state = query["state"][0]
        callback_query = {"code": "auth-code-123", "state": state, "tenant": "demo-tenant"}
        callback_url = f"{redirect_uri}?{urllib.parse.urlencode(callback_query)}"

        def _hit_callback():
            requests.get(callback_url, timeout=5)

        threading.Thread(target=_hit_callback, daemon=True).start()
        return True

    monkeypatch.setattr(oauth.webbrowser, "open", _fake_browser_open)

    token_set = oauth.run_interactive_login(_make_settings())

    assert token_set.access_token == "exchanged-token"
    assert token_set.tenant == "demo-tenant"
    assert captured_exchange["code"] == "auth-code-123"
    assert captured_exchange["tenant"] == "demo-tenant"

    cached = oauth._load_cached_token()
    assert cached is not None
    assert cached.access_token == "exchanged-token"


def test_run_interactive_login_rejects_a_state_mismatch(monkeypatch):
    """(A callback whose state doesn't match what was sent)

    Condition:
        The simulated redirect sends back an unrelated state value.

    Expected:
        run_interactive_login raises OAuthLoginError instead of completing the exchange.
    """

    def _fake_browser_open(authorization_url: str) -> bool:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(authorization_url).query)
        redirect_uri = query["redirect_uri"][0]
        callback_query = {"code": "auth-code-123", "state": "wrong-state", "tenant": "demo-tenant"}
        callback_url = f"{redirect_uri}?{urllib.parse.urlencode(callback_query)}"

        def _hit_callback():
            requests.get(callback_url, timeout=5)

        threading.Thread(target=_hit_callback, daemon=True).start()
        return True

    monkeypatch.setattr(oauth.webbrowser, "open", _fake_browser_open)

    with pytest.raises(oauth.OAuthLoginError, match="state mismatch"):
        oauth.run_interactive_login(_make_settings())
