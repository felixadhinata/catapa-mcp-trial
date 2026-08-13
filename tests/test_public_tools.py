"""Tests for catapa_mcp.public_tools.register_public_tools.

Condition:
    A real (network-inert) Catapa client, constructed with a static access token so no
    OAuth2 handshake occurs.

Expected:
    Every resource operation across the SDK's resource tree is registered as a distinct,
    schema-valid MCP tool, and include/exclude filters narrow that set as documented.
"""

import functools
import json

import pytest
from catapa import Catapa
from mcp.server.mcpserver import MCPServer

from catapa_mcp.public_tools import register_public_tools


@pytest.fixture
def client() -> Catapa:
    """A Catapa client using a static token, so no network call happens on construction."""
    return Catapa(tenant="test-tenant", access_token="fake-token")


async def test_registers_every_operation_with_no_errors(client):
    """(Full, unfiltered registration)

    Condition:
        include/exclude are left unset.

    Expected:
        A large number of uniquely-named tools are registered, matching the count of
        distinct (namespace, operation) pairs in the SDK's resource tree.
    """
    server = MCPServer("test", warn_on_duplicate_tools=False)

    count = register_public_tools(server, client)

    assert count > 100
    tools = await server.list_tools()
    assert len({tool.name for tool in tools}) == count


async def test_registered_tool_schemas_are_json_serializable(client):
    """(Schema sanity check)

    Condition:
        Tools are registered without include/exclude filtering.

    Expected:
        Every generated tool's input schema round-trips through JSON without error.
    """
    server = MCPServer("test", warn_on_duplicate_tools=False)
    register_public_tools(server, client)

    tools = await server.list_tools()
    for tool in tools:
        json.dumps(tool.input_schema)


async def test_include_filter_narrows_to_matching_namespaces(client):
    """(include=["core.cost_centers"])

    Condition:
        include is set to a single, known resource namespace.

    Expected:
        Only tools for that namespace are registered.
    """
    server = MCPServer("test", warn_on_duplicate_tools=False)

    count = register_public_tools(server, client, include=["core.cost_centers"])

    assert count > 0
    tools = await server.list_tools()
    assert all(tool.name.startswith("catapa_core_cost_centers_") for tool in tools)


async def test_exclude_filter_removes_matching_namespaces(client):
    """(exclude=["anomalydetection"])

    Condition:
        exclude is set to a namespace prefix present in the tree.

    Expected:
        No registered tool belongs to that namespace.
    """
    server = MCPServer("test", warn_on_duplicate_tools=False)

    register_public_tools(server, client, exclude=["anomalydetection"])

    tools = await server.list_tools()
    assert not any(tool.name.startswith("catapa_anomalydetection_") for tool in tools)


async def test_call_tool_validates_and_invokes_with_a_pydantic_body(client, monkeypatch):
    """(Calling a tool whose operation takes a pydantic request body)

    Condition:
        catapa_core_cost_centers_create expects a `cost_center_request` body model. The
        underlying SDK call is monkeypatched to avoid a real HTTP request.

    Expected:
        The dict argument supplied through the MCP tool call is validated into the SDK's
        pydantic model before the underlying method is invoked, and the call succeeds.
    """
    api_instance = client.core.cost_centers._get_api_instance()
    captured = {}

    @functools.wraps(api_instance.create)
    def fake_create(**kwargs):
        captured.update(kwargs)
        return {"id": "generated-id"}

    # Patch before registering: the tool wrapper closes over whatever method is bound on the
    # (cached) api instance at registration time. functools.wraps preserves the original
    # method's signature (via __wrapped__), matching what a real SDK method looks like --
    # a bare `**kwargs` fake would (correctly) register as a zero-argument tool instead.
    monkeypatch.setattr(api_instance, "create", fake_create)

    server = MCPServer("test", warn_on_duplicate_tools=False)
    register_public_tools(server, client, include=["core.cost_centers"])

    result = await server.call_tool(
        "catapa_core_cost_centers_create",
        {"cost_center_request": {"code": "C1", "name": "Cost Center 1"}},
    )

    assert result.is_error is False
    assert captured["cost_center_request"].code == "C1"
