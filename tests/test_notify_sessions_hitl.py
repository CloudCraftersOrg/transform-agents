"""The three gaps: escalations nobody heard, HITL tasks that died with the invocation, and an agent
that could not remember the previous question."""
import json

from state.models import DecisionLogEntry
from state.store import InMemoryStateStore
from tools.escalation import Escalation, escalate
from tools.hitl import DynamoDbHitlQueue
from tools.notify import json_notifier, sns_notifier
from tools.sessions import DynamoDbSessions


class _FakeDdb:
    def __init__(self):
        self.items = {}

    def put_item(self, TableName, Item):
        key = Item.get("task_key", Item.get("session_id"))["S"]
        self.items[key] = Item

    def get_item(self, TableName, Key):
        k = next(iter(Key.values()))["S"]
        return {"Item": self.items[k]} if k in self.items else {}

    def scan(self, TableName, **kw):
        return {"Items": list(self.items.values())}


class _FakeSns:
    def __init__(self, boom=False):
        self.published = []
        self._boom = boom

    def publish(self, **kw):
        if self._boom:
            raise RuntimeError("topic gone")
        self.published.append(kw)


# --- 1. escalations reach a person ---

def test_an_escalation_is_published_not_just_written():
    store, sink = InMemoryStateStore(), []
    entry = escalate(store, "t0", Escalation("w1", "MGN will not initialize", "needs an admin", 3),
                     notify=json_notifier(sink))
    assert entry.kind == "escalation"
    assert sink and sink[0]["detail"]["hypothesis"] == "needs an admin"


def test_the_sns_notice_says_what_is_stuck_and_that_nothing_was_lost():
    sns = _FakeSns()
    notify = sns_notifier("arn:aws:sns:us-east-1:1:escalations", client=sns)
    notify(DecisionLogEntry(wave_id="wave-0", ts="t0", actor="agent", kind="escalation",
                            summary="stuck after 3 attempt(s): replication not converging",
                            detail={"hypothesis": "server is undersized", "attempts": 3,
                                    "context": "EC2AMAZ-K9LQ97Q"}))
    msg = sns.published[0]
    assert "wave-0" in msg["Subject"] and len(msg["Subject"]) < 100
    assert "server is undersized" in msg["Message"]
    assert "resumes on the next invocation" in msg["Message"]


def test_no_topic_means_no_notifier_rather_than_a_broken_one(monkeypatch):
    monkeypatch.delenv("ESCALATION_TOPIC_ARN", raising=False)
    assert sns_notifier() is None


def test_a_failed_notification_never_fails_the_wave():
    notify = sns_notifier("arn:aws:sns:us-east-1:1:x", client=_FakeSns(boom=True))
    notify(DecisionLogEntry(wave_id="w", ts="t", actor="agent", kind="escalation", summary="s"))
    # no exception: telling someone is best-effort, the decision_log is the record


# --- 2. HITL tasks survive the invocation ---

def test_a_pending_task_is_still_there_on_the_next_invocation():
    ddb, store = _FakeDdb(), InMemoryStateStore()
    q1 = DynamoDbHitlQueue("t-hitl", store, lambda: "t0", client=ddb)
    q1.submit("w1", "app_probe_spec", {"needed": "urls"})

    # a completely new queue object, as a later invocation would build
    q2 = DynamoDbHitlQueue("t-hitl", store, lambda: "t1", client=ddb)
    pending = q2.pending()
    assert [t.kind for t in pending] == ["app_probe_spec"]
    assert pending[0].payload["needed"] == "urls"


def test_resolving_updates_the_same_row_instead_of_adding_one():
    ddb, store = _FakeDdb(), InMemoryStateStore()
    q = DynamoDbHitlQueue("t-hitl", store, lambda: "t0", client=ddb)
    task = q.submit("w1", "proceed_wave_cutover", {})
    q.resolve(task, True)
    assert len(ddb.items) == 1
    assert q.pending() == []
    assert q.latest("w1", "proceed_wave_cutover").status == "APPROVED"


# --- 3. the agent remembers the previous turn ---

def test_a_session_continues_instead_of_starting_cold():
    ddb = _FakeDdb()
    s = DynamoDbSessions("t-sessions", client=ddb)
    assert s.load("sess-1") == []

    s.save("sess-1", [{"role": "user", "content": "why is wave-0 stuck?"},
                      {"role": "assistant", "content": "EC2AMAZ-K9LQ97Q is not converging"}], "t0")
    back = s.load("sess-1")
    assert len(back) == 2 and "EC2AMAZ" in json.dumps(back)


def test_history_is_bounded_so_a_long_migration_cannot_outgrow_the_row():
    ddb = _FakeDdb()
    s = DynamoDbSessions("t-sessions", client=ddb)
    kept = s.save("sess-1", [{"role": "user", "content": f"turn {i}"} for i in range(500)], "t0")
    from tools.sessions import MAX_TURNS
    assert kept == MAX_TURNS
    # the most recent turns are the ones that survive
    assert "turn 499" in json.dumps(s.load("sess-1"))


def test_a_missing_session_id_is_not_an_error():
    s = DynamoDbSessions("t-sessions", client=_FakeDdb())
    assert s.load("") == [] and s.save("", [{"a": 1}]) == 0
