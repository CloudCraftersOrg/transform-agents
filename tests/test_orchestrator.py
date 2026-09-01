import pytest

from agents.model import FakeModel
from agents.orchestrator import Specialists, build_orchestrator, route
from dispatcher.steps import Dispatcher, StepRejected
from state.models import WaveState, WaveStatus
from state.store import InMemoryStateStore
from state.transitions import IllegalTransition
from tools.hitl import InMemoryHitlQueue
from tools.policy import Action
from tools.spec import load_contract

C = load_contract()
APP = "i-0d6b944117ba6302b"


def _clock():
    return "t0"


def _orq(model):
    store = InMemoryStateStore()
    disp = Dispatcher()
    disp.register("cutover", lambda ctx: {"done": True})
    disp.register("start_replication", lambda ctx: {"replicating": True})
    hitl = InMemoryHitlQueue(store, _clock)
    seen: list[str] = []
    specs = Specialists(interpreter=lambda obj: seen.append(obj) or "config-ok")
    orq = build_orchestrator(
        model, C, store=store, dispatcher=disp, hitl=hitl, specialists=specs, clock=_clock
    )
    return orq, store, seen


def test_route_picks_tool_from_catalog():
    assert route(FakeModel(["use delegate_interpreter now"]), "x") == "delegate_interpreter"


def test_decide_logs_the_choice():
    orq, store, _ = _orq(FakeModel(["escalate"]))
    assert orq.decide("5 attempts and no convergence") == "escalate"
    assert store.decisions("-")[0].kind == "decision"


def test_check_denies_and_logs():
    orq, store, _ = _orq(FakeModel(["evaluate_policy"]))
    d = orq.check(Action("recommend_instance", subject=APP, arch="arm64"), wave_id="w1")
    assert not d.allowed
    assert "policy_denial" in [e.kind for e in store.decisions("w1")]


def test_check_hack_modernization_auto_escalates():
    orq, store, _ = _orq(FakeModel(["escalate"]))
    d = orq.check(
        Action("modernize", subject="fbctf-app", modernization_target="hack"), wave_id="w1"
    )
    assert not d.allowed and d.escalate
    kinds = [e.kind for e in store.decisions("w1")]
    assert "policy_denial" in kinds and "escalation" in kinds


def test_dispatch_idempotent():
    orq, _, _ = _orq(FakeModel(["execute_step"]))
    assert orq.dispatch("w1", "s1", "cutover") == orq.dispatch("w1", "s1", "cutover")


def test_dispatch_reevaluates_policy_server_side():
    orq, _, _ = _orq(FakeModel(["execute_step"]))
    with pytest.raises(StepRejected):
        orq.dispatch(
            "w1", "s2", "cutover", guard=Action("recommend_instance", subject=APP, arch="arm64")
        )


def test_advance_follows_transition_table():
    orq, store, _ = _orq(FakeModel(["execute_step"]))
    store.put_wave(WaveState(wave_id="w1", steps=C.steps, status=WaveStatus.PRECHECK))
    orq.advance("w1", "interpret")
    assert store.get_wave("w1").status == WaveStatus.INTERPRETING


def test_advance_rejects_illegal_skip():
    orq, store, _ = _orq(FakeModel(["x"]))
    store.put_wave(WaveState(wave_id="w1", steps=C.steps, status=WaveStatus.PENDING))
    with pytest.raises(IllegalTransition):
        orq.advance("w1", "cutover")


def test_delegate_calls_specialist():
    orq, _, seen = _orq(FakeModel(["delegate_interpreter"]))
    assert orq.delegate("interpreter", "generate landing zone") == "config-ok"
    assert seen == ["generate landing zone"]


def test_delegate_unwired_specialist_raises():
    orq, _, _ = _orq(FakeModel(["delegate_remediation"]))
    with pytest.raises(NotImplementedError):
        orq.delegate("remediation", "x")


def test_request_approval_creates_pending_hitl():
    orq, _, _ = _orq(FakeModel(["submit_hitl_task"]))
    orq.request_approval("w1", {"case_id": "fbctf-001"})
    assert len(orq.hitl.pending()) == 1


def test_wave_digest_reads_store():
    orq, store, _ = _orq(FakeModel(["wave_status"]))
    store.put_wave(
        WaveState(
            wave_id="w1",
            steps=["a", "b"],
            completed_steps=["a"],
            status=WaveStatus.REPLICATING,
        )
    )
    dg = orq.wave_digest("w1")
    assert dg["status"] == "REPLICATING" and dg["remaining"] == ["b"]


def test_orchestrator_holds_no_aws_client():
    orq, _, _ = _orq(FakeModel(["x"]))
    assert not hasattr(orq, "boto3") and not hasattr(orq, "aws")


def test_default_specialists_share_one_runbook_kb():
    import json

    from tools.runbooks import Runbook

    disp = Dispatcher()
    orq = build_orchestrator(
        FakeModel(['{"action":"resync_volume","rationale":"x"}']), C,
        store=InMemoryStateStore(), dispatcher=disp,
        hitl=InMemoryHitlQueue(InMemoryStateStore(), _clock), clock=_clock,
    )
    orq.runbook_kb.write(Runbook(failure="checksum mismatch on vol-1", action="resync_volume"))

    r = orq.delegate("remediation", json.dumps({"failure": "checksum mismatch on vol-9"}))
    assert any("known runbook" in n for n in r.notes)
