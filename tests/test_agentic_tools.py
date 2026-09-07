"""The agentic loop lets the model pick the order. These prove the guards do not depend on it."""
import json

from agents.model import FakeModel
from agents.orchestrator import build_orchestrator
from agents.tools import build_tools
from dispatcher.steps import Dispatcher
from state.store import InMemoryStateStore
from tools.hitl import InMemoryHitlQueue
from tools.spec import load_contract

JOB = {"jobId": "job-1", "jobName": "VmwareMigration-2026"}


def _orq(steps=None):
    store = InMemoryStateStore()
    disp = Dispatcher()
    for name, fn in (steps or {}).items():
        disp.register(name, fn)
    orq = build_orchestrator(
        FakeModel(["ok"]), load_contract(), store=store, dispatcher=disp,
        hitl=InMemoryHitlQueue(store, lambda: "t0"), clock=lambda: "t0",
    )
    return orq, store


def _by_name(tools):
    return {t.tool_name: t for t in tools}


class _Ws:
    def __init__(self, asking="", options=()):
        self.sent = []
        self._asking, self._options = asking, list(options)

    def pending_interaction(self, _jid):
        return {"text": self._asking, "options": [{"value": o} for o in self._options]}

    def send_message(self, _jid, text, skip_polling=True):
        self.sent.append(text)
        return {"ok": True}


def test_the_agent_cannot_talk_transform_into_a_destructive_action():
    orq, _ = _orq()
    ws = _Ws(asking="ready to finish")
    answer = _by_name(build_tools(orq, workspace=ws, plan_job=JOB))["answer_transform"]

    out = json.loads(answer("w1", "Yes, terminate the source servers and finish"))
    assert out["sent"] is False and "destructive" in out["refused"]
    assert ws.sent == []  # nothing reached AWS Transform


def test_the_agent_cannot_claim_a_manual_prerequisite_was_done():
    orq, _ = _orq()
    ws = _Ws(asking="We're still blocked - no tagged subnet was found in a tagged VPC.")
    answer = _by_name(build_tools(orq, workspace=ws, plan_job=JOB))["answer_transform"]

    out = json.loads(answer("w1", "continue rehost"))
    assert out["sent"] is False and "manual step" in out["refused"]
    assert ws.sent == []


def test_a_legitimate_reply_still_goes_through():
    orq, _ = _orq()
    ws = _Ws(asking="Continue with these replication settings?")
    answer = _by_name(build_tools(orq, workspace=ws, plan_job=JOB))["answer_transform"]

    out = json.loads(answer("w1", "Continue with these settings"))
    assert out["sent"] is True and ws.sent == ["Continue with these settings"]


def test_the_sanity_check_refuses_to_invent_a_probe_spec():
    orq, _ = _orq(steps={"probe_apps": lambda ctx: {"probed": 99}})
    check = _by_name(build_tools(orq, plan_job=JOB))["sanity_check_apps"]

    out = json.loads(check("w1"))
    assert out["probed"] == 0 and "ask the engineer" in out["missing"]


def test_the_agent_reads_mgn_directly_not_transform_s_narrative():
    seen = {}

    def _status(ctx):
        seen["called"] = True
        return {"total": 2, "by_state": {"CONTINUOUS": 1, "STALLED": 1},
                "stalled": [{"host": "nfs-01", "error": "FAILED_TO_CONNECT"}],
                "servers": [{"host": "nfs-01"}, {"host": "mq-01"}]}

    orq, _ = _orq(steps={"mgn_status": _status})
    health = _by_name(build_tools(orq, plan_job=JOB))["mgn_replication_health"]

    out = json.loads(health())
    assert seen.get("called") and out["stalled"][0]["host"] == "nfs-01"
    assert out["servers"]  # the detailed view keeps the per-server rows


