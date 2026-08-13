"""Vercel entrypoint: exposes the multi-tenant, private-API-only CATAPA MCP server.

Vercel's Python runtime auto-detects an ASGI/WSGI app named `app` in this file.
"""

from catapa_mcp.remote.app import build_asgi_app

app = build_asgi_app()
