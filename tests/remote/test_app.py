"""Tests for catapa_mcp.remote.app.build_asgi_app.

Condition:
    Required environment variables set to fake-but-well-formed values (no real Vercel/Upstash/
    CATAPA infrastructure needed -- `RedisTokenStore`'s underlying client doesn't connect until
    an actual Redis command is issued, so app construction alone stays network-free).

Expected:
    build_asgi_app() fails loudly and specifically when a required variable is missing, and
    otherwise produces a working ASGI app whose home page confirms the deployment is reachable
    without requiring an OAuth login first.
"""

import pytest
from starlette.testclient import TestClient

from catapa_mcp import __version__
from catapa_mcp.remote import app as app_module
from catapa_mcp.remote.private_tools import PRIVATE_TOOL_NAMES


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_URL", "https://catapa-mcp-demo.vercel.app")
    monkeypatch.setenv("CATAPA_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("CATAPA_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://fake.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "fake-token")


def test_build_asgi_app_requires_mcp_server_url(monkeypatch):
    """(MCP_SERVER_URL unset)

    Expected:
        Raises RuntimeError naming the missing variable, rather than building a broken app.
    """
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)

    with pytest.raises(RuntimeError, match="MCP_SERVER_URL"):
        app_module.build_asgi_app()


def test_build_asgi_app_requires_catapa_client_id(monkeypatch):
    """(CATAPA_CLIENT_ID unset)

    Expected:
        Raises RuntimeError naming the missing variable.
    """
    monkeypatch.delenv("CATAPA_CLIENT_ID", raising=False)

    with pytest.raises(RuntimeError, match="CATAPA_CLIENT_ID"):
        app_module.build_asgi_app()


def test_home_page_confirms_the_deployment_is_up():
    """(GET / with no authentication)

    Condition:
        The app is built with valid config; the home page is requested without any OAuth token.

    Expected:
        A 200 response (the home page is a public, unauthenticated custom_route) that names the
        MCP endpoint, links the OAuth discovery document, and lists every exposed private tool --
        so visiting it in a browser is enough to confirm the deployment is actually serving
        requests, without needing to drive a full connector/OAuth flow first.
    """
    app = app_module.build_asgi_app()
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert __version__ in response.text
    assert "https://catapa-mcp-demo.vercel.app/mcp" in response.text
    assert "/.well-known/oauth-authorization-server" in response.text
    for tool_name in PRIVATE_TOOL_NAMES:
        assert tool_name in response.text


def test_expected_routes_are_registered():
    """(The full route table of the built app)

    Expected:
        Every route the OAuth broker and MCP protocol need is present: discovery, authorize,
        token, register, revoke, the MCP endpoint itself, and the CATAPA callback.
    """
    app = app_module.build_asgi_app()

    paths = {getattr(route, "path", None) for route in app.routes}

    assert paths >= {
        "/",
        "/.well-known/oauth-authorization-server",
        "/authorize",
        "/token",
        "/register",
        "/revoke",
        "/mcp",
        "/catapa/callback",
    }
