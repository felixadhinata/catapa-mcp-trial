"""Tests for catapa_mcp.private_tools.register_private_tools.

Condition:
    A stub client shaped like catapa_private.CatapaPrivate (get/post/put/patch/delete/
    get_all/is_session_valid), so tests don't require real CATAPA credentials or network
    access.

Expected:
    All seven generic HTTP-verb tools are registered and correctly delegate to the
    underlying client methods with the arguments supplied through the MCP tool call.
"""

from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from catapa_mcp.private_tools import register_private_tools


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_body: Any = None, text: str = ""):
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self._json_body = json_body
        self.text = text

    def json(self) -> Any:
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


class _FakeCatapaPrivate:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, method: str, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        return _FakeResponse(json_body={"ok": True, "method": method})

    def get(self, url, **kwargs):
        return self._record("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._record("post", url, **kwargs)

    def put(self, url, **kwargs):
        return self._record("put", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._record("patch", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._record("delete", url, **kwargs)

    def get_all(self, url, *, params=None):
        self.calls.append(("get_all", (url,), {"params": params}))
        return [{"id": "1"}, {"id": "2"}]

    def is_session_valid(self) -> bool:
        return True


@pytest.fixture
def client() -> _FakeCatapaPrivate:
    return _FakeCatapaPrivate()


@pytest.fixture
def server(client) -> MCPServer:
    server = MCPServer("test", warn_on_duplicate_tools=False)
    register_private_tools(server, client)
    return server


async def test_registers_seven_tools(client):
    """(Registration count)

    Condition:
        A fresh server and stub client.

    Expected:
        Exactly the seven documented tools are registered.
    """
    server = MCPServer("test", warn_on_duplicate_tools=False)

    count = register_private_tools(server, client)

    assert count == 7
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == {
        "catapa_private_get",
        "catapa_private_post",
        "catapa_private_put",
        "catapa_private_patch",
        "catapa_private_delete",
        "catapa_private_get_all",
        "catapa_private_session_status",
    }


async def test_get_forwards_path_and_params(server, client):
    """(catapa_private_get)

    Condition:
        Called with a path and query params.

    Expected:
        The underlying client.get is called with the same path and params, and the tool
        result carries the response status/body.
    """
    result = await server.call_tool(
        "catapa_private_get",
        {"path": "/timemanagement/attendance-statuses", "params": {"query": "(code:CUTPOT)"}},
    )

    assert result.is_error is False
    assert client.calls == [("get", ("/timemanagement/attendance-statuses",), {"params": {"query": "(code:CUTPOT)"}})]


async def test_post_forwards_json_body(server, client):
    """(catapa_private_post)

    Condition:
        Called with a path and a JSON body.

    Expected:
        The underlying client.post is called with json= set to the supplied body.
    """
    body = {"attendanceStatusInId": "status-id", "startDate": "2025-01-01"}

    result = await server.call_tool(
        "catapa_private_post", {"path": "/timemanagement/jobs/missing-attendance-process", "json": body}
    )

    assert result.is_error is False
    assert client.calls[0] == (
        "post",
        ("/timemanagement/jobs/missing-attendance-process",),
        {"json": body, "params": None},
    )


async def test_get_all_returns_every_page(server, client):
    """(catapa_private_get_all)

    Condition:
        The stub's get_all returns two records.

    Expected:
        The tool result reflects both records without wrapping them further.
    """
    result = await server.call_tool("catapa_private_get_all", {"path": "/core/employees"})

    assert result.is_error is False


async def test_session_status_reports_validity(server):
    """(catapa_private_session_status)

    Condition:
        The stub's is_session_valid() returns True.

    Expected:
        The tool call succeeds and does not error.
    """
    result = await server.call_tool("catapa_private_session_status", {})

    assert result.is_error is False
