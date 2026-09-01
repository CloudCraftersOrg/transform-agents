from __future__ import annotations

import os

# Client for the AWS Transform MCP server. Two paths:
#  - stdio (plan B): run the server as a subprocess inside the container with the execution role.
#  - AgentCore Gateway (preferred): the Gateway fronts the MCP; validate the IAM-only headless flow.
# The exact Transform tool schemas are only knowable against credentials - adjust wrappers then.


def stdio_transform_client():
    """`uvx awslabs.aws-transform-mcp-server` over stdio. Wrap in `with client:` to use."""
    from mcp import StdioServerParameters, stdio_client
    from strands.tools.mcp import MCPClient

    return MCPClient(
        lambda: stdio_client(
            StdioServerParameters(
                command="uvx", args=["awslabs.aws-transform-mcp-server@latest"]
            )
        )
    )


def gateway_transform_client(gateway_url: str | None = None, access_token: str | None = None):
    """AgentCore Gateway-fronted MCP over streamable HTTP. `gateway_url` / `access_token` default
    to env (AGENTCORE_GATEWAY_URL / AGENTCORE_GATEWAY_TOKEN)."""
    from mcp.client.streamable_http import streamablehttp_client
    from strands.tools.mcp import MCPClient

    url = gateway_url or os.environ["AGENTCORE_GATEWAY_URL"]
    token = access_token or os.environ.get("AGENTCORE_GATEWAY_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return MCPClient(lambda: streamablehttp_client(url, headers=headers))


def make_transform_client():
    """Gateway if AGENTCORE_GATEWAY_URL is set, otherwise stdio."""
    return gateway_transform_client() if os.environ.get("AGENTCORE_GATEWAY_URL") else stdio_transform_client()


def transform_tools(client) -> dict:
    """Name -> tool for every tool the Transform MCP exposes. Call inside `with client:`."""
    return {t.tool_name: t for t in client.list_tools_sync()}
