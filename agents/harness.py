from __future__ import annotations

import json
import os

from agents.prompt import SYSTEM_PROMPT
from tools.trace import fail, note

# AgentCore Harness: AWS runs the agent loop, we supply the model, the prompt and the tools. The
# tools arrive over MCP from tools/mcp_server.py, so the guards that used to live one function call
# from the loop now live one network hop from it and are enforced the same way.
#
# What this buys over running the loop ourselves in the runtime: a managed loop with its own
# session memory, and a tool surface any MCP client can call - not only our Strands agent.
#
# What it does not buy: a trigger. A harness sitting still does nothing. The EventBridge schedule
# is still what wakes the migration up; it now calls InvokeHarness instead of InvokeAgentRuntime.

REGION = os.environ.get("AWS_REGION", "us-east-1")
NAME = os.environ.get("HARNESS_NAME", "transform_agents_orchestrator")
MCP_SERVER_NAME = "transform-agents"

MODEL_ID = os.environ.get("HARNESS_MODEL_ID") or os.environ.get(
    "ORCHESTRATOR_MODEL_ID", "amazon.nova-lite-v1:0")

# A migration wave is a long turn: a Transform round trip, an MGN read and a dispatched step each
# cost seconds, and the loop has to be allowed to finish one rather than be cut off mid-wave.
MAX_ITERATIONS = int(os.environ.get("HARNESS_MAX_ITERATIONS", "30"))
TIMEOUT_SECONDS = int(os.environ.get("HARNESS_TIMEOUT_SECONDS", "900"))


def _control(client=None):
    import boto3

    return client or boto3.client("bedrock-agentcore-control", region_name=REGION)


def _data(client=None):
    import boto3

    return client or boto3.client("bedrock-agentcore", region_name=REGION)


def mcp_tool() -> dict:
    """How the harness reaches the tool surface. A gateway is the governed path - the harness signs
    with its execution role and the gateway signs onward to the MCP runtime, so no credential is
    ever written into the harness. A bare URL is the fallback for a server that authenticates on a
    header."""
    gateway = os.environ.get("HARNESS_GATEWAY_ARN", "")
    if gateway:
        return {
            "type": "agentcore_gateway",
            "name": MCP_SERVER_NAME,
            "config": {"agentCoreGateway": {"gatewayArn": gateway,
                                            "outboundAuth": {"awsIam": {}}}},
        }
    url = os.environ.get("HARNESS_MCP_URL", "")
    if not url:
        raise RuntimeError("set HARNESS_GATEWAY_ARN or HARNESS_MCP_URL so the harness has tools")
    config: dict = {"url": url}
    headers = os.environ.get("HARNESS_MCP_HEADERS", "")
    if headers:
        config["headers"] = json.loads(headers)
    return {"type": "remote_mcp", "name": MCP_SERVER_NAME, "config": {"remoteMcp": config}}


def spec(execution_role_arn: str) -> dict:
    """The harness definition. `allowedTools` is a safety control, not an optimisation: the harness
    ships `shell` and `file_operations` in every session by default, and a shell on a live migration
    would let the agent reach MGN and Route 53 without passing a single dispatcher guard. Only the
    MCP surface is allowed."""
    return {
        "harnessName": NAME,
        "executionRoleArn": execution_role_arn,
        "model": {"bedrockModelConfig": {"modelId": MODEL_ID}},
        "systemPrompt": [{"text": SYSTEM_PROMPT}],
        "tools": [mcp_tool()],
        "allowedTools": [f"@{MCP_SERVER_NAME}"],
        "maxIterations": MAX_ITERATIONS,
        "timeoutSeconds": TIMEOUT_SECONDS,
    }


def find(name: str = NAME, client=None) -> dict | None:
    """The harness by name. Create is not idempotent on the name, so every deploy looks first."""
    control = _control(client)
    token = None
    while True:
        page = control.list_harnesses(**({"nextToken": token} if token else {}))
        for h in page.get("harnesses", []):
            if h.get("harnessName") == name:
                return h
        token = page.get("nextToken")
        if not token:
            return None


