"""Registers MCP tools that wrap the CATAPA private (session-authenticated) HTTP client.

Unlike the public `catapa` SDK, `catapa-private` does not generate one method per endpoint --
it is a thin, session-authenticated HTTP client (`get`/`post`/`put`/`patch`/`delete`/`get_all`)
that callers point at a path. Tools here mirror that shape 1:1 instead of inventing
per-endpoint tools. Available paths are documented at
https://gdplabs.gitbook.io/catapa/developer-documentation/hris-private-api
"""

from __future__ import annotations

from typing import Any

from catapa_private import CatapaPrivate

JsonDict = dict[str, Any]


def _to_result(response: Any) -> JsonDict:
    """Convert a `requests.Response` into a JSON-serializable dict.

    Args:
        response: The HTTP response returned by the CatapaPrivate client.

    Returns:
        dict: `status_code`, `headers`, and the parsed JSON `body` (or raw text if not JSON).
    """
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    return {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": body,
    }


def register_private_tools(server: Any, client: CatapaPrivate) -> int:
    """Register generic HTTP-verb tools for the CATAPA private API.

    Args:
        server: The `MCPServer` to register tools on.
        client: An authenticated `catapa_private.CatapaPrivate` client.

    Returns:
        int: The number of tools registered.
    """

    async def catapa_private_get(path: str, params: JsonDict | None = None) -> JsonDict:
        """Send a GET request to a CATAPA private API path.

        Args:
            path: API path relative to the base URL, e.g. "/timemanagement/attendance-statuses".
            params: Optional query parameters.
        """
        return _to_result(client.get(path, params=params))

    async def catapa_private_post(path: str, json: JsonDict | None = None, params: JsonDict | None = None) -> JsonDict:
        """Send a POST request with a JSON body to a CATAPA private API path.

        Args:
            path: API path relative to the base URL.
            json: Optional JSON request body.
            params: Optional query parameters.
        """
        return _to_result(client.post(path, json=json, params=params))

    async def catapa_private_put(path: str, json: JsonDict | None = None, params: JsonDict | None = None) -> JsonDict:
        """Send a PUT request with a JSON body to a CATAPA private API path.

        Args:
            path: API path relative to the base URL.
            json: Optional JSON request body.
            params: Optional query parameters.
        """
        return _to_result(client.put(path, json=json, params=params))

    async def catapa_private_patch(path: str, json: JsonDict | None = None, params: JsonDict | None = None) -> JsonDict:
        """Send a PATCH request with a JSON body to a CATAPA private API path.

        Args:
            path: API path relative to the base URL.
            json: Optional JSON request body.
            params: Optional query parameters.
        """
        return _to_result(client.patch(path, json=json, params=params))

    async def catapa_private_delete(path: str, params: JsonDict | None = None) -> JsonDict:
        """Send a DELETE request to a CATAPA private API path.

        Args:
            path: API path relative to the base URL.
            params: Optional query parameters.
        """
        return _to_result(client.delete(path, params=params))

    async def catapa_private_get_all(path: str, params: JsonDict | None = None) -> list[JsonDict]:
        """GET a CATAPA private API path and auto-paginate through every page.

        Args:
            path: API path relative to the base URL.
            params: Optional query parameters.
        """
        return client.get_all(path, params=params)

    async def catapa_private_session_status() -> JsonDict:
        """Check whether the current CATAPA private API session is still valid."""
        return {"session_valid": client.is_session_valid()}

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
