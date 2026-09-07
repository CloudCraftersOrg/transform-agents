import json

import pytest

from tools import console


@pytest.fixture(autouse=True)
def _clear_attention_cache():
    """The cache is module-level, so without this one test reads another's answer."""
    console._attention_cache.update(at=0.0, value=None)
    yield
    console._attention_cache.update(at=0.0, value=None)


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kw):
        return self._pages


class _Dynamo:
    def __init__(self, pages):
        self._pages = pages

    def get_paginator(self, _name):
        return _Paginator(self._pages)


def _doc(**kw):
    return {"doc": {"S": json.dumps(kw)}}


def test_waves_are_listed_newest_first(monkeypatch):
    pages = [{"Items": [
        _doc(wave_id="w-old", status="DONE", updated_at="2026-09-01T00:00:00"),
        _doc(wave_id="w-new", status="ESCALATED", updated_at="2026-09-03T00:00:00"),
    ]}]
    monkeypatch.setattr(console, "_client", lambda _s: _Dynamo(pages))
    assert [w["wave_id"] for w in console.waves()] == ["w-new", "w-old"]


def test_the_decision_log_reads_as_a_narrative_not_a_tail(monkeypatch):
    pages = [{"Items": [
        _doc(wave_id="w1", ts="t3", actor="orchestrator", kind="escalation", summary="c"),
        _doc(wave_id="w1", ts="t1", actor="orchestrator", kind="decision", summary="a"),
        _doc(wave_id="w1", ts="t2", actor="orchestrator", kind="policy_denial", summary="b"),
    ]}]
    monkeypatch.setattr(console, "_client", lambda _s: _Dynamo(pages))
    assert [e["summary"] for e in console.decisions("w1")] == ["a", "b", "c"]


def test_a_halted_wave_reports_no_pipeline_position(monkeypatch):
    monkeypatch.setattr(console, "waves", lambda: [{"wave_id": "w1", "status": "ESCALATED",
                                                    "completed_steps": []}])
    monkeypatch.setattr(console, "decisions", lambda _w: [
        {"wave_id": "w1", "ts": "t1", "actor": "a", "kind": "escalation", "summary": "s"},
    ])
    view = console.wave_view("w1")
    assert view["position"] == -1 and view["wave"]["status"] in view["off_ramps"]
    assert view["counts"] == {"escalation": 1}


def test_the_log_filter_allows_agent_output_and_real_failures_only():
    keep = [
        "AGENT !! escalation               w-1  could not clear the initialize_mgn prerequisite",
        "AGENT    runtime                  -     migration workspace: EPAM-PoC-Business-Case",
        "Traceback (most recent call last):",
        '  File "/app/agents/runtime.py", line 199, in invoke',
        "[tool:send_message] error response | REQUEST_FAILED | HTTP 400",
    ]
    drop = [
        "INFO | __main__:_call_region:233 - Region discovery: calling https://api.transform",
        "INFO     Found credentials from IAM Role: execution_role",
        "INFO     HTTP Request: GET https://x.s3.amazonaws.com/?X-Amz-Signature=abc",
        "botocore.parsers - DEBUG - Response headers: {}",
        "INFO     Processing request of type CallToolRequest",
    ]
    assert all(console.SIGNAL.search(line) for line in keep)
    assert not any(console.SIGNAL.search(line) for line in drop)


def test_attention_lists_escalated_waves_and_unanswered_questions(monkeypatch):
    """The agent writes when it is blocked and nobody is told. This panel is the channel."""
    monkeypatch.setattr(console, "waves", lambda: [
        {"wave_id": "w-stuck", "status": "ESCALATED", "updated_at": "2026-09-03T10:00:00"},
        {"wave_id": "w-fine", "status": "REPLICATING", "updated_at": "2026-09-03T11:00:00"},
    ])
    monkeypatch.setattr(console, "decisions", lambda w: [
        {"kind": "decision", "ts": "t1", "summary": "started"},
        {"kind": "escalation", "ts": "2026-09-03T10:00:00",
         "summary": "stuck after 3 attempt(s): MGN will not initialize",
         "detail": {"hypothesis": "needs an account administrator"}},
    ])
    monkeypatch.setattr(console, "_pending_hitl", lambda: [
        {"wave_id": "w-fine", "kind": "app_probe_spec", "ts": "2026-09-03T12:00:00",
         "payload": {"needed": "a URL per application"}},
    ])

    a = console.attention()
    assert a["count"] == 2
    # newest first, so the freshest thing needing a person is at the top
    assert a["items"][0]["kind"] == "question" and a["items"][0]["wave_id"] == "w-fine"
    esc = a["items"][1]
    assert esc["kind"] == "escalation" and "needs an account administrator" in esc["hypothesis"]
    # a healthy wave with nothing pending never appears
    assert not any(i["wave_id"] == "w-fine" and i["kind"] == "escalation" for i in a["items"])


