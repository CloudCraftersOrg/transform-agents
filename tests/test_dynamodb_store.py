import pytest

from state.models import DecisionLogEntry, WaveState, WaveStatus
from state.store import DynamoDbStateStore
from state.transitions import IllegalTransition


def _store(dynamo):
    return DynamoDbStateStore("wave_state", "decision_log", client=dynamo)


def test_wave_roundtrip(dynamo):
    s = _store(dynamo)
    assert s.get_wave("w1") is None
    s.put_wave(WaveState(wave_id="w1", steps=["precheck"], status=WaveStatus.PENDING))
    got = s.get_wave("w1")
    assert got.status is WaveStatus.PENDING and got.steps == ["precheck"]


def test_legal_and_illegal_transitions(dynamo):
    s = _store(dynamo)
    s.put_wave(WaveState(wave_id="w1", status=WaveStatus.PENDING))
    s.put_wave(WaveState(wave_id="w1", status=WaveStatus.PRECHECK))
    assert s.get_wave("w1").status is WaveStatus.PRECHECK
    with pytest.raises(IllegalTransition):
        s.put_wave(WaveState(wave_id="w1", status=WaveStatus.CUTTING_OVER))


def test_decision_log_is_ordered_and_scoped(dynamo):
    s = _store(dynamo)
    s.append_decision(DecisionLogEntry(wave_id="w1", ts="t0", actor="x", kind="decision", summary="a"))
    s.append_decision(DecisionLogEntry(wave_id="w1", ts="t1", actor="x", kind="decision", summary="b"))
    s.append_decision(DecisionLogEntry(wave_id="w2", ts="t0", actor="x", kind="hitl", summary="c"))
    assert [e.summary for e in s.decisions("w1")] == ["a", "b"]
    assert [e.summary for e in s.decisions("w2")] == ["c"]


def test_same_timestamp_entries_do_not_collide(dynamo):
    s = _store(dynamo)
    for _ in range(3):
        s.append_decision(
            DecisionLogEntry(wave_id="w1", ts="t0", actor="x", kind="decision", summary="same")
        )
    assert len(s.decisions("w1")) == 3
