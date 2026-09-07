from __future__ import annotations

import os

# One image, two roles. AgentCore takes the container's own command, so a runtime that serves MCP
# and a runtime that serves the agent loop cannot differ by CMD - they differ by SERVE_PROTOCOL.
# Keeping them in one image is deliberate: the MCP server and the agent both need the Transform MCP
# server, boto3 and the whole dispatcher, and two images would drift.

PROTOCOL = os.environ.get("SERVE_PROTOCOL", "HTTP").upper()


def main() -> None:
    if PROTOCOL == "MCP":
        from tools.mcp_server import main as serve
    else:
        from agents.agentic import main as serve
    serve()


if __name__ == "__main__":
    main()
