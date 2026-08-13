"""Tests for catapa_mcp.remote.store.

Condition:
    A `RedisTokenStore` backed by an in-memory fake standing in for Upstash Redis (so no real
    Redis instance is required), exercising every operation the OAuth broker depends on.

Expected:
    Client registrations persist; flow state and authorization codes are single-read/single-use
    (consumed on load/delete); revocation records are queryable; `build_token_store` dispatches
    on `CATAPA_MCP_STORE_BACKEND` and rejects unknown backends.
"""

import time

import pytest

from catapa_mcp.remote.store import RedisTokenStore, TokenStore, build_token_store


class _FakeRedis:
    """A minimal in-memory stand-in for the Upstash Redis client's async interface."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.data[key] = value

    async def delete(self, *keys: str):
        for key in keys:
            self.data.pop(key, None)


@pytest.fixture
def store() -> RedisTokenStore:
    return RedisTokenStore(redis=_FakeRedis())


async def test_client_registration_round_trips(store):
    """(save_client then get_client)

    Condition:
        A client registration is saved.

    Expected:
        get_client returns the same data back.
    """
    await store.save_client("client-1", {"client_id": "client-1", "client_name": "Claude"})

    result = await store.get_client("client-1")

    assert result == {"client_id": "client-1", "client_name": "Claude"}


async def test_get_unknown_client_returns_none(store):
    """(get_client for a never-registered client)

    Condition:
        No client has been saved under this ID.

    Expected:
        get_client returns None.
    """
    assert await store.get_client("nonexistent") is None


async def test_flow_is_consumed_on_load(store):
    """(save_flow then load_flow twice)

    Condition:
        A login flow's state is saved, then loaded once.

    Expected:
        The first load returns the data; a second load returns None, since the flow record
        is single-use (deleted once consumed) to prevent CATAPA-callback replay.
    """
    await store.save_flow("flow-1", {"client_id": "c1"})

    first = await store.load_flow("flow-1")
    second = await store.load_flow("flow-1")

    assert first == {"client_id": "c1"}
    assert second is None


async def test_load_unknown_flow_returns_none(store):
    """(load_flow for an unknown flow id)

    Expected:
        Returns None rather than raising.
    """
    assert await store.load_flow("never-saved") is None


async def test_auth_code_load_does_not_consume_but_delete_does(store):
    """(save_auth_code, load_auth_code twice, then delete_auth_code)

    Condition:
        An authorization code's data is saved.

    Expected:
        load_auth_code can be called repeatedly without consuming it (the OAuth handler needs to
        read it before deciding to exchange it); delete_auth_code removes it afterwards.
    """
    await store.save_auth_code("code-1", {"client_id": "c1"})

    assert await store.load_auth_code("code-1") == {"client_id": "c1"}
    assert await store.load_auth_code("code-1") == {"client_id": "c1"}

    await store.delete_auth_code("code-1")

    assert await store.load_auth_code("code-1") is None


async def test_revocation_round_trips(store):
    """(revoke_jti then is_jti_revoked)

    Condition:
        A token's jti is revoked.

    Expected:
        is_jti_revoked reports True for it and False for an unrelated jti.
    """
    await store.revoke_jti("jti-1", expires_at=time.time() + 3600)

    assert await store.is_jti_revoked("jti-1") is True
    assert await store.is_jti_revoked("jti-2") is False


def test_build_token_store_defaults_to_redis(monkeypatch):
    """(CATAPA_MCP_STORE_BACKEND unset)

    Expected:
        build_token_store() returns a RedisTokenStore instance without requiring real Upstash
        credentials to be present (construction only fails on first actual Redis call).
    """
    monkeypatch.delenv("CATAPA_MCP_STORE_BACKEND", raising=False)
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://example.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "fake-token")

    result = build_token_store()

    assert isinstance(result, RedisTokenStore)
    assert isinstance(result, TokenStore)


def test_build_token_store_rejects_unknown_backend(monkeypatch):
    """(CATAPA_MCP_STORE_BACKEND=mariadb, not yet implemented)

    Expected:
        build_token_store() raises ValueError naming the unknown backend, rather than silently
        falling back to Redis.
    """
    monkeypatch.setenv("CATAPA_MCP_STORE_BACKEND", "mariadb")

    with pytest.raises(ValueError, match="mariadb"):
        build_token_store()
