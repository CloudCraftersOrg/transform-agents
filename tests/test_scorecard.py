from pathlib import Path

import pytest

from evals.scorecard import (
    build_scorecard,
    constraint_adherence,
    cost_per_successful_run,
    escalations_per_run,
    false_verdicts,
    interpreter_convergence,
    remediation_outcomes,
    unintervened_success_rate,
)
from state.models import DecisionLogEntry, WaveState, WaveStatus
from state.store import InMemoryStateStore
from tools.spec import load_contract


def _store_with_waves(specs):
    store = InMemoryStateStore()
    for wid, status, entries in specs:
        store.put_wave(WaveState(wave_id=wid, status=status))
        for kind, detail in entries:
            store.append_decision(
                DecisionLogEntry(wave_id=wid, ts="t0", actor="test", kind=kind, summary=kind, detail=detail)
            )
    return store


def test_unintervened_success_rate():
    store = _store_with_waves([
        ("w1", WaveStatus.DONE, []),
        ("w2", WaveStatus.DONE, [("escalation", {})]),
        ("w3", WaveStatus.ESCALATED, []),
    ])
    assert unintervened_success_rate(store, ["w1", "w2", "w3"]) == pytest.approx(1 / 3)


def test_escalations_per_run():
    store = _store_with_waves([
        ("w1", WaveStatus.DONE, [("escalation", {}), ("escalation", {})]),
        ("w2", WaveStatus.DONE, []),
    ])
    assert escalations_per_run(store, ["w1", "w2"]) == 1.0


def test_interpreter_convergence_stats():
    store = _store_with_waves([
        ("w1", WaveStatus.DONE, [("interpreter_convergence", {"ok": True, "iterations": 2})]),
        ("w2", WaveStatus.ESCALATED, [("interpreter_convergence", {"ok": False, "iterations": 5})]),
    ])
    stats = interpreter_convergence(store, ["w1", "w2"])
    assert stats.samples == 2 and stats.converged == 1 and stats.avg_iterations == 2.0
    assert stats.rate == 0.5


def test_remediation_outcomes_stats():
    store = _store_with_waves([
        ("w1", WaveStatus.DONE, [("remediation_outcome", {"resolved": True, "from_runbook": False})]),
        ("w2", WaveStatus.DONE, [("remediation_outcome", {"resolved": True, "from_runbook": True})]),
        ("w3", WaveStatus.ESCALATED, [("remediation_outcome", {"resolved": False, "from_runbook": False})]),
    ])
    stats = remediation_outcomes(store, ["w1", "w2", "w3"])
    assert stats.total == 3 and stats.resolved == 2 and stats.escalated == 1
    assert stats.resolution_rate == pytest.approx(2 / 3)
    assert stats.learning_reuse_rate == 0.5


def test_false_verdicts_only_counts_labeled():
    store = _store_with_waves([
        ("w1", WaveStatus.DONE, []),
        ("w2", WaveStatus.ROLLED_BACK, []),
        ("w3", WaveStatus.DONE, []),
    ])
    fv = false_verdicts(store, ["w1", "w2", "w3"], {"w1": False, "w2": True})
    assert fv.false_green == 1 and fv.false_red == 1 and fv.labeled == 2


def test_constraint_adherence_on_fbctf_contract():
    result = constraint_adherence(load_contract())
    assert result.checked == 5 and result.avoided == 5 and result.misses == []


def test_constraint_adherence_with_no_constraints_checks_nothing():
    c = load_contract().model_copy(update={"constraints": []})
    result = constraint_adherence(c)
    assert result.checked == 0 and result.avoided == 0


def test_cost_per_successful_run():
    store = _store_with_waves([
        ("w1", WaveStatus.DONE, []),
        ("w2", WaveStatus.DONE, []),
        ("w3", WaveStatus.ESCALATED, []),
    ])
    cost = cost_per_successful_run({"w1": 10.0, "w2": 20.0, "w3": 999.0}, store, ["w1", "w2", "w3"])
    assert cost == 15.0


def test_cost_per_successful_run_none_without_data():
    store = _store_with_waves([("w1", WaveStatus.DONE, [])])
    assert cost_per_successful_run({}, store, ["w1"]) is None


