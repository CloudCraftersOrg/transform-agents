from state.store import InMemoryStateStore
from tools.escalation import Escalation, escalate


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
