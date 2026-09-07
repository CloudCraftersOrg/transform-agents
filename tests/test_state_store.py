import pytest

from state.models import DecisionLogEntry, WaveState, WaveStatus
from state.store import InMemoryStateStore
from state.transitions import IllegalTransition


def test_roundtrip_returns_a_copy():
    s = InMemoryStateStore()
    s.put_wave(WaveState(wave_id="w1", steps=["precheck"], status=WaveStatus.PENDING))
    got = s.get_wave("w1")
    got.status = WaveStatus.DONE
    assert s.get_wave("w1").status == WaveStatus.PENDING


def test_illegal_transition_on_put_raises():
    s = InMemoryStateStore()
    s.put_wave(WaveState(wave_id="w1", status=WaveStatus.PENDING))
    with pytest.raises(IllegalTransition):
        s.put_wave(WaveState(wave_id="w1", status=WaveStatus.CUTTING_OVER))


def test_legal_transition_on_put_ok():
    s = InMemoryStateStore()
    s.put_wave(WaveState(wave_id="w1", status=WaveStatus.PENDING))
    s.put_wave(WaveState(wave_id="w1", status=WaveStatus.PRECHECK))
    assert s.get_wave("w1").status == WaveStatus.PRECHECK


def test_decision_log_filtered_by_wave():
    s = InMemoryStateStore()
    s.append_decision(DecisionLogEntry(wave_id="w1", ts="t0", actor="orq", kind="decision", summary="x"))
    s.append_decision(DecisionLogEntry(wave_id="w2", ts="t0", actor="orq", kind="decision", summary="y"))
    assert len(s.decisions("w1")) == 1


def test_waves_in_flight_skips_settled_waves_and_returns_newest_first():
    from state.models import WaveState
    from state.store import InMemoryStateStore

    store = InMemoryStateStore()
    store.put_wave(WaveState(wave_id="old", status="REPLICATING", updated_at="2026-09-01T00:00:00"))
    store.put_wave(WaveState(wave_id="new", status="TESTING", updated_at="2026-09-03T00:00:00"))
    store.put_wave(WaveState(wave_id="mid", status="PRECHECK", updated_at="2026-09-02T00:00:00"))
    for wid, st in [("d", "DONE"), ("f", "FAILED"), ("e", "ESCALATED"), ("r", "ROLLED_BACK")]:
        store.put_wave(WaveState(wave_id=wid, status=st))

    # least recently serviced first: resuming stamps updated_at, so ordering by newest would
    # hand every tick back to the same waves and starve the rest
    assert store.waves_in_flight() == ["old", "mid", "new"]


def test_a_wave_that_has_never_run_is_serviced_before_any_that_has():
    from state.models import WaveState
    from state.store import InMemoryStateStore

    store = InMemoryStateStore()
    store.put_wave(WaveState(wave_id="ran", status="REPLICATING", updated_at="2026-09-01T00:00:00"))
    store.put_wave(WaveState(wave_id="fresh", status="PENDING"))
    assert store.waves_in_flight()[0] == "fresh"


def test_the_orchestrator_stamps_updated_at_on_every_wave_write():
    from agents.model import FakeModel
    from agents.orchestrator import build_orchestrator
    from dispatcher.steps import Dispatcher
    from state.models import WaveState
    from state.store import InMemoryStateStore
    from tools.hitl import InMemoryHitlQueue
    from tools.spec import load_contract

    store = InMemoryStateStore()
    orq = build_orchestrator(
        FakeModel(["escalate"]), load_contract(), store=store, dispatcher=Dispatcher(),
        hitl=InMemoryHitlQueue(store, lambda: "t0"), clock=lambda: "2026-09-03T12:00:00",
    )
    store.put_wave(WaveState(wave_id="w1", status="PENDING", steps=["precheck"]))
    orq.advance("w1", "precheck")
    assert store.get_wave("w1").updated_at == "2026-09-03T12:00:00"
    assert store.waves_in_flight() == ["w1"]