def test_attention_is_empty_when_nothing_needs_a_person(monkeypatch):
    monkeypatch.setattr(console, "waves", lambda: [{"wave_id": "w", "status": "REPLICATING"}])
    monkeypatch.setattr(console, "_pending_hitl", list)
    assert console.attention() == {"count": 0, "items": []}


def test_an_unreadable_hitl_table_degrades_the_panel_not_the_console(monkeypatch):
    def _boom(_svc):
        raise RuntimeError("table gone")

    monkeypatch.setattr(console, "_client", _boom)
    assert console._pending_hitl() == []


def test_an_escalation_reason_is_one_scannable_line_not_a_stack_trace():
    """Some escalations carry a whole boto3 traceback because the failing step's detail was passed
    straight through. The panel has to be readable at a glance."""
    raw = json.dumps({
        "errorMessage": "An error occurred (BadRequestException) when calling StartReplication",
        "errorType": "ClientError",
        "stackTrace": ["File a, line 1", "File b, line 2"],
    })
    out = console._one_line_reason(raw)
    assert out == "An error occurred (BadRequestException) when calling StartReplication"
    assert "stackTrace" not in out and "\n" not in out


def test_a_plain_reason_is_left_alone_and_nothing_is_not_an_error():
    assert console._one_line_reason("invalid config after the iteration budget") == (
        "invalid config after the iteration budget")
    assert console._one_line_reason(None) == ""


def test_a_very_long_reason_is_truncated_rather_than_flooding_the_panel():
    out = console._one_line_reason("x" * 500)
    assert len(out) <= 223 and out.endswith("...")


# --- answering the agent's sanity-check question from the console ------------------------------
# The panel could show what the agent was waiting on and offer no way to answer it, so the only
# route was hand-writing a DynamoDB item.

class _Ddb:
    def __init__(self, pending=()):
        self.puts = []
        self._pending = list(pending)

    def put_item(self, TableName, Item):  # boto3 casing
        self.puts.append((TableName, Item))

    def scan(self, **kw):
        return {"Items": self._pending}


def _hitl_row(wave="w1", kind="app_probe_spec", status="PENDING"):
    return {"task_key": {"S": f"{wave}#{kind}#t0"}, "wave_id": {"S": wave},
            "kind": {"S": kind}, "status": {"S": status}, "ts": {"S": "t0"},
            "payload": {"S": "{}"}}


@pytest.fixture
def ddb(monkeypatch):
    def _install(pending=()):
        client = _Ddb(pending)
        monkeypatch.setattr(console, "_client", lambda _name, c=None: client)
        return client
    return _install


def test_an_answer_is_a_sentence_not_a_form(ddb):
    """A form was the wrong instrument. The agent works out where its applications answer by
    itself; what it cannot work out is what a person means by healthy, and that is a sentence."""
    client = ddb([_hitl_row()])
    out = console.reply_to_agent("w1", "app_probe_spec",
                                 "the leaderboard should list teams and the catalog show products")

    assert out["answered"] is True and out["questions_closed"] == 1
    logged = [i for t, i in client.puts if t == console.LOG_TABLE]
    doc = json.loads(logged[0]["doc"]["S"])
    assert doc["actor"] == "human" and doc["kind"] == "hitl"
    assert doc["detail"]["engineer_reply"].startswith("the leaderboard")
    assert doc["detail"]["gate"] == "app_probe_spec"


def test_an_empty_answer_is_refused(ddb):
    ddb([])
    with pytest.raises(ValueError, match="write an answer"):
        console.reply_to_agent("w1", "app_probe_spec", "   ")


def test_a_reply_with_no_gate_is_recorded_without_closing_anything(ddb):
    client = ddb([_hitl_row()])
    out = console.reply_to_agent("w1", "", "just so you know, catalog-svc is being patched")
    assert out["questions_closed"] == 0
    assert [i for t, i in client.puts if t == console.HITL_TABLE] == []


def test_only_this_wave_and_this_gate_are_closed(ddb):
    ddb([_hitl_row(), _hitl_row(wave="w2"), _hitl_row(kind="proceed_wave_cutover"),
         _hitl_row(status="APPROVED")])
    assert console.reply_to_agent("w1", "app_probe_spec", "looks fine")["questions_closed"] == 1


def test_a_second_console_refuses_to_share_the_port():
    """HTTPServer sets SO_REUSEADDR and Windows honours it, so two consoles both "listen" and split
    the requests. console.html is read per request while the Python is whatever each process
    imported at startup, so the older one serves a new page against old handlers."""
    first = console.serve(port=8799, host="127.0.0.1")
    try:
        with pytest.raises(SystemExit, match="already serving"):
            console.serve(port=8799, host="127.0.0.1")
    finally:
        first.server_close()
