from __future__ import annotations

import json
import os
from urllib.parse import quote

from tools.trace import note

# The Gateway that stands between the Harness and the MCP tool surface.
#
# It exists for one reason: a Harness `remoteMcp` tool takes a URL and static headers and cannot
# SigV4-sign, while an AgentCore Runtime serving MCP accepts only SigV4 or a JWT. A bearer token
# under an unattended agent expires. With a Gateway both hops are IAM - the Harness signs with its
# execution role, the Gateway signs onward with its own - and nothing has to be rotated.

REGION = os.environ.get("AWS_REGION", "us-east-1")
NAME = os.environ.get("GATEWAY_NAME", "transform-agents-tools")
TARGET_NAME = "tools"  # "mcp" is reserved by the service


def runtime_mcp_url(runtime_arn: str, region: str = REGION, qualifier: str = "DEFAULT") -> str:
    """The MCP endpoint of an AgentCore Runtime. The ARN is URL-encoded into the path, colons and
    slashes included; the qualifier is required or the call resolves to no endpoint."""
    return (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
            f"{quote(runtime_arn, safe='')}/invocations?qualifier={qualifier}")


def _control(client=None):
    import boto3

    return client or boto3.client("bedrock-agentcore-control", region_name=REGION)


def find(name: str = NAME, client=None) -> dict | None:
    control = _control(client)
    token = None
    while True:
        page = control.list_gateways(**({"nextToken": token} if token else {}))
        for g in page.get("items", []):
            if g.get("name") == name:
                return g
        token = page.get("nextToken")
        if not token:
            return None


def deploy(role_arn: str, runtime_arn: str, name: str = NAME, client=None) -> dict:
    """Create (or reuse) the gateway and point a target at the MCP runtime. Inbound is AWS_IAM so
    only the harness role can call it; outbound is GATEWAY_IAM_ROLE, which is what makes the second
    hop signed rather than authenticated by a shared secret."""
    control = _control(client)
    gateway = find(name, control)
    if gateway is None:
        gateway = control.create_gateway(
            name=name,
            roleArn=role_arn,
            protocolType="MCP",
            authorizerType="AWS_IAM",
            description="Tool surface of the transform-agents Orchestrator",
        )
        note(f"gateway created: {gateway.get('gatewayArn')}")
    else:
        note(f"gateway reused: {gateway.get('gatewayId')}")

    identifier = gateway.get("gatewayId") or gateway.get("gatewayIdentifier")
    endpoint = runtime_mcp_url(runtime_arn)
    if _target(control, identifier) is None:
        control.create_gateway_target(
            gatewayIdentifier=identifier,
            name=TARGET_NAME,
            targetConfiguration={"mcp": {"mcpServer": {"endpoint": endpoint,
                                                       "listingMode": "DYNAMIC"}}},
            credentialProviderConfigurations=[{
                "credentialProviderType": "GATEWAY_IAM_ROLE",
                "credentialProvider": {"iamCredentialProvider": {"service": "bedrock-agentcore",
                                                                 "region": REGION}},
            }],
        )
        note(f"gateway target -> {endpoint}")
    return gateway


def _target(control, identifier: str) -> dict | None:
    for t in control.list_gateway_targets(gatewayIdentifier=identifier).get("items", []):
        if t.get("name") == TARGET_NAME:
            return t
    return None


def main() -> None:
    """`python -m agents.gateway <gateway-role-arn> <mcp-runtime-arn>`."""
    import sys

    if len(sys.argv) > 2:
        print(json.dumps(deploy(sys.argv[1], sys.argv[2]), default=str, indent=2))
        return
    print(json.dumps(find() or {"gateway": None, "name": NAME}, default=str, indent=2))


if __name__ == "__main__":
    main()
