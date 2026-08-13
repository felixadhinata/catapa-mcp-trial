"""Seals CATAPA credentials into self-contained, encrypted MCP OAuth tokens.

Vercel functions are stateless between invocations, so instead of a server-side token table,
each MCP-issued access/refresh token IS the (encrypted) CATAPA credential, decrypted in-memory
on every request -- no database read needed on the hot path. Only whoever holds
`MCP_TOKEN_ENCRYPTION_KEY` can decrypt them, so that key must be treated as a master credential
for every connected user's CATAPA access.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class TokenCryptoError(Exception):
    """Raised when a sealed token can't be decrypted, was tampered with, or is malformed."""


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """Build the Fernet cipher from `MCP_TOKEN_ENCRYPTION_KEY`.

    Returns:
        Fernet: The cipher used to seal/unseal tokens.

    Raises:
        TokenCryptoError: If the environment variable is unset or not a valid Fernet key.
    """
    key = os.environ.get("MCP_TOKEN_ENCRYPTION_KEY")
    if not key:
        raise TokenCryptoError("MCP_TOKEN_ENCRYPTION_KEY is not set")
    try:
        return Fernet(key.encode())
    except ValueError as e:
        raise TokenCryptoError("MCP_TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from e


def seal(payload: dict[str, Any]) -> str:
    """Encrypt a JSON-serializable payload into an opaque bearer token string.

    Args:
        payload: The data to seal (e.g. a CATAPA access token, tenant, and expiry).

    Returns:
        str: The encrypted, URL-safe token string.
    """
    return _fernet().encrypt(json.dumps(payload).encode()).decode()


def unseal(token: str) -> dict[str, Any]:
    """Decrypt a token string back into its payload.

    Args:
        token: A string previously returned by `seal`.

    Returns:
        dict[str, Any]: The original payload.

    Raises:
        TokenCryptoError: If the token is invalid, tampered with, or not one of ours.
    """
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, ValueError, UnicodeDecodeError) as e:
        raise TokenCryptoError("Invalid or tampered token") from e
