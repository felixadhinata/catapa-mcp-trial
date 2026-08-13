"""Builds the CATAPA MCP server: registers public and private API tools based on config."""

from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer

from catapa_mcp import __version__
from catapa_mcp.clients import build_private_client, build_public_client
from catapa_mcp.config import Settings
from catapa_mcp.private_tools import register_private_tools
from catapa_mcp.public_tools import register_public_tools

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Tools for the CATAPA HR & payroll platform. `catapa_*` tools wrap the public, "
    "OAuth2-authenticated API -- one tool per resource operation (e.g. "
    "`catapa_core_employees_list`). `catapa_private_*` tools wrap the session-authenticated "
    "private API as generic HTTP verbs against a `path` argument; see "
    "https://gdplabs.gitbook.io/catapa/developer-documentation/hris-private-api for available paths."
)


def build_server(settings: Settings | None = None) -> MCPServer:
    """Construct the MCP server and register all available CATAPA tools.

    Args:
        settings: Configuration to use. Defaults to reading from the environment.

    Returns:
        MCPServer: The configured, not-yet-running server.
    """
    settings = settings or Settings.from_env()
    server = MCPServer(
        "catapa-mcp",
        version=__version__,
        instructions=INSTRUCTIONS,
        warn_on_duplicate_tools=False,
    )

    registered = 0

    public_client = build_public_client(settings)
    if public_client is not None:
        registered += register_public_tools(server, public_client, include=settings.include, exclude=settings.exclude)

    private_client = build_private_client(settings)
    if private_client is not None:
        registered += register_private_tools(server, private_client)

    if registered == 0:
        logger.warning(
            "No CATAPA tools registered. Set CATAPA_ACCESS_TOKEN or "
            "CATAPA_CLIENT_ID+CATAPA_CLIENT_SECRET for the public API, and/or "
            "CATAPA_PRIVATE_ACCESS_TOKEN or CATAPA_PRIVATE_USERNAME+CATAPA_PRIVATE_PASSWORD "
            "for the private API."
        )
    else:
        logger.info("Registered %d CATAPA MCP tools", registered)

    return server
