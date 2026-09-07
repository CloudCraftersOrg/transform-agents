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


# --- asking the same question twice ------------------------------------------------------------
# The scheduler re-runs a wave every five minutes and the wave re-asks each time. One unanswered
# question became sixty-odd identical rows in a night and buried the panel it was meant to surface.

PROBE_PAYLOAD = {"needed": "url + expected response per application"}


def _ticking():
    """A clock that moves. The DynamoDB row key is `<wave>#<kind>#<ts>`, so a frozen clock would
    make two genuinely different questions collide on one row - which real timestamps never do."""
    n = iter(range(1, 999))
    return lambda: f"t{next(n):03d}"


def _queues(store, clock=None):
    from tools.hitl import DynamoDbHitlQueue, InMemoryHitlQueue

    clock = clock or _ticking()

    class _Fake:
        def __init__(self):
            self.items = []

        def put_item(self, TableName, Item):  # boto3 casing
            self.items = [i for i in self.items
                          if i["task_key"]["S"] != Item["task_key"]["S"]] + [Item]

        def scan(self, **kw):
            return {"Items": self.items}

    return [InMemoryHitlQueue(store, clock),
            DynamoDbHitlQueue("t", store, _ticking(), client=_Fake())]


def test_the_same_open_question_is_not_asked_twice():
    for q in _queues(InMemoryStateStore()):
        first = q.submit("w1", "app_probe_spec", PROBE_PAYLOAD)
        again = q.submit("w1", "app_probe_spec", PROBE_PAYLOAD)
        assert len(q.pending()) == 1, type(q).__name__
        assert again.wave_id == first.wave_id and again.kind == first.kind


def test_a_different_question_of_the_same_kind_still_opens_one():
    for q in _queues(InMemoryStateStore()):
        q.submit("w1", "ask_engineer", {"question": "which URL?"})
        q.submit("w1", "ask_engineer", {"question": "which region?"})
        assert len(q.pending()) == 2, type(q).__name__


def test_another_wave_asking_the_same_thing_is_its_own_question():
    for q in _queues(InMemoryStateStore()):
        q.submit("w1", "app_probe_spec", PROBE_PAYLOAD)
        q.submit("w2", "app_probe_spec", PROBE_PAYLOAD)
        assert len(q.pending()) == 2, type(q).__name__


def test_once_answered_the_question_can_be_asked_again():
    """Answered is not the same as open. A wave that needs a fresh spec must be able to ask."""
    for q in _queues(InMemoryStateStore()):
        q.resolve(q.submit("w1", "app_probe_spec", PROBE_PAYLOAD), True)
        q.submit("w1", "app_probe_spec", PROBE_PAYLOAD)
        assert len(q.pending()) == 1, type(q).__name__


def test_the_repeat_is_not_written_to_the_decision_log_either():
    store = InMemoryStateStore()
    from tools.hitl import InMemoryHitlQueue

    q = InMemoryHitlQueue(store, lambda: "t0")
    q.submit("w1", "app_probe_spec", PROBE_PAYLOAD)
    q.submit("w1", "app_probe_spec", PROBE_PAYLOAD)
    assert len([e for e in store.decisions("w1") if e.kind == "hitl"]) == 1
