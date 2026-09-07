"""AgentCore Harness: AWS runs the loop. These pin the two things that are ours to get right - what
the harness is allowed to do, and whether a scheduled turn that ran out of room is reported as one."""
import json

import pytest

from agents import harness
from agents.prompt import SYSTEM_PROMPT

ROLE = "arn:aws:iam::111111111111:role/transform-agents-harness"
GATEWAY = "arn:aws:bedrock-agentcore:us-east-1:111111111111:gateway/gw-1"
HARNESS = "arn:aws:bedrock-agentcore:us-east-1:111111111111:harness/h-1"


HARNESS_ENV = ("HARNESS_GATEWAY_ARN", "HARNESS_MCP_URL", "HARNESS_MCP_HEADERS", "HARNESS_ARN")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Every test starts with no harness configured, and anything it sets is undone afterwards -
    these are process-wide and would otherwise decide another test file's outcome."""
    for key in HARNESS_ENV:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch.setenv


def test_the_agent_cannot_reach_the_built_in_shell(env):
    """The harness ships `shell` and `file_operations` in every session. A shell on a live migration
    reaches MGN and Route 53 without passing one dispatcher guard, so only the MCP surface is
    allowed - this is a safety control, not a token optimisation."""
    env("HARNESS_GATEWAY_ARN", GATEWAY)
    spec = harness.spec(ROLE)
    assert spec["allowedTools"] == ["@transform-agents"]
    assert "shell" not in spec["allowedTools"]
    assert "file_operations" not in spec["allowedTools"]


def test_the_harness_runs_the_orchestrator_s_own_instructions(env):
    env("HARNESS_GATEWAY_ARN", GATEWAY)
    assert harness.spec(ROLE)["systemPrompt"] == [{"text": SYSTEM_PROMPT}]


def test_the_gateway_is_preferred_so_no_credential_is_written_into_the_harness(env):
    env("HARNESS_GATEWAY_ARN", GATEWAY)
    env("HARNESS_MCP_URL", "https://example.invalid/mcp")
    tool = harness.mcp_tool()
    assert tool["type"] == "agentcore_gateway"
    assert tool["config"]["agentCoreGateway"] == {
        "gatewayArn": GATEWAY, "outboundAuth": {"awsIam": {}}
    }


def test_a_bare_url_is_the_fallback_and_carries_its_headers(env):
    env("HARNESS_MCP_URL", "https://example.invalid/mcp")
    env("HARNESS_MCP_HEADERS", json.dumps({"Authorization": "Bearer t"}))
    tool = harness.mcp_tool()
    assert tool["type"] == "remote_mcp"
    assert tool["config"]["remoteMcp"]["headers"] == {"Authorization": "Bearer t"}


def test_a_harness_with_no_tools_is_refused_rather_than_deployed():
    with pytest.raises(RuntimeError, match="HARNESS_GATEWAY_ARN or HARNESS_MCP_URL"):
        harness.mcp_tool()


def test_deploy_updates_the_existing_harness_instead_of_making_a_second_one(env):
    env("HARNESS_GATEWAY_ARN", GATEWAY)
    control = _Control(existing=[{"harnessName": harness.NAME, "harnessId": "h-1"}])
    harness.deploy(ROLE, client=control)
    assert control.created == []
    assert control.updated[0]["harnessId"] == "h-1"
    assert "harnessName" not in control.updated[0]


def test_deploy_creates_one_when_the_name_is_free(env):
    env("HARNESS_GATEWAY_ARN", GATEWAY)
    control = _Control(existing=[{"harnessName": "someone_elses", "harnessId": "h-9"}])
    harness.deploy(ROLE, client=control)
    assert control.created[0]["harnessName"] == harness.NAME
    assert control.updated == []


def test_a_turn_reports_the_tools_it_called_not_only_what_it_says_it_did():
    turn = harness._consume(_stream([
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "1", "name": "waves_in_flight"}}}},
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "2", "name": "run_migration_wave"}}}},
        {"contentBlockDelta": {"delta": {"text": "wave-0-prod is "}}},
        {"contentBlockDelta": {"delta": {"text": "replicating"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"totalTokens": 900}}},
    ]))
    assert turn["answer"] == "wave-0-prod is replicating"
    assert turn["tools_called"] == ["waves_in_flight", "run_migration_wave"]
    assert turn["incomplete"] is False


def test_a_turn_that_ran_out_of_room_is_not_reported_as_a_finished_one():
    """max_iterations looks exactly like success until you read stopReason - which is how a stalled
    migration goes unnoticed for a day."""
    turn = harness._consume(_stream([
        {"contentBlockDelta": {"delta": {"text": "still working"}}},
        {"messageStop": {"stopReason": "max_iterations_exceeded"}},
    ]))
    assert turn["incomplete"] is True


