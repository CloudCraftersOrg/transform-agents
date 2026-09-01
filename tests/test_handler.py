import io
import json

from dispatcher.handler import build_lambda_dispatcher, handler
from dispatcher.steps import InMemoryLedger, LambdaInvokingDispatcher, StepContext, StepRejected
from tools.spec import load_contract

C = load_contract()
APP = "i-0d6b944117ba6302b"


class _FakeMgn:
    def __init__(self):
        self.calls = []

    def start_replication(self, **kw):
        self.calls.append(("start_replication", kw))

    def start_cutover(self, **kw):
        self.calls.append(("start_cutover", kw))


class _FakeRoute53:
    def __init__(self):
        self.calls = []

    def change_resource_record_sets(self, **kw):
        self.calls.append(kw)
        return {"ChangeInfo": {"Id": "/change/C1"}}


def _dispatcher():
    return build_lambda_dispatcher(
        ledger=InMemoryLedger(), mgn=_FakeMgn(), route53=_FakeRoute53(), codepipeline=None
    )


def _ctx(step_id, params):
    return StepContext(wave_id="w1", step_id=step_id, contract=C, params=params)


def test_start_replication_calls_mgn_per_server():
    d = _dispatcher()
    out = d.dispatch("start_replication", _ctx("replicate", {"source_server_ids": ["s1", "s2"]}))
    assert out == {"replicating": ["s1", "s2"]}


def test_cutover_flips_dns_then_calls_mgn():
    d = _dispatcher()
    out = d.dispatch(
        "cutover",
        _ctx("cutover", {
            "source_server_ids": ["s1"], "hosted_zone_id": "Z1",
            "record_name": "app.example.com", "target_ip": "10.0.0.9", "ttl": 60,
        }),
    )
    assert out["dns"]["pointed_at"] == "10.0.0.9"


def test_rollback_keeps_replication_alive():
    d = _dispatcher()
    out = d.dispatch(
        "rollback",
        _ctx("test-rollback", {
            "hosted_zone_id": "Z1", "record_name": "app.example.com", "source_ip": "10.0.0.1",
        }),
    )
    assert out["dns"]["pointed_at"] == "10.0.0.1" and out["replication"] == "kept alive"


def test_dispatch_is_idempotent_across_calls():
    d = _dispatcher()
    p = {"source_server_ids": ["s1"]}
    first = d.dispatch("start_replication", _ctx("replicate", p))
    second = d.dispatch("start_replication", _ctx("replicate", p))
    assert first == second


def test_handler_reevaluates_policy_and_returns_structured_rejection():
    event = {
        "wave_id": "w1", "step_id": "s1", "step": "cutover",
        "params": {"source_server_ids": ["s1"], "hosted_zone_id": "Z1",
                   "record_name": "a", "target_ip": "10.0.0.9"},
        "contract": C.model_dump(mode="json"),
        "guard": {"type": "recommend_instance", "subject": APP, "arch": "arm64"},
    }
    out = handler(event)
    assert out["ok"] is False and out["rejected"] is True and "ARM64" in out["reason"]


# --- LambdaInvokingDispatcher: the Runtime-side adapter ---


class _FakeLambda:
    def __init__(self, body: dict, function_error: str | None = None):
        self._body = body
        self._function_error = function_error
        self.invoked: list[tuple[str, dict]] = []

    def invoke(self, FunctionName, Payload):  # matches the boto3 kwargs
        self.invoked.append((FunctionName, json.loads(Payload)))
        resp = {"Payload": io.BytesIO(json.dumps(self._body).encode())}
        if self._function_error:
            resp["FunctionError"] = self._function_error
        return resp


def test_lambda_dispatcher_returns_the_step_result():
    lam = _FakeLambda({"ok": True, "result": {"replicating": ["s1"]}})
    d = LambdaInvokingDispatcher("transform-agents-step-dispatcher", client=lam)
    out = d.dispatch("start_replication", _ctx("replicate", {"source_server_ids": ["s1"]}))
    assert out == {"replicating": ["s1"]}
    name, event = lam.invoked[0]
    assert name == "transform-agents-step-dispatcher"
    assert event["step"] == "start_replication" and event["wave_id"] == "w1"
    assert event["contract"]["case_id"] == "fbctf-001"


def test_lambda_dispatcher_reraises_step_rejection():
    lam = _FakeLambda({"ok": False, "rejected": True, "reason": "architecture arm64 not allowed"})
    d = LambdaInvokingDispatcher("arn:aws:lambda:us-east-1:111122223333:function:x", client=lam)
    try:
        d.dispatch("cutover", _ctx("cutover", {}))
        raise AssertionError("expected StepRejected")
    except StepRejected as e:
        assert "arm64" in str(e)


def test_lambda_dispatcher_raises_on_function_error():
    lam = _FakeLambda({"errorMessage": "boom"}, function_error="Unhandled")
    d = LambdaInvokingDispatcher("x", client=lam)
    try:
        d.dispatch("cutover", _ctx("cutover", {}))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "step Lambda error" in str(e)