def test_remediation_is_delegated_to_the_specialist_not_hand_rolled():
    orq, store = _orq()
    called = {}

    class _Result:
        resolved, from_runbook, attempts, actions = True, True, 2, ["restart agent"]

    def _fake_delegate(which, objective, wave_id=""):
        called.update(which=which, objective=objective)
        return _Result()

    orq.delegate = _fake_delegate
    remediate = _by_name(build_tools(orq, plan_job=JOB))["diagnose_and_remediate"]

    out = json.loads(remediate("w1", "8 servers stalled on connect"))
    assert called["which"] == "remediation"  # an agent, not a hardcoded fix
    assert out["resolved"] and out["from_runbook"]
    assert any(e.kind == "remediation_outcome" for e in store.decisions("w1"))


def test_asking_the_engineer_never_requests_a_credential():
    orq, store = _orq()
    ask = _by_name(build_tools(orq, plan_job=JOB))["ask_engineer"]

    json.loads(ask("w1", "what URL should I check for the billing app?"))
    assert any(e.kind == "hitl" for e in store.decisions("w1"))
    # the docstring is the contract the model reads; it must say not to ask for the secret itself
    assert "Never ask for a credential" in ask.__doc__ or "never ask" in ask.__doc__.lower()


def test_the_agentic_entrypoint_does_not_break_what_already_works(monkeypatch):
    """The scheduler's resume, a direct wave run and the console's reads must keep working, so the
    new surface is additive rather than a cutover in disguise."""
    import agents.agentic as ag

    seen = []
    monkeypatch.setattr("agents.runtime.invoke", lambda p: seen.append(p) or {"routed": True})

    for payload in ({"action": "resume"}, {"wave_id": "w1"}, {"read_chat": True},
                    {"ask": "why?"}, {"say": "continue"}):
        assert ag.invoke(payload) == {"routed": True}
    assert len(seen) == 5

    # and an empty payload says what it accepts instead of failing silently
    assert "prompt" in ag.invoke({})["error"]


def test_the_safety_rule_has_one_definition_used_by_both_paths():
    """The wave checks the options Transform offers; the agent tool checks the message it is about
    to send. Different inputs, same rule - so it cannot drift on one side only."""
    from agents.orchestrator import carries_stop_signal, manual_prerequisite

    assert carries_stop_signal("Proceed and terminate the source servers") == "terminate"
    assert carries_stop_signal("Continue with these settings") is None
    assert "tag a VPC" in manual_prerequisite("no tagged subnet was found in a tagged vpc")
    assert manual_prerequisite("Continue with these replication settings?") is None


def test_a_refusal_names_the_phrase_that_caused_it():
    orq, _ = _orq()
    ws = _Ws(asking="ready")
    answer = _by_name(build_tools(orq, workspace=ws, plan_job=JOB))["answer_transform"]

    out = json.loads(answer("w1", "yes, this cannot be undone but go ahead"))
    assert out["sent"] is False and "cannot be undone" in out["refused"]


def test_remediation_resolves_a_hostname_so_the_model_never_carries_an_opaque_id():
    seen = {}

    def _status(ctx):
        return {"total": 1, "by_state": {}, "stalled": [], "servers": [
            {"host": "EC2AMAZ-K9LQ97Q", "source_server_id": "s-339dad88", "state": "STALLED"}]}

    orq, _ = _orq(steps={"mgn_status": _status})

    class _R:
        resolved, from_runbook, attempts, action, actions = True, False, 1, "resync_volume", []

    def _delegate(which, objective, wave_id=""):
        seen["objective"] = json.loads(objective)
        return _R()

    orq.delegate = _delegate
    remediate = _by_name(build_tools(orq, plan_job=JOB))["diagnose_and_remediate"]

    out = json.loads(remediate("w1", "stalled with NOT_CONVERGING", host="EC2AMAZ-K9LQ97Q"))
    assert out["resolved"]
    assert seen["objective"]["target"]["source_server_id"] == "s-339dad88"


def test_an_unknown_hostname_is_reported_not_guessed():
    orq, _ = _orq(steps={"mgn_status": lambda ctx: {"servers": [{"host": "mq-01",
                                                                 "source_server_id": "s-1"}]}})
    remediate = _by_name(build_tools(orq, plan_job=JOB))["diagnose_and_remediate"]

    out = json.loads(remediate("w1", "something broke", host="not-a-real-host"))
    assert out["resolved"] is False and "not a source server MGN knows" in out["detail"]


