"""Storage for the multi-tenant OAuth broker, behind a backend-agnostic interface.

Only two things need real persistence across serverless invocations: registered OAuth clients
(one record per distinct connecting MCP app, e.g. Claude Desktop/Code/claude.ai -- low churn)
and the few-seconds-lived state of an in-flight CATAPA login redirect. Per-user CATAPA
credentials are NOT stored here -- they're sealed directly into the MCP tokens themselves
(see `crypto.py`), so there is no user-token table to manage.

`TokenStore` is the storage-agnostic contract the rest of the OAuth broker (`oauth_provider.py`,
`app.py`) depends on -- it knows nothing about Redis. `RedisTokenStore` is the one implementation
today (backed by Upstash, via its Vercel Marketplace integration). To move to Postgres, MariaDB,
or anything else, write a new `TokenStore` subclass implementing the same methods and swap it in
`build_token_store()`; nothing else in the codebase needs to change.
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Protocol

FLOW_TTL_SECONDS = 600
AUTH_CODE_TTL_SECONDS = 60


class TokenStore(ABC):
    """Storage contract for OAuth client registrations, login-flow state, and revocations.

    Every method here describes a domain-level operation, not a storage command -- a SQL-backed
    implementation maps these onto tables/rows just as naturally as a key-value one maps them
    onto keys. Callers outside this module should only ever depend on this class, never on a
    concrete backend.
    """

    @abstractmethod
    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        """Look up a previously registered OAuth client.

        Args:
            client_id: The client's ID.

        Returns:
            dict[str, Any] | None: The client's registration data, or None if unknown.
        """

    @abstractmethod
    async def save_client(self, client_id: str, data: dict[str, Any]) -> None:
        """Persist an OAuth client registration.

        Args:
            client_id: The client's ID.
            data: The client's registration data (JSON-serializable).
        """

    @abstractmethod
    async def save_flow(self, flow_id: str, data: dict[str, Any]) -> None:
        """Store the state of an in-flight CATAPA login redirect.

        Args:
            flow_id: A random ID identifying this login attempt (used as CATAPA's OAuth `state`).
            data: The flow state to remember until the CATAPA callback arrives.
        """

    @abstractmethod
    async def load_flow(self, flow_id: str) -> dict[str, Any] | None:
        """Load and consume (delete) the state of an in-flight CATAPA login redirect.

        Args:
            flow_id: The flow ID from `save_flow`.

        Returns:
            dict[str, Any] | None: The stored flow state, or None if expired/unknown.
        """

    @abstractmethod
    async def save_auth_code(self, code: str, data: dict[str, Any]) -> None:
        """Store a newly minted MCP authorization code's data.

        Args:
            code: The opaque authorization code handed to the connecting MCP client.
            data: The code's data (client_id, PKCE challenge, sealed CATAPA credentials, ...).
        """

    @abstractmethod
    async def load_auth_code(self, code: str) -> dict[str, Any] | None:
        """Look up an authorization code's data without consuming it.

        Args:
            code: The authorization code.

        Returns:
            dict[str, Any] | None: The code's data, or None if expired/unknown.
        """

    @abstractmethod
    async def delete_auth_code(self, code: str) -> None:
        """Consume (delete) an authorization code so it can't be replayed.

        Args:
            code: The authorization code to delete.
        """

    @abstractmethod
    async def revoke_jti(self, jti: str, expires_at: float) -> None:
        """Mark a token's `jti` as revoked until its natural expiry.

        Args:
            jti: The token's unique ID.
            expires_at: The token's own expiry (epoch seconds); the revocation record only needs
                to outlive the token, since after that it would be rejected as expired anyway.
        """

    @abstractmethod
    async def is_jti_revoked(self, jti: str) -> bool:
        """Check whether a token's `jti` has been revoked.

        Args:
            jti: The token's unique ID.

        Returns:
            bool: True if revoked.
        """


class _RedisLike(Protocol):
    """The subset of the Upstash Redis client `RedisTokenStore` depends on."""

    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: str, ex: int | None = None) -> Any: ...
    async def delete(self, *keys: str) -> Any: ...


class RedisTokenStore(TokenStore):
    """`TokenStore` backed by Upstash Redis (via the Vercel Marketplace integration)."""

    _CLIENT_KEY_PREFIX = "catapa-mcp:client:"
    _FLOW_KEY_PREFIX = "catapa-mcp:flow:"
    _AUTH_CODE_KEY_PREFIX = "catapa-mcp:code:"
    _REVOKED_KEY_PREFIX = "catapa-mcp:revoked:"

    def __init__(self, redis: _RedisLike | None = None) -> None:
        """Initialize the store.

        Args:
            redis: An Upstash Redis client. Defaults to `AsyncRedis.from_env()`, which reads
                `UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN` (set automatically by the
                Upstash Vercel Marketplace integration).
        """
        if redis is None:
            from upstash_redis import AsyncRedis

            redis = AsyncRedis.from_env()
        self._redis: _RedisLike = redis

    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        raw = await self._redis.get(f"{self._CLIENT_KEY_PREFIX}{client_id}")
        return json.loads(raw) if raw else None

    async def save_client(self, client_id: str, data: dict[str, Any]) -> None:
        await self._redis.set(f"{self._CLIENT_KEY_PREFIX}{client_id}", json.dumps(data))

    async def save_flow(self, flow_id: str, data: dict[str, Any]) -> None:
        await self._redis.set(f"{self._FLOW_KEY_PREFIX}{flow_id}", json.dumps(data), ex=FLOW_TTL_SECONDS)

    async def load_flow(self, flow_id: str) -> dict[str, Any] | None:
        raw = await self._redis.get(f"{self._FLOW_KEY_PREFIX}{flow_id}")
        if not raw:
            return None
        await self._redis.delete(f"{self._FLOW_KEY_PREFIX}{flow_id}")
        return json.loads(raw)

    async def save_auth_code(self, code: str, data: dict[str, Any]) -> None:
        await self._redis.set(f"{self._AUTH_CODE_KEY_PREFIX}{code}", json.dumps(data), ex=AUTH_CODE_TTL_SECONDS)

    async def load_auth_code(self, code: str) -> dict[str, Any] | None:
        raw = await self._redis.get(f"{self._AUTH_CODE_KEY_PREFIX}{code}")
        return json.loads(raw) if raw else None

    async def delete_auth_code(self, code: str) -> None:
        await self._redis.delete(f"{self._AUTH_CODE_KEY_PREFIX}{code}")

    async def revoke_jti(self, jti: str, expires_at: float) -> None:
        ttl = max(1, int(expires_at - time.time()))
        await self._redis.set(f"{self._REVOKED_KEY_PREFIX}{jti}", "1", ex=ttl)

    async def is_jti_revoked(self, jti: str) -> bool:
        return bool(await self._redis.get(f"{self._REVOKED_KEY_PREFIX}{jti}"))


def build_token_store() -> TokenStore:
    """Build the `TokenStore` backend selected by `CATAPA_MCP_STORE_BACKEND`.

    This is the single place a new backend gets wired in: add an `elif` branch and a new
    `TokenStore` subclass, and nothing in `oauth_provider.py` or `app.py` needs to change.

    Returns:
        TokenStore: The configured store.

    Raises:
        ValueError: If `CATAPA_MCP_STORE_BACKEND` names an unknown backend.
    """
    backend = os.environ.get("CATAPA_MCP_STORE_BACKEND", "redis").strip().lower()
    if backend == "redis":
        return RedisTokenStore()
    raise ValueError(f"Unknown CATAPA_MCP_STORE_BACKEND: {backend!r} (supported: 'redis')")
