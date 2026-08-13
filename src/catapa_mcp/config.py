"""Environment-driven configuration for the CATAPA MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.catapa.com"
DEFAULT_AUTHORIZATION_URL = "https://accounts.catapa.com/oauth2/authorize"


def _split_csv(value: str | None) -> list[str] | None:
    """Split a comma-separated environment value into a list.

    Args:
        value: The raw environment value, or None.

    Returns:
        list[str] | None: The trimmed, non-empty parts, or None if value was unset/blank.
    """
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _flag(value: str | None, default: bool) -> bool:
    """Parse a boolean-ish environment value.

    Args:
        value: The raw environment value, or None.
        default: The value to use when unset.

    Returns:
        bool: The parsed flag.
    """
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Runtime configuration loaded from environment variables.

    Two independent credential sets are supported: one for the public, OAuth2-authenticated
    `catapa` SDK, and one for the session-authenticated `catapa-private` SDK. Either half can
    be left unconfigured; that half's tools are simply not registered.

    A third mode, `oauth_enabled`, replaces both of the above with a single interactive OAuth2
    authorization-code login (browser redirect via CATAPA's real OAuth server). CATAPA's private
    API has no OAuth of its own, but its client accepts a static bearer token, so the one access
    token obtained this way authenticates both the public and private clients -- see
    `catapa_mcp.oauth`.
    """

    public_enabled: bool
    public_tenant: str | None
    public_base_url: str
    public_access_token: str | None
    public_client_id: str | None
    public_client_secret: str | None

    private_enabled: bool
    private_tenant: str | None
    private_base_url: str
    private_access_token: str | None
    private_username: str | None
    private_password: str | None

    oauth_enabled: bool
    authorization_url: str

    include: list[str] | None
    exclude: list[str] | None

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the process environment.

        Returns:
            Settings: The parsed configuration.
        """
        tenant = os.environ.get("CATAPA_TENANT")
        base_url = os.environ.get("CATAPA_BASE_URL", DEFAULT_BASE_URL)

        return cls(
            public_enabled=_flag(os.environ.get("CATAPA_MCP_ENABLE_PUBLIC"), True),
            public_tenant=os.environ.get("CATAPA_PUBLIC_TENANT", tenant),
            public_base_url=os.environ.get("CATAPA_PUBLIC_BASE_URL", base_url),
            public_access_token=os.environ.get("CATAPA_ACCESS_TOKEN"),
            public_client_id=os.environ.get("CATAPA_CLIENT_ID"),
            public_client_secret=os.environ.get("CATAPA_CLIENT_SECRET"),
            private_enabled=_flag(os.environ.get("CATAPA_MCP_ENABLE_PRIVATE"), True),
            private_tenant=os.environ.get("CATAPA_PRIVATE_TENANT", tenant),
            private_base_url=os.environ.get("CATAPA_PRIVATE_BASE_URL", base_url),
            private_access_token=os.environ.get("CATAPA_PRIVATE_ACCESS_TOKEN"),
            private_username=os.environ.get("CATAPA_PRIVATE_USERNAME"),
            private_password=os.environ.get("CATAPA_PRIVATE_PASSWORD"),
            oauth_enabled=os.environ.get("CATAPA_MCP_AUTH_MODE", "").strip().lower() == "oauth",
            authorization_url=os.environ.get("CATAPA_AUTHORIZATION_URL", DEFAULT_AUTHORIZATION_URL),
            include=_split_csv(os.environ.get("CATAPA_MCP_INCLUDE")),
            exclude=_split_csv(os.environ.get("CATAPA_MCP_EXCLUDE")),
        )

    def has_oauth_credentials(self) -> bool:
        """Check whether enough configuration exists to run the interactive OAuth login.

        Returns:
            bool: True if OAuth mode is enabled and a client id/secret pair is set. CATAPA's OAuth
                server only exists for the public API's client_id/client_secret; there is no
                separate private-API OAuth client.
        """
        return self.oauth_enabled and bool(self.public_client_id and self.public_client_secret)

    def has_public_credentials(self) -> bool:
        """Check whether enough configuration exists to build the public client.

        Returns:
            bool: True if a tenant is set and either an access token or a client id/secret pair is set,
                or if OAuth login is configured.
        """
        if not self.public_enabled:
            return False
        if self.oauth_enabled:
            return self.has_oauth_credentials()
        if not self.public_tenant:
            return False
        if self.public_access_token:
            return True
        return bool(self.public_client_id and self.public_client_secret)

    def has_private_credentials(self) -> bool:
        """Check whether enough configuration exists to build the private client.

        Returns:
            bool: True if a tenant is set and either an access token or a username/password pair is set,
                or if OAuth login is configured (the OAuth access token also authenticates the private API).
        """
        if not self.private_enabled:
            return False
        if self.oauth_enabled:
            return self.has_oauth_credentials()
        if not self.private_tenant:
            return False
        if self.private_access_token:
            return True
        return bool(self.private_username and self.private_password)
