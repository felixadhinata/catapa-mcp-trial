"""Tests for catapa_mcp.remote.private_tools.

Condition:
    Each tool is invoked with a fake CatapaPrivate class swapped in and the request's
    authenticated CATAPA credentials set via the SDK's real auth contextvar (the same mechanism
    the streamable-HTTP auth middleware uses in production) -- so these tests exercise the actual
    per-request credential resolution path, not a mocked shortcut around it.

Expected:
    Each tool builds its CatapaPrivate client from the current request's sealed CATAPA
    credentials (tenant + access token), not a shared/global client; a request with no
    authenticated context raises instead of silently calling CATAPA unauthenticated.
"""

from typing import Any

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from catapa_mcp.remote import private_tools


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_body: Any = None):
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self._json_body = json_body

    def json(self) -> Any:
        return self._json_body


class _FakeCatapaPrivate:
    """Records the tenant/access_token it was constructed with, and every call made."""

    instances: list["_FakeCatapaPrivate"] = []

    def __init__(self, *, tenant: str, base_url: str, access_token: str):
        self.tenant = tenant
        self.base_url = base_url
        self.access_token = access_token
        self.calls: list[tuple[str, tuple, dict]] = []
        _FakeCatapaPrivate.instances.append(self)

    def _record(self, method: str, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        return _FakeResponse(json_body={"ok": True})

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
        return [{"id": "1"}]

    def is_session_valid(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _fake_catapa_private(monkeypatch):
    _FakeCatapaPrivate.instances = []
    monkeypatch.setattr(private_tools, "CatapaPrivate", _FakeCatapaPrivate)
    yield
    _FakeCatapaPrivate.instances = []


@pytest.fixture
def server() -> MCPServer:
    server = MCPServer("test", warn_on_duplicate_tools=False)
    private_tools.register_private_tools(server, base_url="https://api.catapa.com")
    return server


class _AuthContext:
    """Context manager that sets the auth contextvar like the SDK's real middleware would."""

    def __init__(self, tenant: str, access_token: str):
        self.tenant = tenant
        self.access_token = access_token
        self._token = None

    def __enter__(self):
        user = AuthenticatedUser(
            AccessToken(
                token="sealed-token-does-not-matter-here",
                client_id="mcp-client-1",
                scopes=["private"],
                claims={"catapa_access_token": self.access_token, "catapa_tenant": self.tenant},
            )
        )
        self._token = auth_context_var.set(user)

    def __exit__(self, *exc_info):
        auth_context_var.reset(self._token)


async def test_registers_seven_tools(server):
    """(Registration count)

    Expected:
        Exactly the seven documented tools are registered, matching the stdio server's set.
    """
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


async def test_tool_call_builds_client_from_the_requests_own_credentials(server):
    """(catapa_private_get, called as a specific authenticated user)

    Expected:
        The CatapaPrivate client built for this call uses THIS request's tenant/access_token,
        not a shared global client.
    """
    with _AuthContext(tenant="tenant-a", access_token="token-a"):
        result = await server.call_tool("catapa_private_get", {"path": "/core/employees"})

    assert result.is_error is False
    assert len(_FakeCatapaPrivate.instances) == 1
    assert _FakeCatapaPrivate.instances[0].tenant == "tenant-a"
    assert _FakeCatapaPrivate.instances[0].access_token == "token-a"


async def test_two_requests_from_different_users_get_isolated_clients(server):
    """(Two sequential calls under different authenticated users)

    Expected:
        Each call builds its own client scoped to that call's own user -- no credential leakage
        between requests, the core multi-tenancy guarantee.
    """
    with _AuthContext(tenant="tenant-a", access_token="token-a"):
        await server.call_tool("catapa_private_get", {"path": "/core/employees"})

    with _AuthContext(tenant="tenant-b", access_token="token-b"):
        await server.call_tool("catapa_private_get", {"path": "/core/employees"})

    assert [i.tenant for i in _FakeCatapaPrivate.instances] == ["tenant-a", "tenant-b"]
    assert [i.access_token for i in _FakeCatapaPrivate.instances] == ["token-a", "token-b"]


async def test_post_forwards_json_body(server):
    """(catapa_private_post)

    Expected:
        The underlying client.post is called with the supplied JSON body.
    """
    body = {"attendanceStatusInId": "status-id"}
    with _AuthContext(tenant="tenant-a", access_token="token-a"):
        result = await server.call_tool("catapa_private_post", {"path": "/timemanagement/jobs", "json": body})

    assert result.is_error is False
    expected_call = ("post", ("/timemanagement/jobs",), {"json": body, "params": None})
    assert _FakeCatapaPrivate.instances[0].calls[0] == expected_call


async def test_get_all_and_session_status(server):
    """(catapa_private_get_all and catapa_private_session_status)

    Expected:
        Both succeed and route through the per-request client.
    """
    with _AuthContext(tenant="tenant-a", access_token="token-a"):
        get_all_result = await server.call_tool("catapa_private_get_all", {"path": "/core/employees"})
        status_result = await server.call_tool("catapa_private_session_status", {})

    assert get_all_result.is_error is False
    assert status_result.is_error is False


async def test_tool_call_without_authentication_fails(server):
    """(A tool call with no authenticated context set)

    Condition:
        This should be unreachable in production -- the streamable HTTP transport requires auth
        on every tool call -- but the tool must fail loudly rather than calling CATAPA
        unauthenticated if it somehow is.

    Expected:
        The call errors instead of silently building a client with no credentials.
    """
    with pytest.raises(ToolError):
        await server.call_tool("catapa_private_get", {"path": "/core/employees"})

    assert len(_FakeCatapaPrivate.instances) == 0


def test_client_from_request_raises_tool_error_without_auth():
    """(_client_from_request called directly, no auth context)

    Expected:
        Raises ToolError rather than returning a client with placeholder/empty credentials.
    """
    with pytest.raises(ToolError):
        private_tools._client_from_request(base_url="https://api.catapa.com")
