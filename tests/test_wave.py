from pathlib import Path

from agents.model import FakeModel
from agents.orchestrator import WaveInputs, WaveOutcome, build_orchestrator
from dispatcher.steps import Dispatcher
from state.models import WaveStatus
from state.store import InMemoryStateStore
from tools.hitl import APPROVE_DERIVED_CONTRACT, InMemoryHitlQueue
from tools.spec import load_contract

VALID_LZA = (
    Path(__file__).resolve().parents[1] / "fixtures" / "lza" / "valid_config.yaml"
).read_text(encoding="utf-8")

TAGGED_CHEAP = [{"instance_type": "t3.small", "ebs_gib": 10, "tags": {"app": "billing"}}]


def _model(verdict: str = "green"):
    def respond(prompt: str) -> str:
        if '"verdict"' in prompt:
            return f'{{"verdict":"{verdict}","reasons":["schema changed"]}}'
        if '"attribution"' in prompt:
            return '{"attribution":"EBS oversized"}'
        return VALID_LZA

    return FakeModel(respond)


def _orq(model):
    store = InMemoryStateStore()
    disp = Dispatcher()
    disp.register("start_replication", lambda ctx: {"replicating": True})
    disp.register("cutover", lambda ctx: {"done": True})
    disp.register("rollback", lambda ctx: {"rolled_back": True})
    hitl = InMemoryHitlQueue(store, lambda: "t0")
    orq = build_orchestrator(
        model, load_contract(), store=store, dispatcher=disp, hitl=hitl, clock=lambda: "t0"
    )
    return orq, store


def test_happy_path_reaches_done():
    orq, store = _orq(_model())
    out = orq.run_wave("w1", WaveInputs(finops_resources=TAGGED_CHEAP))
    assert out is WaveOutcome.DONE
    wave = store.get_wave("w1")
    assert wave.status is WaveStatus.DONE
    assert wave.completed_steps == [
        "precheck", "interpret", "replicate", "test", "cutover", "parity", "finops"
    ]
    assert any(e.kind == "finops_variance" for e in store.decisions("w1"))


def test_waits_on_replication_then_resumes():
    orq, store = _orq(_model())
    ready = {"v": False}
    out = orq.run_wave("w1", WaveInputs(), replication_ready=lambda _w: ready["v"])
    assert out is WaveOutcome.WAITING
    assert store.get_wave("w1").status is WaveStatus.REPLICATING
    assert store.get_wave("w1").completed_steps == ["precheck", "interpret"]

    ready["v"] = True
    out2 = orq.run_wave(
        "w1", WaveInputs(finops_resources=TAGGED_CHEAP), replication_ready=lambda _w: True
    )
    assert out2 is WaveOutcome.DONE


def test_red_verdict_rolls_back():
    orq, store = _orq(_model(verdict="red"))
    out = orq.run_wave("w1", WaveInputs(test_diffs={"changed": {"total": [1, "x"]}}))
    assert out is WaveOutcome.ROLLED_BACK
    assert store.get_wave("w1").status is WaveStatus.ROLLED_BACK
    assert "test" not in store.get_wave("w1").completed_steps
    assert "rollback" in [e.kind for e in store.decisions("w1")]


def test_interpreter_failure_escalates():
    def respond(prompt: str) -> str:
        return "global_config: {}" if "OBJECTIVE" in prompt else "{}"

    orq, store = _orq(FakeModel(respond))
    out = orq.run_wave("w1")
    assert out is WaveOutcome.ESCALATED
    assert store.get_wave("w1").status is WaveStatus.ESCALATED
    assert any(e.kind == "escalation" for e in store.decisions("w1"))


def test_contract_approval_gate_blocks_until_approved():
    orq, _ = _orq(_model())
    assert orq.run_wave("w1", require_contract_approval=True) is WaveOutcome.WAITING
    assert len(orq.hitl.pending()) == 1
    assert orq.run_wave("w1", require_contract_approval=True) is WaveOutcome.WAITING

    orq.hitl.resolve(orq.hitl.latest("w1", APPROVE_DERIVED_CONTRACT), approved=True)
    out = orq.run_wave(
        "w1", WaveInputs(finops_resources=TAGGED_CHEAP), require_contract_approval=True
    )
    assert out is WaveOutcome.DONE


def test_rejected_contract_escalates():
    orq, _ = _orq(_model())
    orq.run_wave("w1", require_contract_approval=True)
    orq.hitl.resolve(orq.hitl.latest("w1", APPROVE_DERIVED_CONTRACT), approved=False)
    assert orq.run_wave("w1", require_contract_approval=True) is WaveOutcome.ESCALATED


def test_server_side_step_rejection_escalates_not_crashes(monkeypatch):
    from dispatcher.steps import StepRejected

    orq, store = _orq(_model())

    def boom(*_a, **_k):
        raise StepRejected("policy said no")

    monkeypatch.setattr(orq, "_run_step", boom)
    out = orq.run_wave("w1")
    assert out is WaveOutcome.ESCALATED
    assert store.get_wave("w1").status is WaveStatus.ESCALATED
    assert any(e.kind == "escalation" for e in store.decisions("w1"))