def test_stream_errors_are_surfaced():
    turn = harness._consume(_stream([
        {"validationException": {"message": "runtimeSessionId is too short", "reason": "CannotParse"}},
    ]))
    assert turn["errors"] and "too short" in turn["errors"][0]


def test_invoke_without_an_arn_says_so():
    with pytest.raises(RuntimeError, match="HARNESS_ARN"):
        harness.invoke("do the thing", "s" * 40)


def _stream(events):
    return iter(events)


class _Control:
    def __init__(self, existing):
        self._existing = existing
        self.created, self.updated = [], []

    def list_harnesses(self, **kw):
        return {"harnesses": self._existing}

    def create_harness(self, **kw):
        self.created.append(kw)
        return {"harness": {"arn": HARNESS, "status": "READY", **kw}}

    def update_harness(self, **kw):
        self.updated.append(kw)
        return {"harness": {"arn": HARNESS, "status": "READY", **kw}}


# --- the gateway that makes the second hop signed instead of secret-authenticated ---

def test_the_runtime_mcp_url_encodes_the_arn_into_the_path():
    """Colons and slashes must be percent-encoded and the qualifier is not optional - without it
    the call resolves to no endpoint."""
    from agents.gateway import runtime_mcp_url

    url = runtime_mcp_url("arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1", region="us-east-1")
    assert "arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A1%3Aruntime%2Fr-1" in url
    assert url.endswith("/invocations?qualifier=DEFAULT")
    assert ":runtime/r-1" not in url


def test_the_gateway_signs_onward_with_its_own_role():
    from agents import gateway

    gw = _Gateway()
    gateway.deploy("arn:aws:iam::1:role/gw", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1",
                   client=gw)
    assert gw.created[0]["authorizerType"] == "AWS_IAM"
    creds = gw.targets[0]["credentialProviderConfigurations"][0]
    assert creds["credentialProviderType"] == "GATEWAY_IAM_ROLE"
    assert creds["credentialProvider"]["iamCredentialProvider"]["service"] == "bedrock-agentcore"


def test_redeploying_reuses_the_gateway_and_its_target():
    from agents import gateway

    gw = _Gateway(existing=[{"name": gateway.NAME, "gatewayId": "gw-1"}], has_target=True)
    gateway.deploy("arn:aws:iam::1:role/gw", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1",
                   client=gw)
    assert gw.created == [] and gw.targets == []


class _Gateway:
    def __init__(self, existing=None, has_target=False):
        self._existing = existing or []
        self._has_target = has_target
        self.created, self.targets = [], []

    def list_gateways(self, **kw):
        return {"items": self._existing}

    def create_gateway(self, **kw):
        self.created.append(kw)
        return {"gatewayArn": "arn:gw", "gatewayId": "gw-1", **kw}

    def list_gateway_targets(self, **kw):
        return {"items": [{"name": "tools"}] if self._has_target else []}

    def create_gateway_target(self, **kw):
        self.targets.append(kw)
        return kw


# --- a poisoned session must not cost every later tick ----------------------------------------

def test_a_corrupt_session_is_retried_on_a_fresh_id(env):
    """A session whose stored history is no longer a valid conversation never recovers, and the
    scheduler reuses one id all day - so without this every tick for the rest of the day fails."""
    env("HARNESS_ARN", HARNESS)
    calls = []

    class _Data:
        def invoke_harness(self, **kw):
            calls.append(kw["runtimeSessionId"])
            if len(calls) == 1:
                raise RuntimeError("ValidationException: A conversation must start with a user "
                                   "message. Try again with a conversation that starts with one.")
            return {"stream": _stream([{"messageStop": {"stopReason": "end_turn"}}])}

    turn = harness.invoke("go", "transform-agents-resume-2026-09-04", client=_Data())
    assert turn["stop_reason"] == "end_turn"
    assert len(calls) == 2 and calls[1].startswith(calls[0]) and calls[1] != calls[0]
    assert len(calls[1]) >= 33


def test_an_ordinary_failure_is_not_retried(env):
    env("HARNESS_ARN", HARNESS)
    calls = []

    class _Data:
        def invoke_harness(self, **kw):
            calls.append(1)
            raise RuntimeError("ThrottlingException: slow down")

    with pytest.raises(RuntimeError, match="Throttling"):
        harness.invoke("go", "s" * 40, client=_Data())
    assert len(calls) == 1
