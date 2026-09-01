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
