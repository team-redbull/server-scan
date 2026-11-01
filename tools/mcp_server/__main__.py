"""Run the MCP server.

Usage:
    uv run python -m tools.mcp_server            # stdio, token from SERVER_SCAN_TOKEN (local use)
    uv run python -m tools.mcp_server --http     # streamable HTTP on :8090/mcp (deployed)

Environment: `INVENTORY_MCP_API_URL` (default http://localhost:8080), `INVENTORY_MCP_PORT` (8090),
`INVENTORY_MCP_ALLOWED_HOSTS` (comma-separated `Host` values the HTTP transport accepts).
"""

from __future__ import annotations

import argparse
import os

from tools.mcp_server.api import ScanApi
from tools.mcp_server.server import build_server


def main() -> None:
    """Parse the transport choice and run the server."""
    parser = argparse.ArgumentParser(description="Server Scan MCP server")
    parser.add_argument(
        "--http", action="store_true", help="serve streamable HTTP instead of stdio"
    )
    args = parser.parse_args()
    hosts = [
        h.strip() for h in os.environ.get("INVENTORY_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]
    server = build_server(
        ScanApi(os.environ.get("INVENTORY_MCP_API_URL", "http://localhost:8080")),
        host="0.0.0.0" if args.http else "127.0.0.1",  # noqa: S104 - the pod listens on all interfaces
        port=int(os.environ.get("INVENTORY_MCP_PORT", "8090")),
        allowed_hosts=hosts or None,
    )
    server.run("streamable-http" if args.http else "stdio")


if __name__ == "__main__":
    main()
