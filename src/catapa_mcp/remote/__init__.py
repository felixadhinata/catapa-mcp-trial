"""Multi-tenant, Vercel-deployable CATAPA MCP server (private API only).

Unlike `catapa_mcp`'s stdio server -- one local process, one shared login, one on-disk token
cache -- this subpackage serves many independently-authenticated users over Streamable HTTP.
Each connecting MCP client's user logs in to CATAPA on their own via the OAuth broker in
`oauth_provider.py`; their CATAPA credentials are sealed into the MCP token itself (see
`crypto.py`) rather than kept in server-side session state, since Vercel functions are
stateless between invocations. The only persistent store needed (`store.py`, backed by
Upstash Redis) is for registered OAuth clients and the few-seconds-lived login handshake.
"""
