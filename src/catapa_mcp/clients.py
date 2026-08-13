"""Builds authenticated CATAPA SDK client instances from Settings."""

from __future__ import annotations

import logging

from catapa import Catapa
from catapa_private import CatapaPrivate

from catapa_mcp import oauth
from catapa_mcp.config import Settings

logger = logging.getLogger(__name__)


def build_public_client(settings: Settings) -> Catapa | None:
    """Build the public CATAPA API client, if credentials are configured.

    Args:
        settings: The server configuration.

    Returns:
        Catapa | None: The configured client, or None if credentials are missing/disabled.
    """
    if not settings.has_public_credentials():
        logger.warning("Public CATAPA credentials not configured; skipping public API tools")
        return None

    if settings.oauth_enabled:
        access_token, tenant = oauth.get_access_token(settings)
        return Catapa(tenant=tenant, base_url=settings.public_base_url, access_token=access_token)

    assert settings.public_tenant is not None  # noqa: S101 -- guaranteed by has_public_credentials()

    if settings.public_access_token:
        return Catapa(
            tenant=settings.public_tenant,
            base_url=settings.public_base_url,
            access_token=settings.public_access_token,
        )

    return Catapa(
        tenant=settings.public_tenant,
        base_url=settings.public_base_url,
        client_id=settings.public_client_id,
        client_secret=settings.public_client_secret,
    )


def build_private_client(settings: Settings) -> CatapaPrivate | None:
    """Build the private CATAPA API client, if credentials are configured.

    Args:
        settings: The server configuration.

    Returns:
        CatapaPrivate | None: The configured client, or None if credentials are missing/disabled.
    """
    if not settings.has_private_credentials():
        logger.warning("Private CATAPA credentials not configured; skipping private API tools")
        return None

    if settings.oauth_enabled:
        access_token, tenant = oauth.get_access_token(settings)
        return CatapaPrivate(tenant=tenant, base_url=settings.private_base_url, access_token=access_token)

    assert settings.private_tenant is not None  # noqa: S101 -- guaranteed by has_private_credentials()

    if settings.private_access_token:
        return CatapaPrivate(
            tenant=settings.private_tenant,
            base_url=settings.private_base_url,
            access_token=settings.private_access_token,
        )

    return CatapaPrivate(
        tenant=settings.private_tenant,
        base_url=settings.private_base_url,
        username=settings.private_username,
        password=settings.private_password,
    )
