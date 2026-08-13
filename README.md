# catapa-mcp

An [MCP](https://modelcontextprotocol.io) server exposing [CATAPA](https://catapa.com)'s HR & payroll APIs as tools, built on top of the official [`catapa`](https://pypi.org/project/catapa/) (public, OAuth2) and [`catapa-private`](https://pypi.org/project/catapa-private/) (private, session-authenticated) Python SDKs.

## What it exposes

- **Public API tools** (`catapa_*`) -- one MCP tool per resource operation in the `catapa` SDK, generated automatically at startup by walking the SDK's resource tree (`catapa.resource_registry`). This covers ~190 resources (employees, payroll, time management, analytics, ...) and several hundred operations in total. Tool names follow the SDK's own path, e.g. `catapa_core_employees_list`, `catapa_core_cost_centers_create`.
- **Private API tools** (`catapa_private_*`) -- the `catapa-private` SDK is a thin, session-authenticated HTTP client rather than a per-endpoint client, so it's wrapped 1:1 as seven generic tools: `catapa_private_get`, `catapa_private_post`, `catapa_private_put`, `catapa_private_patch`, `catapa_private_delete`, `catapa_private_get_all` (auto-paginating), and `catapa_private_session_status`. See the [private API docs](https://gdplabs.gitbook.io/catapa/developer-documentation/hris-private-api) for available paths.

Each half is independent -- set up credentials for one, both, or neither (an unconfigured half is simply skipped, with a warning logged to stderr).

## Install

```bash
pip install -e .
# or: uv sync
```

Requires Python 3.11-3.13.

## Configure

Copy `.env.example` to `.env` and fill in credentials, or set the environment variables directly wherever the server runs (e.g. in your MCP client's config).

```bash
# Public API: either an access token, or OAuth2 client credentials
CATAPA_TENANT=your-tenant
CATAPA_ACCESS_TOKEN=...
# or
CATAPA_CLIENT_ID=...
CATAPA_CLIENT_SECRET=...

# Private API: either an access token, or username/password (session auth)
CATAPA_PRIVATE_ACCESS_TOKEN=...
# or
CATAPA_PRIVATE_USERNAME=...
CATAPA_PRIVATE_PASSWORD=...
```

See `.env.example` for the full list, including `CATAPA_MCP_INCLUDE` / `CATAPA_MCP_EXCLUDE` for scoping the public API's tool count down to specific resource namespaces (e.g. `CATAPA_MCP_INCLUDE=core.employees,timemanagement`), and `CATAPA_MCP_ENABLE_PUBLIC` / `CATAPA_MCP_ENABLE_PRIVATE` for turning either half off entirely.

## Run

```bash
catapa-mcp
# or
python -m catapa_mcp
```

The server speaks MCP over stdio.

### Claude Desktop / Claude Code

Add to your MCP client's config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "catapa": {
      "command": "catapa-mcp",
      "env": {
        "CATAPA_TENANT": "your-tenant",
        "CATAPA_ACCESS_TOKEN": "...",
        "CATAPA_PRIVATE_ACCESS_TOKEN": "..."
      }
    }
  }
}
```

## How the public API tools are generated

The `catapa` SDK exposes a fluent resource tree (`client.core.employees.list(...)`) backed by an auto-generated OpenAPI client, where every operation method is fully typed -- including nested pydantic request/response models. `src/catapa_mcp/public_tools.py` walks that tree (`catapa.resource_registry.ROOT_RESOURCES`) at startup, and for every operation:

1. Copies the SDK method's own signature (minus transport-only kwargs like `_headers`) onto a thin async wrapper function.
2. Registers that wrapper as an MCP tool -- the MCP server derives the tool's JSON schema straight from the wrapper's type hints, so nested pydantic models become nested JSON schema automatically.
3. On invocation, validates/coerces the tool call's arguments back into the SDK's own types, calls the real SDK method, and serializes the (often pydantic) response back to JSON.

This means the tool surface tracks the SDK automatically -- upgrading `catapa` picks up new/changed endpoints without any code changes here.

## Development

```bash
uv sync --group dev   # or: pip install -e . pytest pytest-asyncio ruff
pytest
ruff check .
ruff format .
```
