"""The scheduler's native Lambda target. The universal target it replaces fired without ever
delivering, so this path is the one that actually wakes the agent."""
import io
import json
import sys

import pytest

from dispatcher.resume import RESUME_PROMPT, _session_id, handler


@pytest.fixture(autouse=True)
def _no_harness(monkeypatch):
    """These cover the deterministic fallback. HARNESS_ARN would silently reroute all of them."""
    monkeypatch.delenv("HARNESS_ARN", raising=False)


class _FakeRuntime:
    def __init__(self, body):
        self.calls = []
        self._body = body

    def invoke_agent_runtime(self, **kw):
        self.calls.append(kw)
        return {"statusCode": 200, "response": io.BytesIO(json.dumps(self._body).encode())}


@pytest.fixture
def _boto(monkeypatch):
    def _install(client):
        import types

        fake = types.SimpleNamespace(client=lambda _name: client)
        monkeypatch.setitem(sys.modules, "boto3", fake)
        return client

    return _install


def test_it_sends_the_resume_payload_the_runtime_understands(monkeypatch, _boto):
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1")
    rt = _boto(_FakeRuntime({"action": "resume", "resumed": {"w1": "WAITING"}}))

    out = handler({}, None)

    assert out["ok"] and out["result"]["resumed"] == {"w1": "WAITING"}
    sent = json.loads(rt.calls[0]["payload"])
    # exactly the shape agents/runtime.py routes to the deterministic resume path
    assert sent == {"action": "resume"}
    assert rt.calls[0]["agentRuntimeArn"].endswith("runtime/r-1")


def test_a_missing_runtime_arn_is_reported_not_guessed(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_ARN", raising=False)
    out = handler({}, None)
    assert out["ok"] is False and "AGENT_RUNTIME_ARN" in out["error"]


def test_a_non_json_body_still_returns_something_readable(monkeypatch, _boto):
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1")

    class _Raw(_FakeRuntime):
        def invoke_agent_runtime(self, **kw):
            self.calls.append(kw)
            return {"statusCode": 500, "response": io.BytesIO(b"<html>gateway error</html>")}

    _boto(_Raw(None))
    out = handler({}, None)
    assert out["ok"] and "gateway error" in out["result"]["raw"]


def test_the_outcome_reaches_cloudwatch(monkeypatch, _boto, capsys):
    """A scheduled run nobody watches is only useful if its result is in the log."""
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1")
    _boto(_FakeRuntime({"resumed": {"w1": "ESCALATED"}}))

    handler({}, None)

    out = capsys.readouterr().out
    assert out.startswith("AGENT") and "ESCALATED" in out


# --- the harness path: the same tick, but the agent decides what to do with it ---

HARNESS = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"


def test_the_tick_goes_to_the_harness_when_one_is_configured(monkeypatch):
    monkeypatch.setenv("HARNESS_ARN", HARNESS)
    seen = {}

    def _invoke(prompt, session_id, harness_arn=""):
        seen.update(prompt=prompt, session_id=session_id, arn=harness_arn)
        return {"answer": "resumed wave-0-prod", "tools_called": ["waves_in_flight"],
                "stop_reason": "end_turn", "incomplete": False, "errors": [], "usage": {}}

    monkeypatch.setattr("agents.harness.invoke", _invoke)
    out = handler({}, None)

    assert out["ok"] and out["via"] == "harness"
    assert out["tools_called"] == ["waves_in_flight"]
    assert seen["arn"] == HARNESS and seen["prompt"] == RESUME_PROMPT


def test_the_session_id_is_long_enough_for_agentcore_to_accept_it():
    """Under 33 characters the invoke fails silently - no error, no invocation, no wave."""
    assert len(_session_id()) >= 33


def test_consecutive_ticks_in_a_day_share_one_session_so_the_agent_remembers():
    assert _session_id() == _session_id()


def test_a_failed_turn_does_not_kill_the_schedule(monkeypatch):
    monkeypatch.setenv("HARNESS_ARN", HARNESS)

    def _boom(*a, **k):
        raise RuntimeError("harness is not READY")

    monkeypatch.setattr("agents.harness.invoke", _boom)
    out = handler({}, None)
    assert out["ok"] is False and "not READY" in out["error"]


def test_with_neither_configured_it_says_so_rather_than_guessing(monkeypatch):
    monkeypatch.delenv("HARNESS_ARN", raising=False)
    monkeypatch.delenv("AGENT_RUNTIME_ARN", raising=False)
    out = handler({}, None)
    assert out["ok"] is False and "HARNESS_ARN" in out["error"]


def test_the_tick_prompt_forbids_answering_from_memory():
    """Ticks share a session, so the agent remembers 'nothing in flight' from five minutes ago and
    skips the read - it slept through a wave being un-escalated exactly once."""
    assert "never answer this from memory" in RESUME_PROMPT
    assert "Call `waves_in_flight` now" in RESUME_PROMPT


def test_the_prompt_says_reading_is_not_progress():
    """The agent read `migration_status` in a loop and ended turns having moved nothing."""
    assert "only tool that moves a wave" in RESUME_PROMPT
    assert "run_migration_wave" in RESUME_PROMPT