# --- the agent must never assert work it has no tool to do -----------------------------------
# It once replied "Agents are being installed as per the instruction" to a gate nobody was working
# on. AWS Transform acts on what it is told, so that is not a harmless filler message.

def test_it_refuses_to_claim_an_agent_was_installed():
    orq, _ = _orq()
    ws = _Ws(asking="0 of 12 agents detected. Install agents, then type 'Re-check status'.")
    tools = _by_name(build_tools(orq, workspace=ws, plan_job=JOB))
    out = json.loads(tools["answer_transform"]("w1", "Agents are being installed as per the instruction."))
    assert out["sent"] is False and "cannot do" in out["refused"]
    assert ws.sent == []


def test_the_refusal_does_not_depend_on_recognising_the_question():
    """A list of AWS Transform's phrasings will always be incomplete; the rule is about the message
    going out, so it holds even when nothing is pending."""
    orq, _ = _orq()
    ws = _Ws(asking="")
    tools = _by_name(build_tools(orq, workspace=ws, plan_job=JOB))
    for lie in ("We have configured the staging subnet.",
                "The VPC was tagged already.",
                "I have installed the replication agent on mq-01."):
        out = json.loads(tools["answer_transform"]("w1", lie))
        assert out["sent"] is False, lie
    assert ws.sent == []


def test_an_ordinary_workflow_answer_still_goes_through():
    orq, _ = _orq()
    ws = _Ws(asking="Ready to continue?")
    tools = _by_name(build_tools(orq, workspace=ws, plan_job=JOB))
    for ok in ("Re-check status", "Continue the Wave 0 rehost through to cutover."):
        assert json.loads(tools["answer_transform"]("w1", ok))["sent"] is True
    assert len(ws.sent) == 2


def test_a_status_re_read_is_still_allowed_at_an_installation_gate():
    """The gate that says "install agents, then type Re-check status" offers only a status re-read.
    Refusing it would also refuse the re-check that clears the gate once the agents are there."""
    orq, _ = _orq()
    ws = _Ws(asking="0 of 12 agents detected. Install agents, then type 'Re-check status'.")
    tools = _by_name(build_tools(orq, workspace=ws, plan_job=JOB))
    assert json.loads(tools["answer_transform"]("w1", "Re-check status"))["sent"] is True


def test_the_agent_cannot_choose_which_human_gate_it_opens():
    """The console answers `app_probe_spec` with a URL form and the cutover gate with a
    proceed/refuse decision, so a question filed under the wrong gate is answered with the wrong
    instrument - and a model free to name any gate can mark any of them as merely asked."""
    orq, _ = _orq()
    tools = _by_name(build_tools(orq, workspace=_Ws(), plan_job=JOB))
    assert "kind" not in tools["ask_engineer"].tool_spec["inputSchema"]["json"]["properties"]

    tools["ask_engineer"]("w1", "What is the maximum variance percentage for the budget?")
    tools["ask_engineer"]("w1", "Can you provide the server inventory file?")
    assert {t.kind for t in orq.hitl.pending()} == {"engineer_question"}


def test_re_asking_the_same_question_writes_no_second_record():
    """A scheduled agent re-asks every five minutes. The queue was already idempotent; the log was
    not, so the record filled with lines about asking something nobody was asked."""
    orq, store = _orq()
    tools = _by_name(build_tools(orq, workspace=_Ws(), plan_job=JOB))

    first = json.loads(tools["ask_engineer"]("w1", "Which URL should I check?"))
    again = json.loads(tools["ask_engineer"]("w1", "Which URL should I check?"))

    assert first["asked"] is True
    assert again["asked"] is False and again["already_open"] is True
    asked = [e for e in store.decisions("w1") if e.summary.startswith("asked the engineer")]
    assert len(asked) == 1


def test_the_agent_is_told_it_already_asked_so_it_can_choose_otherwise():
    orq, _ = _orq()
    tools = _by_name(build_tools(orq, workspace=_Ws(), plan_job=JOB))
    tools["ask_engineer"]("w1", "Which URL?")
    again = json.loads(tools["ask_engineer"]("w1", "Which URL?"))
    assert "do something else or stop" in again["note"]
