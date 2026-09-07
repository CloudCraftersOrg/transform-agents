from agents.model import FakeModel
from agents.orchestrator import build_orchestrator
from dispatcher.steps import Dispatcher
from state.models import WaveState, WaveStatus
from state.store import InMemoryStateStore
from tools.escalation import Escalation, escalate
from tools.hitl import InMemoryHitlQueue
from tools.spec import load_contract


def test_escalate_writes_log_and_notifies():
    store = InMemoryStateStore()
    seen = []
    entry = escalate(
        store,
        "t0",
        Escalation("w1", "LZA config does not converge", "region declared incorrectly", 5),
        notify=seen.append,
    )
    log = store.decisions("w1")
    assert len(log) == 1
    assert log[0].kind == "escalation"
    assert log[0].detail["attempts"] == 5
    assert seen == [entry]


def test_escalate_without_notifier():
    store = InMemoryStateStore()
    escalate(store, "t0", Escalation("w1", "x", "y", 1))
    assert len(store.decisions("w1")) == 1


# --- escalating has to take the wave out of the scheduler's hands ---------------------------
# A wave that escalates but stays REPLICATING is re-driven every five minutes: it rediscovers the
# same stall, re-publishes a record and escalates again. One wave reached 102 rounds that way.

def _orq():
    store = InMemoryStateStore()
    orq = build_orchestrator(
        FakeModel(["ok"]), load_contract(), store=store, dispatcher=Dispatcher(),
        hitl=InMemoryHitlQueue(store, lambda: "t0"), clock=lambda: "t0",
    )
    return orq, store


def test_escalating_marks_the_wave_so_the_scheduler_leaves_it_alone():
    orq, store = _orq()
    store.put_wave(WaveState(wave_id="w1", status=WaveStatus.REPLICATING, steps=["cutover"]))
    assert store.waves_in_flight() == ["w1"]

    orq.escalate("w1", context="transform is not advancing", hypothesis="the estate never registers", attempts=9)

    assert store.get_wave("w1").status is WaveStatus.ESCALATED
    assert store.waves_in_flight() == []


def test_escalating_twice_is_not_an_error():
    """The stall is real and may be reported again; the second report must not crash the wave."""
    orq, store = _orq()
    store.put_wave(WaveState(wave_id="w1", status=WaveStatus.REPLICATING, steps=[]))
    orq.escalate("w1", context="c", hypothesis="h", attempts=1)
    orq.escalate("w1", context="c", hypothesis="h", attempts=2)
    assert store.get_wave("w1").status is WaveStatus.ESCALATED
    assert len(store.decisions("w1")) == 2


def test_the_diagnosis_survives_when_there_is_no_wave_to_mark():
    """Escalations are raised against '-' too. Losing the diagnosis would be the worse failure."""
    orq, store = _orq()
    orq.escalate("-", context="policy denied the action", hypothesis="scope limit", attempts=0)
    assert [e.kind for e in store.decisions("-")] == ["decision", "escalation"]


def test_a_finished_wave_is_not_dragged_back_out_of_its_terminal_state():
    orq, store = _orq()
    store.put_wave(WaveState(wave_id="w1", status=WaveStatus.DONE, steps=[]))
    orq.escalate("w1", context="late report", hypothesis="h", attempts=1)
    assert store.get_wave("w1").status is WaveStatus.DONE
    assert any(e.kind == "escalation" for e in store.decisions("w1"))
