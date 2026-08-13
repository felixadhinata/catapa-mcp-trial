"""Tests for catapa_mcp.remote.crypto.

Condition:
    `MCP_TOKEN_ENCRYPTION_KEY` set to a valid Fernet key (or deliberately unset/invalid).

Expected:
    A sealed payload round-trips back to the original data; tampered, foreign, or malformed
    tokens are rejected; a missing/invalid encryption key fails loudly rather than silently.
"""

import pytest
from cryptography.fernet import Fernet

from catapa_mcp.remote import crypto


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    """Use a fresh, valid Fernet key for every test, and reset the cached cipher."""
    monkeypatch.setenv("MCP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


def test_seal_and_unseal_round_trips():
    """(A JSON-serializable payload)

    Condition:
        A dict with nested/typed values is sealed then unsealed.

    Expected:
        The unsealed payload is byte-for-byte equal to the original.
    """
    payload = {"catapa_access_token": "abc123", "catapa_tenant": "demo", "expires_at": 12345.6}

    token = crypto.seal(payload)
    result = crypto.unseal(token)

    assert result == payload


def test_unseal_rejects_tampered_token():
    """(A sealed token with a flipped character)

    Condition:
        One character of a validly sealed token is altered.

    Expected:
        unseal raises TokenCryptoError instead of returning corrupted data.
    """
    token = crypto.seal({"a": 1})
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    with pytest.raises(crypto.TokenCryptoError):
        crypto.unseal(tampered)


def test_unseal_rejects_token_from_a_different_key(monkeypatch):
    """(A token sealed under a different encryption key)

    Condition:
        A payload is sealed with one key; the environment then switches to a different key.

    Expected:
        unseal with the new key raises TokenCryptoError.
    """
    token = crypto.seal({"a": 1})

    monkeypatch.setenv("MCP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()

    with pytest.raises(crypto.TokenCryptoError):
        crypto.unseal(token)


def test_missing_encryption_key_raises(monkeypatch):
    """(MCP_TOKEN_ENCRYPTION_KEY unset)

    Condition:
        The environment variable is not set.

    Expected:
        seal() raises TokenCryptoError rather than silently using a default key.
    """
    monkeypatch.delenv("MCP_TOKEN_ENCRYPTION_KEY", raising=False)
    crypto._fernet.cache_clear()

    with pytest.raises(crypto.TokenCryptoError):
        crypto.seal({"a": 1})


def test_invalid_encryption_key_raises(monkeypatch):
    """(MCP_TOKEN_ENCRYPTION_KEY set to garbage)

    Condition:
        The environment variable is set to a string that isn't a valid Fernet key.

    Expected:
        seal() raises TokenCryptoError.
    """
    monkeypatch.setenv("MCP_TOKEN_ENCRYPTION_KEY", "not-a-valid-key")
    crypto._fernet.cache_clear()

    with pytest.raises(crypto.TokenCryptoError):
        crypto.seal({"a": 1})
