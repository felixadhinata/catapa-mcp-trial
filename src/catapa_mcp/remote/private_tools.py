"""Registers the CATAPA private-API tools for the multi-tenant remote server.

Unlike the stdio server's `catapa_mcp.private_tools` (one shared `CatapaPrivate` client, built
once at startup), each call here may belong to a different logged-in CATAPA user: the OAuth
broker in `oauth_provider.py` seals that user's CATAPA access token into the MCP bearer token
they present, and `mcp.server.auth.middleware.auth_context.get_access_token()` exposes it (via
`AccessToken.claims`) for the duration of the request. Every tool call builds its own
short-lived `CatapaPrivate` client from those claims instead of closing over a fixed one.
"""

from __future__ import annotations

from typing import Any

from catapa_private import CatapaPrivate
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError

from catapa_mcp.private_tools import JsonDict, _to_result

PRIVATE_TOOL_NAMES = (
    "catapa_private_get",
    "catapa_private_post",
    "catapa_private_put",
    "catapa_private_patch",
    "catapa_private_delete",
    "catapa_private_get_all",
    "catapa_private_session_status",
)


def _client_from_request(base_url: str) -> CatapaPrivate:
    """Build a `CatapaPrivate` client from the current request's authenticated CATAPA credentials.

    Args:
        base_url: The CATAPA private API base URL.

    Returns:
        CatapaPrivate: A client authenticated as the current MCP caller's CATAPA user.

    Raises:
        ToolError: If the request isn't authenticated (should be unreachable -- the server
            requires auth on every tool call -- but fails loudly rather than silently if it is).
    """
    access_token = get_access_token()
    if access_token is None or not access_token.claims:
        raise ToolError("Not authenticated with CATAPA")
    return CatapaPrivate(
        tenant=access_token.claims["catapa_tenant"],
        base_url=base_url,
        access_token=access_token.claims["catapa_access_token"],
    )


def register_private_tools(server: Any, base_url: str) -> int:
    """Register the per-request CATAPA private API tools.

    Args:
        server: The `MCPServer` to register tools on.
        base_url: The CATAPA private API base URL.

    Returns:
        int: The number of tools registered.
    """

    async def catapa_private_get(path: str, params: JsonDict | None = None) -> JsonDict:
        """Send a GET request to a CATAPA private API path.

        Args:
            path: API path relative to the base URL, e.g. "/timemanagement/attendance-statuses".
            params: Optional query parameters.
        """
        return _to_result(_client_from_request(base_url).get(path, params=params))

    async def catapa_private_post(path: str, json: JsonDict | None = None, params: JsonDict | None = None) -> JsonDict:
        """Send a POST request with a JSON body to a CATAPA private API path.

        Args:
            path: API path relative to the base URL.
            json: Optional JSON request body.
            params: Optional query parameters.
        """
        return _to_result(_client_from_request(base_url).post(path, json=json, params=params))

    async def catapa_private_put(path: str, json: JsonDict | None = None, params: JsonDict | None = None) -> JsonDict:
        """Send a PUT request with a JSON body to a CATAPA private API path.

        Args:
            path: API path relative to the base URL.
            json: Optional JSON request body.
            params: Optional query parameters.
        """
        return _to_result(_client_from_request(base_url).put(path, json=json, params=params))

    async def catapa_private_patch(path: str, json: JsonDict | None = None, params: JsonDict | None = None) -> JsonDict:
        """Send a PATCH request with a JSON body to a CATAPA private API path.

        Args:
            path: API path relative to the base URL.
            json: Optional JSON request body.
            params: Optional query parameters.
        """
        return _to_result(_client_from_request(base_url).patch(path, json=json, params=params))

    async def catapa_private_delete(path: str, params: JsonDict | None = None) -> JsonDict:
        """Send a DELETE request to a CATAPA private API path.

        Args:
            path: API path relative to the base URL.
            params: Optional query parameters.
        """
        return _to_result(_client_from_request(base_url).delete(path, params=params))

    async def catapa_private_get_all(path: str, params: JsonDict | None = None) -> list[JsonDict]:
        """GET a CATAPA private API path and auto-paginate through every page.

        Args:
            path: API path relative to the base URL.
            params: Optional query parameters.
        """
        return _client_from_request(base_url).get_all(path, params=params)

    async def catapa_private_session_status() -> JsonDict:
        """Check whether the current CATAPA private API session is still valid."""
        return {"session_valid": _client_from_request(base_url).is_session_valid()}

    tool_functions = (
        catapa_private_get,
        catapa_private_post,
        catapa_private_put,
        catapa_private_patch,
        catapa_private_delete,
        catapa_private_get_all,
        catapa_private_session_status,
    )
    for tool_function in tool_functions:
        server.add_tool(tool_function, name=tool_function.__name__)

    return len(tool_functions)
