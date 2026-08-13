"""Tests for catapa_mcp.remote.oauth_provider.CatapaOAuthProvider.

Condition:
    An in-memory fake TokenStore (no real Redis) and mocked CATAPA HTTP calls (no real CATAPA
    server), driving the provider through the full broker flow: authorize -> CATAPA redirects
    back to our callback -> we mint our own code -> the connecting MCP client exchanges it ->
    later refreshes it -> and eventually revokes it.

Expected:
    Each stage produces the right redirect/token/claims, an authorization code is single-use,
    refresh tokens rotate (the old one stops working), and revoked/expired/tampered tokens are
    rejected by load_access_token/load_refresh_token.
"""

import time
import urllib.parse
from typing import Any

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AccessToken, AuthorizationParams, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

from catapa_mcp.remote import crypto
from catapa_mcp.remote.oauth_provider import CatapaOAuthProvider
from catapa_mcp.remote.store import TokenStore


class _InMemoryStore(TokenStore):
    """A plain-dict TokenStore, for testing the provider's logic in isolation from Redis."""

    def __init__(self):
        self.clients: dict[str, dict] = {}
        self.flows: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.revoked: set[str] = set()

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def save_client(self, client_id, data):
        self.clients[client_id] = data

    async def save_flow(self, flow_id, data):
        self.flows[flow_id] = data

    async def load_flow(self, flow_id):
        return self.flows.pop(flow_id, None)

    async def save_auth_code(self, code, data):
        self.codes[code] = data

    async def load_auth_code(self, code):
        return self.codes.get(code)

    async def delete_auth_code(self, code):
        self.codes.pop(code, None)

    async def revoke_jti(self, jti, expires_at):
        self.revoked.add(jti)

    async def is_jti_revoked(self, jti):
        return jti in self.revoked


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest.fixture
def store() -> _InMemoryStore:
    return _InMemoryStore()


@pytest.fixture
def provider(store) -> CatapaOAuthProvider:
    return CatapaOAuthProvider(
        store=store,
        client_id="catapa-app-client-id",
        client_secret="catapa-app-client-secret",
        base_url="https://api.catapa.com",
        authorization_url="https://accounts.catapa.com/oauth2/authorize",
        callback_url="https://my-deployment.vercel.app/catapa/callback",
    )


@pytest.fixture
def mcp_client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="mcp-client-1",
        redirect_uris=["https://claude.ai/api/mcp/callback"],
    )


def _query(url: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(url)
    return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}


async def test_register_and_get_client_round_trip(provider, mcp_client):
    """(register_client then get_client)

    Expected:
        The retrieved client matches what was registered.
    """
    await provider.register_client(mcp_client)

    result = await provider.get_client(mcp_client.client_id)

    assert result is not None
    assert result.client_id == mcp_client.client_id


async def test_get_unregistered_client_returns_none(provider):
    """(get_client for a client that never registered)

    Expected:
        Returns None.
    """
    assert await provider.get_client("never-registered") is None


async def test_authorize_redirects_to_catapa_and_saves_flow(provider, store, mcp_client):
    """(authorize with a PKCE-bearing request)

    Condition:
        A connecting MCP client (Claude) requests authorization with its own redirect_uri, state,
        and PKCE code_challenge.

    Expected:
        The returned URL points at CATAPA's real authorization endpoint, carrying our app's own
        client_id and a callback redirect_uri pointing back at this deployment (not the MCP
        client's redirect_uri, which CATAPA never sees); the MCP client's original request is
        remembered under that state (our internal flow id) for the callback to pick back up.
    """
    params = AuthorizationParams(
        state="claude-state-abc",
        scopes=["private"],
        code_challenge="challenge-xyz",
        redirect_uri="https://claude.ai/api/mcp/callback",
        redirect_uri_provided_explicitly=True,
    )

    url = await provider.authorize(mcp_client, params)

    assert url.startswith("https://accounts.catapa.com/oauth2/authorize?")
    query = _query(url)
    assert query["client_id"] == "catapa-app-client-id"
    assert query["redirect_uri"] == "https://my-deployment.vercel.app/catapa/callback"
    assert query["response_type"] == "code"

    flow_id = query["state"]
    assert store.flows[flow_id]["client_id"] == mcp_client.client_id
    assert store.flows[flow_id]["state"] == "claude-state-abc"
    assert store.flows[flow_id]["code_challenge"] == "challenge-xyz"


async def _run_authorize(provider, mcp_client, **param_overrides) -> str:
    """Helper: run authorize() and return the flow_id (CATAPA-side state) it generated."""
    defaults: dict[str, Any] = {
        "state": "claude-state-abc",
        "scopes": ["private"],
        "code_challenge": "challenge-xyz",
        "redirect_uri": "https://claude.ai/api/mcp/callback",
        "redirect_uri_provided_explicitly": True,
    }
    defaults.update(param_overrides)
    url = await provider.authorize(mcp_client, AuthorizationParams(**defaults))
    return _query(url)["state"]


