# catapa-mcp

An [MCP](https://modelcontextprotocol.io) server exposing [CATAPA](https://catapa.com)'s HR & payroll APIs as tools, built on top of the official [`catapa`](https://pypi.org/project/catapa/) (public, OAuth2) and [`catapa-private`](https://pypi.org/project/catapa-private/) (private, session-authenticated) Python SDKs.

## What it exposes

- **Public API tools** (`catapa_*`) -- one MCP tool per resource operation in the `catapa` SDK, generated automatically at startup by walking the SDK's resource tree (`catapa.resource_registry`). This covers ~190 resources (employees, payroll, time management, analytics, ...) and several hundred operations in total. Tool names follow the SDK's own path, e.g. `catapa_core_employees_list`, `catapa_core_cost_centers_create`.
- **Private API tools** (`catapa_private_*`) -- the `catapa-private` SDK is a thin, session-authenticated HTTP client rather than a per-endpoint client, so it's wrapped 1:1 as seven generic tools: `catapa_private_get`, `catapa_private_post`, `catapa_private_put`, `catapa_private_patch`, `catapa_private_delete`, `catapa_private_get_all` (auto-paginating), and `catapa_private_session_status`. See the [private API docs](https://gdplabs.gitbook.io/catapa/developer-documentation/hris-private-api) for available paths.

Each half is independent -- set up credentials for one, both, or neither (an unconfigured half is simply skipped, with a warning logged to stderr).

### OAuth login (recommended)

`catapa-private` has no OAuth of its own -- only a direct username/password login -- but its client also accepts a static bearer token, and CATAPA's *public* API already has a real, browser-redirect OAuth2 authorization-code flow. Setting `CATAPA_MCP_AUTH_MODE=oauth` uses that flow to authenticate **both** clients with a single login: the first time the server starts (or whenever the cached token can't be silently refreshed), it opens your browser to CATAPA's hosted login page, waits for the redirect on a local loopback server, exchanges the resulting code for an access/refresh token pair, and caches it to `~/.catapa-mcp/oauth-token.json` for future launches. See `src/catapa_mcp/oauth.py`.

## Install

```bash
pip install -e .
# or: uv sync
```

Requires Python 3.11-3.13.

## Configure