def deploy(execution_role_arn: str, client=None) -> dict:
    """Create the harness, or update the one that already carries this name. Returns the harness."""
    control = _control(client)
    body = spec(execution_role_arn)
    existing = find(body["harnessName"], control)
    if existing:
        harness = control.update_harness(
            harnessId=existing["harnessId"],
            **{k: v for k, v in body.items() if k != "harnessName"},
        )["harness"]
        note(f"harness updated: {harness['arn']} ({harness['status']})")
    else:
        harness = control.create_harness(**body)["harness"]
        note(f"harness created: {harness['arn']} ({harness['status']})")
    return harness


# stopReasons that mean the loop ran out of room rather than finished. Reported rather than
# swallowed: a wave that stopped at max_iterations looks identical to one that finished until you
# read this field, and that is exactly how a stalled migration goes unnoticed.
INCOMPLETE = ("max_iterations_exceeded", "max_tokens", "max_output_tokens_exceeded",
              "timeout_exceeded", "model_context_window_exceeded", "partial_turn", "interrupted")


def invoke(prompt: str, session_id: str, harness_arn: str = "", client=None) -> dict:
    """One turn. Returns the answer, the tools the agent actually called, and why it stopped."""
    arn = harness_arn or os.environ.get("HARNESS_ARN", "")
    if not arn:
        raise RuntimeError("HARNESS_ARN is not set")
    data = _data(client)
    try:
        return _consume(_send(data, arn, session_id, prompt))
    except Exception as e:  # re-raised below unless it is the one failure worth retrying
        if not _session_is_corrupt(e):
            raise
        fresh = f"{session_id}-retry-{_suffix()}"
        fail(f"harness session {session_id} is unusable ({str(e)[:120]}); retrying on {fresh}")
        return _consume(_send(data, arn, fresh, prompt))


def _send(client, arn: str, session_id: str, prompt: str):
    return client.invoke_harness(
        harnessArn=arn,
        runtimeSessionId=session_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )["stream"]


# A session whose stored history no longer forms a valid conversation. It cannot be repaired from
# here and it never recovers on its own, so every later tick on that id would fail the same way.
CORRUPT_SESSION_SYMPTOMS = (
    "must start with a user message",
    "conversation must alternate",
    "last message must be",
)


def _session_is_corrupt(e: Exception) -> bool:
    low = str(e).lower()
    return any(s in low for s in CORRUPT_SESSION_SYMPTOMS)


def _suffix() -> str:
    """Long enough that the retry id still clears AgentCore's 33-character minimum."""
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%H%M%S")


def _consume(stream) -> dict:
    """Flatten the event stream. Tool calls are collected because they are the record of what the
    agent did - the final text is what it says it did."""
    text: list[str] = []
    tools: list[str] = []
    stop = ""
    errors: list[str] = []
    usage: dict = {}
    for event in stream:
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                tools.append(start["toolUse"].get("name", "?"))
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                text.append(delta["text"])
        elif "messageStop" in event:
            stop = event["messageStop"].get("stopReason", "")
        elif "metadata" in event:
            usage = event["metadata"].get("usage", {})
        else:
            for key in ("internalServerException", "validationException", "runtimeClientError"):
                if key in event:
                    errors.append(f"{key}: {event[key].get('message', '')}"[:300])
    if errors:
        fail("harness stream: " + "; ".join(errors))
    answer = "".join(text).strip()
    note(f"harness turn: stop={stop or '?'} tools={tools} "
         f"tokens={usage.get('totalTokens', '?')} answer={answer[:160]!r}")
    return {"answer": answer, "tools_called": tools, "stop_reason": stop,
            "incomplete": stop in INCOMPLETE, "errors": errors, "usage": usage}


def main() -> None:
    """`python -m agents.harness <execution-role-arn>` deploys; with no argument it reports."""
    import sys

    if len(sys.argv) > 1:
        print(json.dumps(deploy(sys.argv[1]), default=str, indent=2))
        return
    found = find()
    print(json.dumps(found or {"harness": None, "name": NAME}, default=str, indent=2))


if __name__ == "__main__":
    main()