class _FakeRequest:
    """A minimal stand-in for starlette.requests.Request, exposing only query_params."""

    def __init__(self, params: dict[str, str]):
        self.query_params = params


async def test_callback_error_redirects_with_error_to_mcp_client(provider, mcp_client):
    """(CATAPA redirects back with ?error=access_denied)

    Expected:
        The user is redirected to the ORIGINAL MCP client's redirect_uri (not CATAPA's), carrying
        the error and the MCP client's own original state.
    """
    flow_id = await _run_authorize(provider, mcp_client)

    response = await provider.handle_catapa_callback(_FakeRequest({"state": flow_id, "error": "access_denied"}))

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://claude.ai/api/mcp/callback?")
    query = _query(location)
    assert query["error"] == "access_denied"
    assert query["state"] == "claude-state-abc"


async def test_callback_with_unknown_state_returns_400(provider):
    """(A callback whose state doesn't match any saved flow)

    Expected:
        A 400 response, not a redirect (there's nowhere safe to redirect to).
    """
    response = await provider.handle_catapa_callback(_FakeRequest({"state": "no-such-flow", "code": "x"}))

    assert response.status_code == 400


async def test_callback_success_mints_mcp_code_and_redirects_to_mcp_client(provider, store, mcp_client, monkeypatch):
    """(A successful CATAPA login redirect)

    Condition:
        CATAPA's token exchange and userinfo lookup are mocked to avoid real HTTP calls.

    Expected:
        The user is redirected to the MCP client's original redirect_uri with a freshly minted
        MCP authorization code and the original state; that code's stored data carries the
        CATAPA credentials obtained from the (mocked) exchange.
    """

    async def fake_exchange(code, tenant):
        assert code == "catapa-auth-code"
        assert tenant == "demo-tenant"
        return {"access_token": "catapa-access-1", "refresh_token": "catapa-refresh-1", "expires_in": 3600}

    async def fake_fetch_subject(access_token, tenant):
        return "user@example.com_demo-tenant"

    monkeypatch.setattr(provider, "_exchange_catapa_code", fake_exchange)
    monkeypatch.setattr(provider, "_fetch_subject", fake_fetch_subject)

    flow_id = await _run_authorize(provider, mcp_client)

    response = await provider.handle_catapa_callback(
        _FakeRequest({"state": flow_id, "code": "catapa-auth-code", "tenant": "demo-tenant"})
    )

    assert response.status_code == 302
    query = _query(response.headers["location"])
    assert response.headers["location"].startswith("https://claude.ai/api/mcp/callback?")
    assert query["state"] == "claude-state-abc"

    mcp_code = query["code"]
    code_data = store.codes[mcp_code]
    assert code_data["catapa_access_token"] == "catapa-access-1"
    assert code_data["catapa_refresh_token"] == "catapa-refresh-1"
    assert code_data["catapa_tenant"] == "demo-tenant"
    assert code_data["subject"] == "user@example.com_demo-tenant"


async def _complete_login(provider, mcp_client, monkeypatch) -> str:
    """Helper: run a full authorize -> callback flow and return the resulting MCP auth code."""

    async def fake_exchange(code, tenant):
        return {"access_token": "catapa-access-1", "refresh_token": "catapa-refresh-1", "expires_in": 3600}

    async def fake_fetch_subject(access_token, tenant):
        return "user@example.com_demo-tenant"

    monkeypatch.setattr(provider, "_exchange_catapa_code", fake_exchange)
    monkeypatch.setattr(provider, "_fetch_subject", fake_fetch_subject)

    flow_id = await _run_authorize(provider, mcp_client)
    response = await provider.handle_catapa_callback(
        _FakeRequest({"state": flow_id, "code": "catapa-auth-code", "tenant": "demo-tenant"})
    )
    return _query(response.headers["location"])["code"]


async def test_authorization_code_is_single_use(provider, mcp_client, monkeypatch):
    """(load then exchange, then load again)

    Expected:
        The code is loadable and exchangeable once; after exchange it's gone.
    """
    mcp_code = await _complete_login(provider, mcp_client, monkeypatch)

    loaded = await provider.load_authorization_code(mcp_client, mcp_code)
    assert loaded is not None

    token = await provider.exchange_authorization_code(mcp_client, loaded)
    assert token.access_token
    assert token.refresh_token

    assert await provider.load_authorization_code(mcp_client, mcp_code) is None


async def test_load_authorization_code_rejects_wrong_client(provider, mcp_client, monkeypatch):
    """(A different client tries to load someone else's code)

    Expected:
        load_authorization_code returns None -- codes are bound to the client that requested them.
    """
    mcp_code = await _complete_login(provider, mcp_client, monkeypatch)
    other_client = OAuthClientInformationFull(client_id="someone-else", redirect_uris=["https://evil.example/cb"])

    assert await provider.load_authorization_code(other_client, mcp_code) is None