Copy `.env.example` to `.env` and fill in credentials, or set the environment variables directly wherever the server runs (e.g. in your MCP client's config).

```bash
# Recommended: a single interactive OAuth login for both APIs (opens your browser)
CATAPA_MCP_AUTH_MODE=oauth
CATAPA_CLIENT_ID=...
CATAPA_CLIENT_SECRET=...

# Or, per-API credentials:

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

## Remote deployment (Vercel, private API only, multi-tenant)

`src/catapa_mcp/remote/` is a separate, Streamable-HTTP MCP server for deploying to Vercel so multiple people/orgs can connect without each running the server locally. It intentionally only exposes the `catapa_private_*` tools -- it does not port the public API's ~300 generated tools.

Unlike the stdio server (one shared login, one local token cache), each connecting user authenticates with their **own** CATAPA account:

1. The MCP client (Claude) starts an OAuth flow against this deployment.
2. This deployment redirects the user's browser to CATAPA's real, hosted login page (there's no separate "private API OAuth" -- CATAPA only has OAuth on the public API side, so that's what's used; see `src/catapa_mcp/remote/oauth_provider.py`).
3. Once CATAPA redirects back, the resulting CATAPA access/refresh token is sealed (encrypted, via `src/catapa_mcp/remote/crypto.py`) directly into the MCP token handed back to the client -- there is no per-user token table.
4. Every subsequent tool call decrypts that request's own token to build a `CatapaPrivate` client scoped to that specific user (`src/catapa_mcp/remote/private_tools.py`), so different users' requests never share credentials.

The only persistent storage needed is for OAuth client registrations and the few-seconds-lived login handshake (`src/catapa_mcp/remote/store.py`, backed by Upstash Redis via the Vercel Marketplace integration). Storage is behind the `TokenStore` abstract interface specifically so a future move to Postgres/MariaDB/etc. is a new subclass wired into `build_token_store()`, not a rewrite.

### Deploying

1. Attach an Upstash Redis store to the Vercel project (Marketplace tab) -- this sets `UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN` automatically.
2. Set these environment variables in the Vercel project:

   ```bash
   MCP_SERVER_URL=https://your-app.vercel.app   # this deployment's own public URL
   CATAPA_CLIENT_ID=...
   CATAPA_CLIENT_SECRET=...
   MCP_TOKEN_ENCRYPTION_KEY=...   # generate: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   `MCP_TOKEN_ENCRYPTION_KEY` decrypts every connected user's CATAPA credentials -- treat it as a master secret, and don't rotate it casually (rotating logs everyone out). `CATAPA_BASE_URL`, `CATAPA_AUTHORIZATION_URL`, and `CATAPA_PRIVATE_BASE_URL` are optional overrides with the same defaults as the stdio server.

3. Deploy. `api/index.py` exposes the ASGI app Vercel's Python runtime auto-detects; `vercel.json` routes all paths to it; `.python-version` pins Python 3.12, since Vercel's Python runtime only supports 3.12+.
4. Visit `https://your-app.vercel.app/` in a browser -- a status page confirms the deployment is actually up (version, the MCP endpoint URL, a link to the OAuth discovery document, and the list of exposed tools) before you try connecting a client to it.

**Vercel gotcha (confirmed, not hypothetical):** Vercel's zero-config Python detection bundles the whole project into a single function, but mounts it at the fixed path `/python` -- not at `/api/index` (the file-based path you'd expect from `api/index.py`). `vercel.json`'s `rewrites` destination must point at `/python`; pointing it at `/api/index` (a very natural first guess) silently 404s on every route, since the deployment still reports "Ready" -- there's no build error to notice. If a fresh deploy 404s everywhere, check the deployment's Resources tab for the actual Function `Path` column and match `rewrites` to it.

**Other caveats, since this hasn't been fully tested against real CATAPA infrastructure:**
- `CATAPA_AUTHORIZATION_URL`'s default (`https://accounts.catapa.com/oauth2/authorize`) is an unverified guess mirroring CATAPA's dev-environment naming; override it if wrong.

### Connecting a client

The deployment speaks standard MCP Streamable HTTP with OAuth 2.1 (dynamic client registration, authorization-code flow) at `https://your-app.vercel.app/mcp` -- any compliant MCP client can add it as a remote connector. Two concrete examples:

**Claude (claude.ai web or Claude Desktop):** Settings -> Connectors -> Add custom connector -> paste `https://your-app.vercel.app/mcp` -> Connect. Claude discovers OAuth support automatically from `/.well-known/oauth-authorization-server` and opens a browser to CATAPA's login page.

**Claude Code (CLI):**
```bash
claude mcp add --transport http catapa https://your-app.vercel.app/mcp
```
Authentication isn't triggered by `add` itself -- run `claude mcp list` (shows `! Needs authentication`), then inside a session run `/mcp`, select the server, and choose "Authenticate"; your browser opens for the CATAPA login, and status flips to `✔ Connected`. Add `--scope user` instead of the default `--scope local` to make it available across all your projects rather than just the current one.

**ChatGPT:** requires a Plus/Pro/Business/Enterprise/Education account with **Developer Mode** enabled first (Settings -> Apps -> Advanced settings, or Settings -> Connectors -> Advanced -> Developer Mode, depending on account type -- the "Add custom connector" button only appears once this is on). Then Settings -> Connectors -> add a custom connector, paste the `/mcp` URL, and select **OAuth** as the authentication type; ChatGPT triggers the browser login on first use. (Verified via web search against OpenAI's help center rather than a direct fetch of their docs from this environment -- double-check against ChatGPT's own Settings UI if the menu names have since moved.)

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
