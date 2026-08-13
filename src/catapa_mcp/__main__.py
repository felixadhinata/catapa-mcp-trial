"""Entry point: `python -m catapa_mcp` or the `catapa-mcp` console script."""

from __future__ import annotations

import logging
import sys

from catapa_mcp.server import build_server


def main() -> None:
    """Build and run the CATAPA MCP server over stdio."""
    # stdio transport reserves stdout for the JSON-RPC stream, so logs must go to stderr.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