async def test_exchanged_access_token_decodes_to_the_right_catapa_claims(provider, mcp_client, monkeypatch):
    """(exchange_authorization_code -> load_access_token)

    Expected:
        The sealed access token, once loaded back, exposes the CATAPA access token/tenant via
        AccessToken.claims -- this is what remote/private_tools.py relies on per request.
    """
    mcp_code = await _complete_login(provider, mcp_client, monkeypatch)
    authorization_code = await provider.load_authorization_code(mcp_client, mcp_code)
    issued = await provider.exchange_authorization_code(mcp_client, authorization_code)

    access_token = await provider.load_access_token(issued.access_token)

    assert access_token is not None
    assert access_token.claims["catapa_access_token"] == "catapa-access-1"
    assert access_token.claims["catapa_tenant"] == "demo-tenant"
    assert access_token.subject == "user@example.com_demo-tenant"


async def test_load_access_token_rejects_expired_token(provider):
    """(A token whose payload's expires_at is in the past)

    Expected:
        load_access_token returns None even though the token decrypts fine -- decryption success
        alone isn't sufficient, expiry must also be checked.
    """
    expired_token = crypto.seal(
        {
            "typ": "access",
            "jti": "jti-expired",
            "client_id": "mcp-client-1",
            "scopes": ["private"],
            "subject": "user_demo",
            "catapa_access_token": "x",
            "catapa_tenant": "demo",
            "expires_at": time.time() - 10,
        }
    )

    assert await provider.load_access_token(expired_token) is None


async def test_load_access_token_rejects_garbage(provider):
    """(A string that isn't a sealed token at all)

    Expected:
        load_access_token returns None rather than raising.
    """
    assert await provider.load_access_token("not-a-real-token") is None


async def test_revoke_token_makes_load_access_token_reject_it(provider, mcp_client, monkeypatch):
    """(revoke_token then load_access_token on the same token)

    Expected:
        The revoked token is rejected even though it hasn't naturally expired yet.
    """
    mcp_code = await _complete_login(provider, mcp_client, monkeypatch)
    authorization_code = await provider.load_authorization_code(mcp_client, mcp_code)
    issued = await provider.exchange_authorization_code(mcp_client, authorization_code)

    assert await provider.load_access_token(issued.access_token) is not None

    await provider.revoke_token(AccessToken(token=issued.access_token, client_id=mcp_client.client_id, scopes=[]))

    assert await provider.load_access_token(issued.access_token) is None


async def test_refresh_rotates_tokens_and_invalidates_the_old_refresh_token(provider, mcp_client, monkeypatch):
    """(exchange_refresh_token)

    Condition:
        CATAPA's own refresh grant is mocked to return a new CATAPA access/refresh token pair.

    Expected:
        A new MCP access+refresh token pair is issued reflecting the new CATAPA credentials, and
        the OLD refresh token is rotated out -- load_refresh_token on it afterwards returns None.
    """
    mcp_code = await _complete_login(provider, mcp_client, monkeypatch)
    authorization_code = await provider.load_authorization_code(mcp_client, mcp_code)
    issued = await provider.exchange_authorization_code(mcp_client, authorization_code)

    old_refresh_token = await provider.load_refresh_token(mcp_client, issued.refresh_token)
    assert old_refresh_token is not None

    async def fake_refresh(refresh_token, tenant):
        assert refresh_token == "catapa-refresh-1"
        assert tenant == "demo-tenant"
        return {"access_token": "catapa-access-2", "refresh_token": "catapa-refresh-2", "expires_in": 3600}

    monkeypatch.setattr(provider, "_refresh_catapa_token", fake_refresh)

    rotated = await provider.exchange_refresh_token(mcp_client, old_refresh_token, scopes=[])

    new_access = await provider.load_access_token(rotated.access_token)
    assert new_access.claims["catapa_access_token"] == "catapa-access-2"

    assert await provider.load_refresh_token(mcp_client, issued.refresh_token) is None


async def test_load_refresh_token_rejects_wrong_client(provider, mcp_client, monkeypatch):
    """(A different client tries to use someone else's refresh token)

    Expected:
        load_refresh_token returns None.
    """
    mcp_code = await _complete_login(provider, mcp_client, monkeypatch)
    authorization_code = await provider.load_authorization_code(mcp_client, mcp_code)
    issued = await provider.exchange_authorization_code(mcp_client, authorization_code)

    other_client = OAuthClientInformationFull(client_id="someone-else", redirect_uris=["https://evil.example/cb"])

    assert await provider.load_refresh_token(other_client, issued.refresh_token) is None


async def test_revoke_token_accepts_a_refresh_token_too(provider):
    """(revoke_token called with a RefreshToken instance rather than an AccessToken)

    Expected:
        No error -- revoke_token works for either token type, matching the Protocol's contract.
    """
    token_str = crypto.seal(
        {
            "typ": "refresh",
            "jti": "jti-r1",
            "client_id": "c1",
            "scopes": ["private"],
            "subject": "s",
            "catapa_refresh_token": "x",
            "catapa_tenant": "demo",
            "expires_at": time.time() + 3600,
        }
    )

    await provider.revoke_token(RefreshToken(token=token_str, client_id="c1", scopes=["private"]))

    assert "jti-r1" in provider.store.revoked