def test_build_scorecard_notes_missing_ground_truth():
    store = _store_with_waves([("w1", WaveStatus.DONE, [])])
    sc = build_scorecard(store, ["w1"])
    assert sc.total_runs == 1
    assert sc.constraint_adherence is None
    assert sc.false_verdicts is None
    assert sc.cost_per_successful_run is None
    assert any("no contract supplied" in n for n in sc.notes)
    assert any("no known_healthy" in n for n in sc.notes)
    assert any("no cost data" in n for n in sc.notes)


def test_build_scorecard_with_all_inputs():
    store = _store_with_waves([("w1", WaveStatus.DONE, [])])
    sc = build_scorecard(
        store, ["w1"], contract=load_contract(), known_healthy={"w1": True}, cost_by_wave={"w1": 5.0}
    )
    assert sc.constraint_adherence is not None and sc.constraint_adherence.rate == 1.0
    assert sc.false_verdicts is not None and sc.false_verdicts.labeled == 1
    assert sc.cost_per_successful_run == 5.0


def test_scorecard_reads_a_real_wave_run():
    from agents.model import FakeModel
    from agents.orchestrator import WaveInputs, build_orchestrator
    from dispatcher.steps import Dispatcher
    from tools.hitl import InMemoryHitlQueue

    valid_lza = (
        Path(__file__).resolve().parents[1] / "fixtures" / "lza" / "valid_config.yaml"
    ).read_text(encoding="utf-8")

    def respond(prompt: str) -> str:
        return '{"verdict":"green","reasons":[]}' if '"verdict"' in prompt else valid_lza

    store = InMemoryStateStore()
    disp = Dispatcher()
    disp.register("start_replication", lambda ctx: {"replicating": True})
    disp.register("cutover", lambda ctx: {"done": True})
    disp.register("probe_apps", lambda ctx: {"probed": 1, "healthy": 1, "results": [
        {"name": "billing", "ok": True, "status": 200, "fingerprint": "abc"}]})
    hitl = InMemoryHitlQueue(store, lambda: "t0")
    orq = build_orchestrator(
        FakeModel(respond), load_contract(), store=store, dispatcher=disp, hitl=hitl, clock=lambda: "t0"
    )

    orq.run_wave(
        "w1", WaveInputs(
            finops_resources=[{"instance_type": "t3.small", "ebs_gib": 5, "tags": {"a": "b"}}],
            app_probes=[{"name": "billing", "url": "http://billing/health"}],
        )
    )

    stats = interpreter_convergence(store, ["w1"])
    assert stats.samples == 1 and stats.converged == 1 and stats.avg_iterations == 1.0
    assert unintervened_success_rate(store, ["w1"]) == 1.0


def test_gate_autonomy_reads_what_the_agent_actually_decided():
    from evals.scorecard import gate_autonomy
    from state.models import DecisionLogEntry
    from state.store import InMemoryStateStore

    store = InMemoryStateStore()

    def add(kind, summary, detail=None):
        store.append_decision(DecisionLogEntry(wave_id="w1", ts="t", actor="orchestrator",
                                               kind=kind, summary=summary, detail=detail or {}))

    add("decision", "job: autonomously chose 'Continue' - plain workflow advance")
    add("decision", "job: autonomously chose 'Accept as shown' - plain workflow advance")
    add("decision", "job: autonomously chose 'Dynamic IP' - model chose the safe default for a "
                    "configuration gate")
    add("hitl", "job: a human decision is needed - an option carries an irreversible action",
        {"reason": "an option carries an irreversible action"})
    add("hitl", "job: the workflow has stalled - 'Re-check status' for 4 invocations")
    add("remediation_outcome", "job: cleared the initialize_mgn prerequisite the wave was blocked on")

    g = gate_autonomy(store, ["w1"])
    assert g.advanced_plain == 2 and g.advanced_by_model == 1
    assert g.decided == 3 and g.escalated == 1
    assert g.rate == 0.75
    # a stall is the workflow blocked outside the agent, not a decision it declined
    assert g.stalled == 1 and g.prerequisites_cleared == 1
    assert g.escalation_reasons[0][0].startswith("an option carries an irreversible")


def test_gate_autonomy_is_zero_safe_with_no_gates():
    from evals.scorecard import gate_autonomy
    from state.store import InMemoryStateStore

    g = gate_autonomy(InMemoryStateStore(), ["w-empty"])
    assert g.decided == 0 and g.rate == 0.0
