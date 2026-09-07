
import agents.runtime as rt
from agents.orchestrator import WaveOutcome
from tools.spec import load_contract


def test_invoke_runs_the_wave(monkeypatch):
    seen = {}

    class _FakeOrq:
        def run_wave(self, wave_id, inputs, *, require_contract_approval=False):
            seen.update(wave_id=wave_id, inputs=inputs, gate=require_contract_approval)
            return WaveOutcome.DONE

    monkeypatch.setattr(rt, "_build", lambda _contract: _FakeOrq())
    out = rt.invoke(
        {
            "contract": load_contract().model_dump(mode="json"),
            "wave_id": "w1",
            "inputs": {"interpret_objective": "x"},
            "require_contract_approval": True,
        }
    )
    assert out == {"wave_id": "w1", "outcome": "DONE"}
    assert seen["wave_id"] == "w1" and seen["gate"] is True
    assert seen["inputs"].interpret_objective == "x"



def test_a_rerun_reuses_the_contract_derived_from_the_same_plan_job():
    from agents.runtime import _cached_contract, _contract_key
    from state.models import DecisionLogEntry
    from state.store import InMemoryStateStore
    from tools.spec import load_contract

    store, contract = InMemoryStateStore(), load_contract()
    key = _contract_key("job-2")
    store.append_decision(DecisionLogEntry(
        wave_id=key, ts="t0", actor="orchestrator", kind="decision",
        summary="contract derived from VmwareMigration",
        detail={"derived_contract": contract.model_dump(mode="json")},
    ))
    # a brand-new wave has no log of its own, so the plan-scoped entry is what saves the re-derive
    assert _cached_contract(store, "wave-never-seen", key).case_id == contract.case_id
    assert _cached_contract(store, "wave-never-seen") is None


def test_ask_answers_only_from_the_record_it_assembles(monkeypatch):
    from state.models import DecisionLogEntry, WaveState
    from state.store import InMemoryStateStore

    store = InMemoryStateStore()
    store.put_wave(WaveState(wave_id="w1", status="REPLICATING", current_step="replicate",
                             completed_steps=["precheck", "interpret"]))
    for kind, summary in [
        ("policy_denial", "cutover withheld: 9 of 12 servers fail the contract"),
        ("remediation_outcome", "cleared the initialize_mgn prerequisite"),
        ("escalation", "blocked on a tagged subnet in a tagged VPC"),
    ]:
        store.append_decision(DecisionLogEntry(wave_id="w1", ts="t", actor="orchestrator",
                                               kind=kind, summary=summary))

    seen = {}

    class _Model:
        def complete(self, prompt, *, system=None):
            seen["prompt"], seen["system"] = prompt, system
            return "  Wave w1 is on replicate, blocked on subnet tagging.  "

    monkeypatch.setenv("WAVE_STATE_TABLE", "t-wave")
    monkeypatch.setenv("DECISION_LOG_TABLE", "t-log")
    monkeypatch.setattr(rt, "DynamoDbStateStore", lambda *a: store)
    monkeypatch.setattr(rt, "_model", lambda: _Model())
    monkeypatch.setattr(rt, "_cached_contract", lambda *a: None)

    class _Ws:
        def pending_interaction(self, _jid):
            return {"text": "no tagged subnet was found", "options": [{"value": "continue rehost"}]}

    out = rt._ask("why is it stuck?", "w1", _Ws(),
                  {"jobId": "j1", "jobName": "VmwareMigration", "statusDetails": {"status": "AWAITING_HUMAN_INPUT"}})

    assert out["answer"] == "Wave w1 is on replicate, blocked on subnet tagging."
    assert out["grounded_on"] == {"wave_id": "w1", "decisions": 3,
                                  "job_status": "AWAITING_HUMAN_INPUT", "has_contract": False,
                                  "job_is_asking": True}
    # the model sees the decision log and the job's question, and is told not to infer
    assert "cutover withheld: 9 of 12 servers fail" in seen["prompt"]
    assert "no tagged subnet was found" in seen["prompt"]
    assert "Answer ONLY from the record" in seen["system"]


def test_resume_skips_settled_waves_and_survives_one_that_raises(monkeypatch):
    from state.models import WaveState
    from state.store import InMemoryStateStore

    store = InMemoryStateStore()
    for wid, st in [("w-a", "REPLICATING"), ("w-b", "TESTING"), ("w-done", "DONE"),
                    ("w-esc", "ESCALATED"), ("w-failed", "FAILED")]:
        store.put_wave(WaveState(wave_id=wid, status=st))

    monkeypatch.setenv("WAVE_STATE_TABLE", "t-wave")
    monkeypatch.setenv("DECISION_LOG_TABLE", "t-log")
    monkeypatch.setattr(rt, "DynamoDbStateStore", lambda *a: store)

    real = rt.invoke
    calls = []

    def _spy(payload):
        if payload.get("action") == "resume":
            return real(payload)
        calls.append(payload["wave_id"])
        if payload["wave_id"] == "w-b":
            raise RuntimeError("transform is busy")
        return {"outcome": "WAITING"}

    monkeypatch.setattr(rt, "invoke", _spy)
    out = real({"action": "resume"})

    assert sorted(calls) == ["w-a", "w-b"]  # settled waves are left alone
    assert out["resumed"]["w-a"] == "WAITING"
    # one wave blowing up must not stop the others
    assert out["resumed"]["w-b"].startswith("ERROR: RuntimeError")


def test_the_deterministic_path_reads_the_engineers_answer():
    """The agentic tool read the spec back and the deterministic wave did not, so a wave resumed by
    the scheduler probed nothing and escalated for want of an answer already given."""
    from agents.runtime import _answered_probe_spec
    from state.models import DecisionLogEntry
    from state.store import InMemoryStateStore

    store = InMemoryStateStore()
    assert _answered_probe_spec(store, "w1") == []
    store.append_decision(DecisionLogEntry(
        wave_id="w1", ts="t0", actor="human", kind="hitl", summary="spec",
        detail={"app_probes": [{"name": "a", "url": "https://a/"}]}))
    assert _answered_probe_spec(store, "w1") == [{"name": "a", "url": "https://a/"}]


def test_both_surfaces_read_the_answer_through_one_definition():
    """Two readers that can disagree about whether the engineer answered is how one of them ends up
    probing nothing."""
    import inspect

    from agents.tools import _probe_spec

    assert "_answered_probe_spec" in inspect.getsource(_probe_spec)
