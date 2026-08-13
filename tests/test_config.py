"""Tests for catapa_mcp.config.Settings.

Condition:
    Various combinations of CATAPA_* environment variables.

Expected:
    Settings.from_env parses them correctly and has_*_credentials reflects what's usable.
"""

from catapa_mcp.config import Settings


def test_from_env_defaults_to_disabled_without_credentials(monkeypatch):
    """(Settings with no CATAPA env vars set)

    Condition:
        No CATAPA_* environment variables are set.

    Expected:
        Neither public nor private credentials are considered available.
    """
    monkeypatch.delenv("CATAPA_TENANT", raising=False)
    monkeypatch.delenv("CATAPA_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CATAPA_CLIENT_ID", raising=False)
    monkeypatch.delenv("CATAPA_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("CATAPA_PRIVATE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CATAPA_PRIVATE_USERNAME", raising=False)
    monkeypatch.delenv("CATAPA_PRIVATE_PASSWORD", raising=False)

    settings = Settings.from_env()

    assert settings.has_public_credentials() is False
    assert settings.has_private_credentials() is False


def test_public_access_token_is_sufficient(monkeypatch):
    """(Settings with only a public access token)

    Condition:
        CATAPA_TENANT and CATAPA_ACCESS_TOKEN are set; no client id/secret.

    Expected:
        has_public_credentials() is True.
    """
    monkeypatch.setenv("CATAPA_TENANT", "demo")
    monkeypatch.setenv("CATAPA_ACCESS_TOKEN", "token-123")
    monkeypatch.delenv("CATAPA_CLIENT_ID", raising=False)
    monkeypatch.delenv("CATAPA_CLIENT_SECRET", raising=False)

    settings = Settings.from_env()

    assert settings.has_public_credentials() is True


def test_public_client_credentials_require_both_id_and_secret(monkeypatch):
    """(Settings with only a client id, no secret)

    Condition:
        CATAPA_TENANT and CATAPA_CLIENT_ID are set, but CATAPA_CLIENT_SECRET is not.

    Expected:
        has_public_credentials() is False.
    """
    monkeypatch.setenv("CATAPA_TENANT", "demo")
    monkeypatch.delenv("CATAPA_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("CATAPA_CLIENT_ID", "id-123")
    monkeypatch.delenv("CATAPA_CLIENT_SECRET", raising=False)

    settings = Settings.from_env()

    assert settings.has_public_credentials() is False


def test_private_username_password_is_sufficient(monkeypatch):
    """(Settings with private username/password)

    Condition:
        CATAPA_TENANT, CATAPA_PRIVATE_USERNAME, and CATAPA_PRIVATE_PASSWORD are set.

    Expected:
        has_private_credentials() is True.
    """
    monkeypatch.setenv("CATAPA_TENANT", "demo")
    monkeypatch.delenv("CATAPA_PRIVATE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("CATAPA_PRIVATE_USERNAME", "user")
    monkeypatch.setenv("CATAPA_PRIVATE_PASSWORD", "pass")

    settings = Settings.from_env()

    assert settings.has_private_credentials() is True


def test_enable_flags_disable_credential_sets(monkeypatch):
    """(Settings with CATAPA_MCP_ENABLE_PUBLIC=false)

    Condition:
        Valid public credentials are set, but CATAPA_MCP_ENABLE_PUBLIC=false.

    Expected:
        has_public_credentials() is False regardless of the credentials present.
    """
    monkeypatch.setenv("CATAPA_TENANT", "demo")
    monkeypatch.setenv("CATAPA_ACCESS_TOKEN", "token-123")
    monkeypatch.setenv("CATAPA_MCP_ENABLE_PUBLIC", "false")

    settings = Settings.from_env()

    assert settings.has_public_credentials() is False


def test_oauth_mode_requires_client_id_and_secret_but_not_tenant(monkeypatch):
    """(Settings with CATAPA_MCP_AUTH_MODE=oauth)

    Condition:
        CATAPA_MCP_AUTH_MODE=oauth and a client id/secret are set, but no CATAPA_TENANT.

    Expected:
        Both has_public_credentials() and has_private_credentials() are True -- OAuth mode
        discovers the tenant from the login callback, so it isn't required upfront.
    """
    monkeypatch.setenv("CATAPA_MCP_AUTH_MODE", "oauth")
    monkeypatch.delenv("CATAPA_TENANT", raising=False)
    monkeypatch.setenv("CATAPA_CLIENT_ID", "id-123")
    monkeypatch.setenv("CATAPA_CLIENT_SECRET", "secret-123")
    monkeypatch.delenv("CATAPA_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CATAPA_PRIVATE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CATAPA_PRIVATE_USERNAME", raising=False)
    monkeypatch.delenv("CATAPA_PRIVATE_PASSWORD", raising=False)

    settings = Settings.from_env()

    assert settings.oauth_enabled is True
    assert settings.has_public_credentials() is True
    assert settings.has_private_credentials() is True


def test_oauth_mode_without_client_credentials_is_unusable(monkeypatch):
    """(Settings with CATAPA_MCP_AUTH_MODE=oauth but no client id/secret)

    Condition:
        CATAPA_MCP_AUTH_MODE=oauth is set, but CATAPA_CLIENT_ID/CATAPA_CLIENT_SECRET are not.

    Expected:
        Neither half is usable, since CATAPA's OAuth server has no separate private-API client.
    """
    monkeypatch.setenv("CATAPA_MCP_AUTH_MODE", "oauth")
    monkeypatch.delenv("CATAPA_CLIENT_ID", raising=False)
    monkeypatch.delenv("CATAPA_CLIENT_SECRET", raising=False)

    settings = Settings.from_env()

    assert settings.has_public_credentials() is False
    assert settings.has_private_credentials() is False


def test_authorization_url_defaults_and_override(monkeypatch):
    """(CATAPA_AUTHORIZATION_URL unset vs. set)

    Condition:
        First unset, then set to a custom value.

    Expected:
        Settings.authorization_url falls back to the documented default, then honors the override.
    """
    monkeypatch.delenv("CATAPA_AUTHORIZATION_URL", raising=False)
    assert Settings.from_env().authorization_url == "https://accounts.catapa.com/oauth2/authorize"

    monkeypatch.setenv("CATAPA_AUTHORIZATION_URL", "https://accounts-development.catapa.com/oauth2/authorize")
    assert Settings.from_env().authorization_url == "https://accounts-development.catapa.com/oauth2/authorize"


def test_include_exclude_parsing(monkeypatch):
    """(Settings with CATAPA_MCP_INCLUDE/EXCLUDE set)

    Condition:
        CATAPA_MCP_INCLUDE and CATAPA_MCP_EXCLUDE are comma-separated lists.

    Expected:
        Both are parsed into trimmed lists of strings.
    """
    monkeypatch.setenv("CATAPA_MCP_INCLUDE", "core.employees, timemanagement")
    monkeypatch.setenv("CATAPA_MCP_EXCLUDE", "anomalydetection")

    settings = Settings.from_env()

    assert settings.include == ["core.employees", "timemanagement"]
    assert settings.exclude == ["anomalydetection"]
