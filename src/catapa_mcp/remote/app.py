"""Builds the Vercel-deployable, multi-tenant CATAPA MCP server (private API only).

Unlike the stdio server (one shared client, one local login), each request here belongs to an
independently-authenticated CATAPA user: `oauth_provider.CatapaOAuthProvider` resolves each MCP
bearer token back to that caller's own CATAPA credentials, and `private_tools.register_private_tools`
builds a fresh `CatapaPrivate` client per call from them.
"""

from __future__ import annotations

import os

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response

from catapa_mcp import __version__
from catapa_mcp.config import DEFAULT_AUTHORIZATION_URL, DEFAULT_BASE_URL
from catapa_mcp.remote.oauth_provider import SCOPE, CatapaOAuthProvider
from catapa_mcp.remote.private_tools import register_private_tools
from catapa_mcp.remote.store import build_token_store

INSTRUCTIONS = (
    "Tools for the CATAPA private (session-authenticated) HR & payroll API. Each connecting "
    "user authenticates with their own CATAPA account via OAuth; tool calls act as that user. "
    "See https://gdplabs.gitbook.io/catapa/developer-documentation/hris-private-api for paths."
)


def _required_env(name: str) -> str:
    """Read a required environment variable.

    Args:
        name: The variable's name.

    Returns:
        str: Its value.

    Raises:
        RuntimeError: If it isn't set.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set for the remote CATAPA MCP server")
    return value


def build_asgi_app() -> Starlette:
    """Build the Starlette ASGI app: the multi-tenant, private-API-only CATAPA MCP server.

    Returns:
        Starlette: The app, ready to be exposed as `app` for Vercel's Python runtime.
    """
    server_url = _required_env("MCP_SERVER_URL").rstrip("/")
    base_url = os.environ.get("CATAPA_BASE_URL", DEFAULT_BASE_URL)

    provider = CatapaOAuthProvider(
        store=build_token_store(),
        client_id=_required_env("CATAPA_CLIENT_ID"),
        client_secret=_required_env("CATAPA_CLIENT_SECRET"),
        base_url=base_url,
        authorization_url=os.environ.get("CATAPA_AUTHORIZATION_URL", DEFAULT_AUTHORIZATION_URL),
        callback_url=f"{server_url}/catapa/callback",
    )

    server = MCPServer(
        "catapa-mcp-private",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(server_url),
            resource_server_url=AnyHttpUrl(f"{server_url}/mcp"),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        auth_server_provider=provider,
    )

    register_private_tools(server, base_url=os.environ.get("CATAPA_PRIVATE_BASE_URL", base_url))

    @server.custom_route("/catapa/callback", methods=["GET"])
    async def catapa_callback(request: Request) -> Response:
        return await provider.handle_catapa_callback(request)

    return server.streamable_http_app(stateless_http=True, json_response=True)
