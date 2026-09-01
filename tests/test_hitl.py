from state.store import InMemoryStateStore
from tools.hitl import APPROVE_DERIVED_CONTRACT, InMemoryHitlQueue


def _queue():
    return InMemoryHitlQueue(InMemoryStateStore(), lambda: "t0")


def test_submit_then_pending():
    q = _queue()
    q.submit("w1", APPROVE_DERIVED_CONTRACT, {"case_id": "fbctf-001"})
    assert len(q.pending()) == 1
    assert len(q.store.decisions("w1")) == 1


def test_resolve_clears_pending_and_logs():
    q = _queue()
    task = q.submit("w1", APPROVE_DERIVED_CONTRACT, {"case_id": "fbctf-001"})
    q.resolve(task, approved=True)
    assert q.pending() == []
    assert task.status == "APPROVED"
    assert len(q.store.decisions("w1")) == 2


def test_reject_sets_status():
    q = _queue()
    task = q.submit("w1", APPROVE_DERIVED_CONTRACT, {})
    q.resolve(task, approved=False)
    assert task.status == "REJECTED"
