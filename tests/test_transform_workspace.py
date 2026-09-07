import json
from pathlib import Path

import pytest

from agents.model import FakeModel
from agents.orchestrator import WaveInputs, WaveOutcome, build_orchestrator
from dispatcher.steps import Dispatcher
from state.store import InMemoryStateStore
from tools.hitl import InMemoryHitlQueue
from tools.spec import load_contract
from tools.transform_mcp import TransformError, TransformWorkspace, unwrap

VALID_LZA = (
    Path(__file__).resolve().parents[1] / "fixtures" / "lza" / "valid_config.yaml"
).read_text(encoding="utf-8")

WS = "ws-1"
JOB = {"jobId": "job-1", "jobName": "SourceCodeContainerization-2026", "statusDetails": {"status": "AWAITING_HUMAN_INPUT"}}
CRITICAL = {"taskId": "t-1", "title": "Set up Containerization Connector", "status": "IN_PROGRESS", "severity": "CRITICAL"}
CLOSED = {"taskId": "t-2", "title": "Disclaimer", "status": "CLOSED", "severity": "STANDARD"}


class FakeMcp:
    """Mirrors the real server: job-scoped calls fail until load_instructions is called."""

    def __init__(self, *, tasks=(CRITICAL, CLOSED), instructions_required=True):
        self.calls: list[tuple[str, dict]] = []
        self._instructed: set[str] = set()
        self._tasks = list(tasks)
        self._require = instructions_required

    def __call__(self, name, **kw):
        self.calls.append((name, kw))
        jid = kw.get("jobId")
        if name == "load_instructions":
            self._instructed.add(jid)
            return {"success": True, "data": {"instructionsFound": False, "reason": "Proceed normally."}}
        if self._require and jid and jid not in self._instructed:
            return {"success": False, "error": "INSTRUCTIONS_REQUIRED"}
        if name == "list_resources" and kw.get("resource") == "workspaces":
            return {"success": True, "data": {"items": [{"id": WS, "name": "Container-App-Migration"}]}}
        if name == "list_resources" and kw.get("resource") == "jobs":
            return {"success": True, "data": {"items": [JOB]}}
        if name == "list_resources" and kw.get("resource") == "tasks":
            return {"success": True, "data": {"items": self._tasks}}
        if name == "list_resources" and kw.get("resource") == "artifacts":
            if not kw.get("pathPrefix"):
                return {"success": True, "data": {"artifacts": [], "folders": ["out/"]}}
            return {"success": True, "data": {"artifacts": [{"artifactId": "a-1"}], "folders": []}}
        if name in ("control_job", "get_job_status", "get_resource", "complete_task"):
            return {"success": True, "data": {"ok": True, **kw}}
        return {"success": True, "data": {}}


def test_unwrap_peels_nested_mcp_envelopes():
    inner = json.dumps({"success": True, "data": {"items": [1]}})
    assert unwrap({"content": [{"type": "text", "text": inner}]}) == {"success": True, "data": {"items": [1]}}
    assert unwrap("not json") == "not json"


def test_load_instructions_runs_before_any_job_scoped_call():
    fake = FakeMcp()
    ws = TransformWorkspace(fake, WS)
    ws.tasks("job-1")
    names = [n for n, _ in fake.calls]
    assert names[0] == "load_instructions"
    # and only once for the same job
    ws.tasks("job-1")
    assert names.count("load_instructions") == 1


def test_blocking_tasks_are_open_and_critical():
    ws = TransformWorkspace(FakeMcp(), WS)
    blocking = ws.blocking_tasks("job-1")
    assert [t["taskId"] for t in blocking] == ["t-1"]
    assert len(ws.tasks("job-1")) == 2
    assert len(ws.tasks("job-1", open_only=True)) == 1


def test_walk_artifacts_descends_folders():
    ws = TransformWorkspace(FakeMcp(), WS)
    assert [a["artifactId"] for a in ws.walk_artifacts("job-1")] == ["a-1"]


def test_job_by_name_matches_loosely():
    ws = TransformWorkspace(FakeMcp(), WS)
    assert ws.job_by_name("sourcecodecontainerization")["jobId"] == "job-1"
    assert ws.job_by_name("nope") is None


def test_failed_response_raises():
    def boom(name, **kw):
        return {"success": False, "error": "nope"}

    with pytest.raises(TransformError):
        TransformWorkspace(boom, WS).jobs()


def test_list_workspaces_is_classmethod_over_a_bare_call():
    assert TransformWorkspace.list_workspaces(FakeMcp())[0]["name"] == "Container-App-Migration"


# --- the Orchestrator driving Transform's containerization job ---


def _orq(workspace):
    store = InMemoryStateStore()
    disp = Dispatcher()
    for step in ("start_replication", "cutover", "rollback"):
        disp.register(step, lambda ctx: {"ok": True})
    orq = build_orchestrator(
        FakeModel(lambda p: '{"verdict":"green","reasons":[]}' if '"verdict"' in p else VALID_LZA),
        load_contract(),
        store=store,
        dispatcher=disp,
        hitl=InMemoryHitlQueue(store, lambda: "t0"),
        clock=lambda: "t0",
        workspace=workspace,
    )
    return orq, store


def _kinds(store, wave_id):
    return [(e.kind, e.summary) for e in store.decisions(wave_id)]


def test_blocking_critical_task_escalates_instead_of_being_answered():
    ws = TransformWorkspace(FakeMcp(), WS)
    orq, store = _orq(ws)
    orq.run_wave("w-block", WaveInputs(modernization_target="container"))
    kinds = _kinds(store, "w-block")
    hitl = [s for k, s in kinds if k == "hitl"]
    assert any("Set up Containerization Connector" in s for s in hitl)
    assert any(k == "escalation" for k, _ in kinds)
    # the agent never answers a CRITICAL task on its own
    assert not any(n == "complete_task" for n, _ in ws._call.calls)


def test_unblocked_job_is_started_and_logged_as_the_modernization_artifact():
    fake = FakeMcp(tasks=(CLOSED,))
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.run_wave("w-go", WaveInputs(modernization_target="container"))
    art = [s for k, s in _kinds(store, "w-go") if k == "modernization_artifact"]
    assert art and "driven through AWS Transform" in art[0]
    assert ("control_job", {"workspaceId": WS, "jobId": "job-1", "action": "start"}) in fake.calls


def test_no_workspace_falls_back_to_the_interpreter_artifact():
    orq, store = _orq(None)
    orq.run_wave("w-fallback", WaveInputs(modernization_target="container"))
    art = [s for k, s in _kinds(store, "w-fallback") if k == "modernization_artifact"]
    assert art and "modernization IaC" in art[0]


REHOST_JOB = {"jobId": "job-2", "jobName": "VmwareMigration-2026", "statusDetails": {"status": "EXECUTING"}}


class RehostMcp(FakeMcp):
    """A workspace whose job is the rehost, not the containerization."""

    def __call__(self, name, **kw):
        if name == "list_resources" and kw.get("resource") == "jobs":
            self.calls.append((name, kw))
            return {"success": True, "data": {"items": [REHOST_JOB]}}
        return super().__call__(name, **kw)


def test_rehost_is_driven_through_transform_not_mgn():
    fake = RehostMcp(tasks=(CLOSED,))
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.run_wave("w-rehost", WaveInputs(migration_job="VmwareMigration"))
    summaries = [s for k, s in _kinds(store, "w-rehost")]
    assert any("instructed" in s for s in summaries)
    # the deterministic MGN step is never dispatched when Transform owns the rehost
    assert not any("dispatched start_replication" in s for s in summaries)


def test_rehost_blocked_on_a_human_task_escalates():
    fake = RehostMcp()  # the CRITICAL connector task is open
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.run_wave("w-rehost-blocked", WaveInputs(migration_job="VmwareMigration"))
    kinds = _kinds(store, "w-rehost-blocked")
    assert any(k == "hitl" for k, _ in kinds)
    assert any(k == "escalation" for k, _ in kinds)


PENDING = {
    "messageId": "m-1", "text": "ready to begin Wave 0 cutover",
    "messageOrigin": "SYSTEM",
    "interactions": [{"actionType": "SELECT", "data": {"selectInteractionData": {
        "options": [{"label": "Proceed with Wave 0", "value": "Proceed with Wave 0"}]}}}],
}


class WaitingMcp(RehostMcp):
    """The job sits in EXECUTING with a chat interaction pending - nothing is actually running."""

    def __call__(self, name, **kw):
        if name == "list_resources" and kw.get("resource") == "messages":
            self.calls.append((name, kw))
            return {"success": True, "data": {"messages": [PENDING]}}
        return super().__call__(name, **kw)


UNDERSIZED = [{"name": "mq-01", "instance_type": "t3a.nano", "vcpu": 2, "ram_gib": 0.5}]
RIGHT_SIZED = [{"name": "mq-01", "instance_type": "t3a.small", "vcpu": 2, "ram_gib": 2.0}]


def test_pending_interaction_is_detected():
    ws = TransformWorkspace(WaitingMcp(tasks=(CLOSED,)), WS)
    p = ws.pending_interaction("job-2")
    assert p and p["options"][0]["value"] == "Proceed with Wave 0"


def test_the_agent_refuses_to_advance_a_plan_that_breaks_the_contract():
    fake = WaitingMcp(tasks=(CLOSED,))
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.run_wave("w-denied", WaveInputs(migration_job="VmwareMigration", sizing=UNDERSIZED))
    kinds = _kinds(store, "w-denied")
    assert any(k == "policy_denial" for k, _ in kinds)
    assert any(k == "escalation" for k, _ in kinds)
    # it never answered the interaction
    assert not any(n == "send_message" for n, _ in fake.calls)


def test_a_compliant_plan_is_instructed_to_proceed():
    fake = WaitingMcp(tasks=(CLOSED,))
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.run_wave("w-go-live", WaveInputs(migration_job="VmwareMigration", sizing=RIGHT_SIZED))
    assert any("instructed" in s for _, s in _kinds(store, "w-go-live"))
    sent = [kw["text"] for n, kw in fake.calls if n == "send_message"]
    assert sent and "cutover" in sent[0].lower()


def test_a_destructive_option_is_escalated_never_chosen():
    off_list = {**PENDING, "text": "the wave plan is stale", "interactions": [
        {"actionType": "SELECT", "data": {"selectInteractionData": {"options": [
            {"label": "Delete the wave plan", "value": "Delete the wave plan"}]}}}]}

    class OffList(RehostMcp):
        def __call__(self, name, **kw):
            if name == "list_resources" and kw.get("resource") == "messages":
                self.calls.append((name, kw))
                return {"success": True, "data": {"messages": [off_list]}}
            return super().__call__(name, **kw)

    fake = OffList(tasks=(CLOSED,))
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.run_wave("w-offlist", WaveInputs(migration_job="VmwareMigration", sizing=RIGHT_SIZED))
    assert any(k == "hitl" and "irreversible, destructive" in s
               for k, s in _kinds(store, "w-offlist"))
    assert "Delete the wave plan" not in [kw["text"] for n, kw in fake.calls if n == "send_message"]


def test_a_plain_workflow_gate_is_advanced_without_a_human():
    gate = {**PENDING, "text": "Continue with these replication settings or modify them?",
            "interactions": [{"actionType": "SELECT", "data": {"selectInteractionData": {"options": [
                {"label": "Continue with these settings", "value": "Continue with these settings"},
                {"label": "Modify settings", "value": "Modify settings"}]}}}]}

    class Gate(RehostMcp):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.sent: list[str] = []

        def __call__(self, name, **kw):
            if name == "send_message":
                self.sent.append(kw.get("text", ""))
            if name == "list_resources" and kw.get("resource") == "messages":
                self.calls.append((name, kw))
                done = "Continue with these settings" in self.sent
                msg = {"messageId": "m-done", "messageOrigin": "SYSTEM",
                       "createdAt": "2026-09-02 18:00:00",
                       "processingInfo": {"messageType": "FINAL_RESPONSE"},
                       "text": "Settings saved."} if done else gate
                return {"success": True, "data": {"messages": [msg]}}
            return super().__call__(name, **kw)

    fake = Gate(tasks=(CLOSED,))
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.run_wave("w-settings", WaveInputs(migration_job="VmwareMigration", sizing=RIGHT_SIZED))
    kinds = _kinds(store, "w-settings")
    assert any('autonomously chose' in s and 'Continue with these settings' in s
               for _, s in kinds)
    assert not any(k == "escalation" for k, _ in kinds)
    sent = [kw["text"] for n, kw in fake.calls if n == "send_message"]
    assert "Continue with these settings" in sent


def test_human_approval_lets_the_denied_cutover_through():
    fake = WaitingMcp(tasks=(CLOSED,))
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.run_wave("w-approved", WaveInputs(
        migration_job="VmwareMigration", sizing=UNDERSIZED,
        approved_gates=["proceed_wave_cutover"],
    ))
    kinds = _kinds(store, "w-approved")
    # the denial is still recorded - the override is the human's, and it is auditable
    assert any(k == "policy_denial" for k, _ in kinds)
    assert any(k == "hitl" and "APPROVED" in s for k, s in kinds)
    assert any("instructed" in s for _, s in kinds)
    assert [kw for n, kw in fake.calls if n == "send_message"]


def test_pending_interaction_survives_thinking_messages_on_top():
    THINKING = {"messageId": "m-0", "text": "Routing your request...",
                "messageOrigin": "SYSTEM", "createdAt": "2026-09-02 17:09:00",
                "processingInfo": {"messageType": "THINKING"}}
    answered = dict(PENDING, createdAt="2026-09-02 17:09:11")

    class Noisy(RehostMcp):
        def __call__(self, name, **kw):
            if name == "list_resources" and kw.get("resource") == "messages":
                self.calls.append((name, kw))
                return {"success": True, "data": {"messages": [THINKING, answered]}}
            return super().__call__(name, **kw)

    ws = TransformWorkspace(Noisy(tasks=(CLOSED,)), WS)
    p = ws.pending_interaction("job-2")
    assert p and p["options"][0]["value"] == "Proceed with Wave 0"


def test_resize_approval_asks_transform_to_replan_instead_of_cutting_over():
    fake = WaitingMcp(tasks=(CLOSED,))
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.run_wave("w-resize", WaveInputs(
        migration_job="VmwareMigration", sizing=UNDERSIZED,
        approved_gates=["resize_to_peak"],
    ))
    kinds = _kinds(store, "w-resize")
    assert any(k == "policy_denial" for k, _ in kinds)
    assert any(k == "remediation_outcome" and "re-size" in s for k, s in kinds)
    sent = [kw["text"] for n, kw in fake.calls if n == "send_message"]
    assert sent and "peak utilisation" in sent[0]
    # it asks for a better plan; it never tells Transform to cut over
    assert not any("cutover" in t.lower() for t in sent)


def test_a_busy_conversation_waits_instead_of_queueing_another_message():
    from tools.transform_mcp import TransformBusy

    class Busy(RehostMcp):
        def __call__(self, name, **kw):
            if name == "send_message":
                self.calls.append((name, kw))
                return {"success": False, "error": {
                    "message": "HTTP 400: A message is already being processed for this conversation."}}
            return super().__call__(name, **kw)

    fake = Busy(tasks=(CLOSED,))
    ws = TransformWorkspace(fake, WS)
    ws.load_instructions("job-2")
    try:
        ws.send_message("job-2", "hello")
        raise AssertionError("expected TransformBusy")
    except TransformBusy:
        pass

    orq, store = _orq(TransformWorkspace(Busy(tasks=(CLOSED,)), WS))
    orq.run_wave("w-busy", WaveInputs(migration_job="VmwareMigration", sizing=RIGHT_SIZED))
    assert any("still working on the previous turn" in s for _, s in _kinds(store, "w-busy"))


def test_republishing_the_same_record_is_not_a_failure():
    class Dupe(RehostMcp):
        def __call__(self, name, **kw):
            if name == "upload_artifact":
                self.calls.append((name, kw))
                return {"success": False, "error": {"message": "HTTP 409: File already exists."}}
            return super().__call__(name, **kw)

    ws = TransformWorkspace(Dupe(tasks=(CLOSED,)), WS)
    ws.load_instructions("job-2")
    assert ws.upload_artifact("job-2", "# body", "x.md")["alreadyPublished"] == "x.md"


MGN_BLOCKED = {
    "messageId": "m-mgn", "messageOrigin": "SYSTEM", "createdAt": "2026-09-03 05:18:00",
    "processingInfo": {"messageType": "FINAL_RESPONSE"},
    "text": "I've already started the setup for Wave 0, but **AWS Application Migration Service "
            "(MGN) needs to be initialized** before we can continue. After MGN is initialized, "
            "type 'continue rehost'.",
}


class BlockedMcp(RehostMcp):
    """The assistant announces a prerequisite in prose, with no option to select."""

    def __call__(self, name, **kw):
        if name == "list_resources" and kw.get("resource") == "messages":
            self.calls.append((name, kw))
            return {"success": True, "data": {"messages": [MGN_BLOCKED]}}
        return super().__call__(name, **kw)


def test_a_prerequisite_announced_in_prose_is_cleared_and_the_job_resumed():
    fake = BlockedMcp(tasks=(CLOSED,))
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.dispatcher.register("initialize_mgn", lambda ctx: {"template_id": "tpl-1"})
    orq.run_wave("w-mgn", WaveInputs(migration_job="VmwareMigration"))

    summaries = [s for _, s in _kinds(store, "w-mgn")]
    assert any("cleared the initialize_mgn prerequisite" in s for s in summaries)
    sent = [kw["text"] for n, kw in fake.calls if n == "send_message"]
    assert "continue rehost" in sent  # the phrase the assistant asked for, not an invented one


def test_a_prerequisite_the_agent_cannot_clear_goes_to_a_human():
    fake = BlockedMcp(tasks=(CLOSED,))
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.dispatcher.register("initialize_mgn", lambda ctx: {"error": "AccessDenied"})
    orq.run_wave("w-mgn-denied", WaveInputs(migration_job="VmwareMigration"))

    kinds = _kinds(store, "w-mgn-denied")
    assert any(k == "escalation" for k, _ in kinds)


class AwaitingMcp(BlockedMcp):
    """The job is alive and asking a question, and it refuses to be restarted."""

    def __call__(self, name, **kw):
        if name == "list_resources" and kw.get("resource") == "jobs":
            self.calls.append((name, kw))
            job = dict(REHOST_JOB, statusDetails={"status": "AWAITING_HUMAN_INPUT"})
            return {"success": True, "data": {"items": [job]}}
        if name == "control_job":
            self.calls.append((name, kw))
            return {"success": False, "error": {"message": "HTTP 400: Job of this type "
                                                           "cannot be restarted"}}
        return super().__call__(name, **kw)


def test_a_job_that_refuses_a_restart_is_still_driven_through_chat():
    fake = AwaitingMcp(tasks=(CLOSED,))
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.dispatcher.register("initialize_mgn", lambda ctx: {"template_id": "tpl-1"})
    orq.run_wave("w-awaiting", WaveInputs(migration_job="VmwareMigration"))

    summaries = [s for _, s in _kinds(store, "w-awaiting")]
    assert any("cannot be restarted, driving it as-is" in s for s in summaries)
    # the refused restart is not the end of the wave: the prerequisite still gets cleared
    assert any("cleared the initialize_mgn prerequisite" in s for s in summaries)


def test_the_blocker_is_recognised_however_transform_words_it():
    from agents.orchestrator import BLOCKERS

    phrasings = [
        "AWS Application Migration Service (MGN) needs to be initialized before we continue.",
        "**Current blocker:** MGN is still **not initialized** in account 337058058699.",
    ]
    for said in phrasings:
        low = said.lower()
        assert any(all(t in low for t in terms) for terms, _, _ in BLOCKERS), said
    # a job reporting healthy MGN must not look like a blocker
    healthy = "mgn is initialized and replication has started for 12 servers".lower()
    assert not any(all(t in healthy for t in terms) for terms, _, _ in BLOCKERS)


class OptionBlockedMcp(RehostMcp):
    """The prerequisite is the preamble to an option, not a message of its own."""

    def __call__(self, name, **kw):
        if name == "list_resources" and kw.get("resource") == "messages":
            self.calls.append((name, kw))
            return {"success": True, "data": {"messages": [
                {"messageId": "m-noise", "messageOrigin": "SYSTEM",
                 "createdAt": "2026-09-03 06:00:00",
                 "processingInfo": {"messageType": "FINAL_RESPONSE"},
                 "text": "Working on migrate wave 0..."},
                {"messageId": "m-opt", "messageOrigin": "SYSTEM",
                 "createdAt": "2026-09-03 05:59:00",
                 "processingInfo": {"messageType": "FINAL_RESPONSE"},
                 "text": "MGN is still not initialized in account 337058058699.",
                 "interactions": [{"actionType": "SELECT", "data": {"selectInteractionData": {
                     "options": [{"label": "continue rehost", "value": "continue rehost"}]}}}]},
            ]}}
        return super().__call__(name, **kw)


def test_a_prerequisite_stated_alongside_an_option_is_still_recognised():
    fake = OptionBlockedMcp(tasks=(CLOSED,))
    orq, store = _orq(TransformWorkspace(fake, WS))
    orq.dispatcher.register("initialize_mgn", lambda ctx: {"template_id": "tpl-1"})
    orq.run_wave("w-opt", WaveInputs(migration_job="VmwareMigration"))

    # a newer, unrelated reply must not hide the blocker stated with the option
    assert any("cleared the initialize_mgn prerequisite" in s for _, s in _kinds(store, "w-opt"))


def test_a_prerequisite_the_agent_cannot_clear_is_published_for_a_human_to_read():
    fake = BlockedMcp(tasks=(CLOSED,))
    orq, _ = _orq(TransformWorkspace(fake, WS))
    orq.dispatcher.register("initialize_mgn", lambda ctx: {"error": "AccessDeniedException"})
    orq.run_wave("w-mgn-record", WaveInputs(migration_job="VmwareMigration"))

    uploads = [kw for n, kw in fake.calls if n == "upload_artifact"]
    assert uploads, "the escalation has to reach the console, not just the decision log"
    body = uploads[0]["content"]
    assert "AccessDeniedException" in body and "What is left for a human" in body
    assert "initialize_mgn" in uploads[0]["fileName"]


def test_classify_gate_advances_defaults_but_stops_on_judgement_calls():
    orq, _ = _orq(TransformWorkspace(RehostMcp(tasks=(CLOSED,)), WS))

    # plain progression -> pick it
    opt, stop = orq._classify_gate(
        {"text": "Review complete.", "options": [{"value": "Continue to replication"},
                                                 {"value": "Modify"}]}, False)
    assert opt == "Continue to replication" and stop is None

    # a lone "continue rehost" is safe once the guards pass
    opt, stop = orq._classify_gate({"text": "ok", "options": [{"value": "continue rehost"}]}, False)
    assert opt == "continue rehost"

    # a manual prerequisite -> escalate, do not answer
    opt, stop = orq._classify_gate(
        {"text": "no tagged subnet was found in a tagged vpc", "options": [{"value": "continue"}]},
        False)
    assert opt is None and "manual step" in stop

    # a dollar figure in the prompt is NOT judged here - Transform shows aggregate costs routinely
    opt, stop = orq._classify_gate(
        {"text": "Estimated run-rate for Wave 0 is $4,174 shown for review.",
         "options": [{"value": "Continue"}]}, False)
    assert opt == "Continue" and stop is None

    # a destructive option -> escalate even though "proceed" is present
    opt, stop = orq._classify_gate(
        {"text": "ready", "options": [{"value": "Proceed and terminate source servers"}]}, False)
    assert opt is None and "destructive" in stop

    # the cutover, with a plan that failed the contract -> the denial path owns it
    opt, stop = orq._classify_gate({"text": "ready", "options": [{"value": "Cut over now"}]}, True)
    assert opt is None and "withheld" in stop


def _orq_with_model(workspace, model):
    store = InMemoryStateStore()
    disp = Dispatcher()
    for step in ("start_replication", "cutover", "rollback"):
        disp.register(step, lambda ctx: {"ok": True})
    orq = build_orchestrator(model, load_contract(), store=store, dispatcher=disp,
                             hitl=InMemoryHitlQueue(store, lambda: "t0"), clock=lambda: "t0",
                             workspace=workspace)
    return orq, store


def test_a_configuration_choice_is_decided_by_the_model_bounded_to_the_offered_options():
    picks = FakeModel(lambda p: "Dynamic IP" if "Your choice (one option" in p else "green")
    orq, _ = _orq_with_model(TransformWorkspace(RehostMcp(tasks=(CLOSED,)), WS), picks)

    opt, stop = orq._classify_gate(
        {"text": "Choose the IPv4 address assignment for replication servers.",
         "options": [{"value": "Static IP"}, {"value": "Dynamic IP"}]}, False)
    assert opt == "Dynamic IP" and "safe default" in stop
    # the contract and the question both reached the model
    assert any("IPv4 address assignment" in p and "budget ceiling" in p for p in picks.prompts)


def test_the_model_cannot_invent_an_option_or_dodge_the_bound():
    orq, _ = _orq_with_model(TransformWorkspace(RehostMcp(tasks=(CLOSED,)), WS),
                             FakeModel(lambda p: "whatever seems best"))
    opt, stop = orq._classify_gate(
        {"text": "Pick a staging disk type.", "options": [{"value": "GP2"}, {"value": "GP3"}]},
        False)
    assert opt is None and "could not resolve" in stop


def test_the_model_may_escalate_a_choice_it_will_not_make():
    orq, _ = _orq_with_model(TransformWorkspace(RehostMcp(tasks=(CLOSED,)), WS),
                             FakeModel(lambda p: "ESCALATE"))
    opt, _reason = orq._classify_gate(
        {"text": "Provide the KMS key for EBS encryption.",
         "options": [{"value": "Use aws/ebs default"}, {"value": "Specify a customer key"}]}, False)
    assert opt is None


def test_the_agent_stops_when_the_same_choice_stops_advancing_the_workflow():
    # Deliberately not a human prerequisite: a gate that names one is caught earlier and
    # escalates on the first look, which is right and is a different test. This one is about a
    # workflow that simply stops moving.
    loop = {**PENDING, "text": "Still reconciling the landing zone; nothing has changed yet.",
            "interactions": [{"actionType": "SELECT", "data": {"selectInteractionData": {"options": [
                {"value": "Re-check status"}, {"value": "Troubleshooting help"}]}}}]}

    class Loop(RehostMcp):
        def __call__(self, name, **kw):
            if name == "list_resources" and kw.get("resource") == "messages":
                self.calls.append((name, kw))
                return {"success": True, "data": {"messages": [loop]}}
            return super().__call__(name, **kw)

    picks = FakeModel(lambda p: (
        '{"verdict":"green","reasons":[]}' if '"verdict"' in p
        else "Re-check status" if "Your choice (one option" in p
        else VALID_LZA))
    fake = Loop(tasks=(CLOSED,))
    orq, store = _orq_with_model(TransformWorkspace(fake, WS), picks)

    # the first invocations re-poll Transform once each and wait, they do not escalate
    for _ in range(3):
        assert orq.run_wave("w-loop", WaveInputs(migration_job="VmwareMigration",
                                                 sizing=RIGHT_SIZED)) != WaveOutcome.ESCALATED
    kinds = _kinds(store, "w-loop")
    assert not any(k == "hitl" and "stalled" in s for k, s in kinds)
    assert [kw["text"] for n, kw in fake.calls
            if n == "send_message"].count("Re-check status") == 3  # one nudge per invocation

    # after STALL_ESCALATE_AFTER rounds with no change it stops for real
    orq.run_wave("w-loop", WaveInputs(migration_job="VmwareMigration", sizing=RIGHT_SIZED))
    kinds = _kinds(store, "w-loop")
    assert any(k == "hitl" and "stalled" in s for k, s in kinds)
    published = [kw["content"] for n, kw in fake.calls
                if n == "upload_artifact" and "stalled" in kw.get("fileName", "")]
    assert published and "outside the agent" in published[0]


def test_a_long_wait_whose_status_keeps_changing_is_not_treated_as_a_stall():
    # Transform shows replication progress that counts up each invocation.
    class Sync(RehostMcp):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.n = 0

        def __call__(self, name, **kw):
            if name == "send_message":
                self.n += 1
            if name == "list_resources" and kw.get("resource") == "messages":
                self.calls.append((name, kw))
                msg = {**PENDING, "text": f"Initial sync: {self.n} of 12 servers complete.",
                       "interactions": [{"actionType": "SELECT", "data": {"selectInteractionData": {
                           "options": [{"value": "Check sync status"}]}}}]}
                return {"success": True, "data": {"messages": [msg]}}
            return super().__call__(name, **kw)

    picks = FakeModel(lambda p: (
        '{"verdict":"green","reasons":[]}' if '"verdict"' in p
        else "Check sync status" if "Your choice (one option" in p else VALID_LZA))
    fake = Sync(tasks=(CLOSED,))
    orq, store = _orq_with_model(TransformWorkspace(fake, WS), picks)
    for _ in range(6):
        out = orq.run_wave("w-sync", WaveInputs(migration_job="VmwareMigration", sizing=RIGHT_SIZED))
        assert out != WaveOutcome.ESCALATED
    assert not any(k == "hitl" and "stalled" in s for k, s in _kinds(store, "w-sync"))


def test_a_stall_tries_the_replication_resize_before_asking_a_human():
    loop = {**PENDING, "text": "All 12 servers still show as pending agent installation.",
            "interactions": [{"actionType": "SELECT", "data": {"selectInteractionData": {
                "options": [{"value": "Re-check status"}]}}}]}

    class Loop(RehostMcp):
        def __call__(self, name, **kw):
            if name == "list_resources" and kw.get("resource") == "messages":
                self.calls.append((name, kw))
                return {"success": True, "data": {"messages": [loop]}}
            return super().__call__(name, **kw)

    picks = FakeModel(lambda p: (
        '{"verdict":"green","reasons":[]}' if '"verdict"' in p
        else "Re-check status" if "Your choice (one option" in p else VALID_LZA))
    fake = Loop(tasks=(CLOSED,))
    orq, store = _orq_with_model(TransformWorkspace(fake, WS), picks)
    orq.dispatcher.register("resize_replication_server",
                            lambda ctx: {"resized": True, "from": "t3.small", "to": "m5.large",
                                         "stalled": ["cache-01", "nfs-01"]})
    for _ in range(4):
        orq.run_wave("w-resize", WaveInputs(migration_job="VmwareMigration", sizing=RIGHT_SIZED))

    kinds = _kinds(store, "w-resize")
    assert any(k == "remediation_outcome" and "t3.small -> m5.large" in s for k, s in kinds)
    # it fixed the cause instead of handing the stall to a person
    assert not any(k == "hitl" and "stalled" in s for k, s in kinds)


def test_a_stall_the_resize_cannot_explain_still_reaches_a_human():
    loop = {**PENDING, "text": "Still waiting on the source environment.",
            "interactions": [{"actionType": "SELECT", "data": {"selectInteractionData": {
                "options": [{"value": "Re-check status"}]}}}]}

    class Loop(RehostMcp):
        def __call__(self, name, **kw):
            if name == "list_resources" and kw.get("resource") == "messages":
                self.calls.append((name, kw))
                return {"success": True, "data": {"messages": [loop]}}
            return super().__call__(name, **kw)

    picks = FakeModel(lambda p: (
        '{"verdict":"green","reasons":[]}' if '"verdict"' in p
        else "Re-check status" if "Your choice (one option" in p else VALID_LZA))
    orq, store = _orq_with_model(TransformWorkspace(Loop(tasks=(CLOSED,)), WS), picks)
    orq.dispatcher.register("resize_replication_server",
                            lambda ctx: {"resized": False, "reason": "no source server is failing"})
    for _ in range(4):
        orq.run_wave("w-noresize", WaveInputs(migration_job="VmwareMigration", sizing=RIGHT_SIZED))

    kinds = _kinds(store, "w-noresize")
    assert any("the replication server is not the cause" in s for _, s in kinds)
    assert any(k == "hitl" and "stalled" in s for k, s in kinds)


def test_connect_failures_are_remediated_even_when_the_chat_never_stalls():
    """MGN can have servers failing to connect while Transform's chat keeps reporting progress.
    The resize must not be reachable only through the chat-stall path."""
    class Progress(RehostMcp):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.n = 0

        def __call__(self, name, **kw):
            if name == "send_message":
                self.n += 1
            if name == "list_resources" and kw.get("resource") == "messages":
                self.calls.append((name, kw))
                msg = {**PENDING, "text": f"Initial sync: {self.n} of 12 servers complete.",
                       "interactions": [{"actionType": "SELECT", "data": {"selectInteractionData": {
                           "options": [{"value": "Check sync status"}]}}}]}
                return {"success": True, "data": {"messages": [msg]}}
            return super().__call__(name, **kw)

    picks = FakeModel(lambda p: (
        '{"verdict":"green","reasons":[]}' if '"verdict"' in p
        else "Check sync status" if "Your choice (one option" in p else VALID_LZA))
    orq, store = _orq_with_model(TransformWorkspace(Progress(tasks=(CLOSED,)), WS), picks)
    orq.dispatcher.register("resize_replication_server",
                            lambda ctx: {"resized": True, "from": "t3.small", "to": "m5.large",
                                         "stalled": ["cache-01", "nfs-01"]})
    orq.run_wave("w-silent", WaveInputs(migration_job="VmwareMigration", sizing=RIGHT_SIZED))

    kinds = _kinds(store, "w-silent")
    assert any(k == "remediation_outcome" and "m5.large" in s for k, s in kinds), (
        "the chat never stalls, so the resize is never reached")



def test_the_opening_message_carries_what_mgn_reports():
    """An AWS Transform chat thread is per-identity: the agent's conversation starts empty however
    much context a person gave the job in theirs, and an assistant told nothing about the estate
    answers "the workflow is at the very beginning"."""
    fake = RehostMcp(tasks=(CLOSED,))
    picks = FakeModel(lambda p: (
        '{"verdict":"green","reasons":[]}' if '"verdict"' in p
        else "Proceed" if "Your choice (one option" in p
        else VALID_LZA))
    orq, _ = _orq_with_model(TransformWorkspace(fake, WS), picks)
    orq.dispatcher.register("mgn_status", lambda ctx: {
        "total": 13, "by_state": {"CONTINUOUS": 12, "PENDING_INSTALLATION": 1}, "stalled": []})

    note = orq._estate_note()
    assert "13 source servers" in note and "12 of them replicating" in note


def test_the_opening_message_is_a_read_out_not_a_claim():
    """It must pass the same honesty check as anything the agent sends through its tools - the two
    paths reach the same assistant and must not be allowed to diverge."""
    from agents.orchestrator import claims_unverifiable_work

    fake = RehostMcp(tasks=(CLOSED,))
    orq, _ = _orq_with_model(TransformWorkspace(fake, WS), FakeModel(["ok"]))
    orq.dispatcher.register("mgn_status", lambda ctx: {
        "total": 13, "by_state": {"CONTINUOUS": 12}, "stalled": [{"host": "x", "error": "e"}]})

    text = f"Continue the J Wave 0 rehost through to cutover.{orq._estate_note()}"
    assert claims_unverifiable_work(text) is None


def test_no_estate_note_when_mgn_cannot_be_read():
    fake = RehostMcp(tasks=(CLOSED,))
    orq, _ = _orq_with_model(TransformWorkspace(fake, WS), FakeModel(["ok"]))
    assert orq._estate_note() == ""


# --- affirming a manual step is checked against MGN, never guessed ------------------------------
# "Completed" is an innocuous word until it is the reply to "install the agent on your source
# servers". The agent answered one of these correctly by guessing, which is the same thing as
# answering the next one wrongly by guessing.

INSTALL_GATE = "#### Step 3: Install the Agent on Your Source Servers\nUse your organization's tools"


def test_an_affirmation_at_an_installation_gate_is_recognised():
    from agents.orchestrator import affirms_a_manual_step

    assert affirms_a_manual_step(INSTALL_GATE, "Completed")
    assert affirms_a_manual_step(INSTALL_GATE, "Yes, completed")
    assert affirms_a_manual_step(INSTALL_GATE, "done")
    # not an affirmation, and not that gate
    assert not affirms_a_manual_step(INSTALL_GATE, "Proceed")
    assert not affirms_a_manual_step("Pick an export format", "Completed")


def test_mgn_is_what_decides_whether_the_step_is_finished():
    from agents.orchestrator import installation_is_done

    ready = {"servers": [{"host": "web-01", "lifecycle": "READY_FOR_TEST"},
                         {"host": "db-01", "lifecycle": "READY_FOR_TEST"}]}
    assert installation_is_done(ready, ["web-01", "db-01"])[0] is True

    half = {"servers": [{"host": "web-01", "lifecycle": "READY_FOR_TEST"},
                        {"host": "db-01", "lifecycle": "PENDING_INSTALLATION"}]}
    done, why = installation_is_done(half, ["web-01", "db-01"])
    assert done is False and "db-01" in why


def test_a_pending_server_outside_the_wave_neither_satisfies_nor_blocks():
    """Orphaned placeholder records sit at PENDING_INSTALLATION forever. They are not this wave."""
    from agents.orchestrator import installation_is_done

    mixed = {"servers": [{"host": "web-01", "lifecycle": "READY_FOR_TEST"},
                         {"host": "ghost-01", "lifecycle": "PENDING_INSTALLATION"}]}
    assert installation_is_done(mixed, ["web-01"])[0] is True


def test_no_evidence_is_not_evidence_of_completion():
    from agents.orchestrator import installation_is_done

    assert installation_is_done({}, ["web-01"])[0] is False
    assert installation_is_done({"servers": []}, ["web-01"])[0] is False
    assert installation_is_done({"servers": [{"host": "other", "lifecycle": "READY_FOR_TEST"}]},
                                ["web-01"])[0] is False


def test_the_wave_refuses_the_affirmation_when_mgn_does_not_back_it():
    gate = {**PENDING, "text": INSTALL_GATE,
            "interactions": [{"actionType": "SELECT", "data": {"selectInteractionData": {
                "options": [{"value": "Completed"}]}}}]}

    class Gate(RehostMcp):
        def __call__(self, name, **kw):
            if name == "list_resources" and kw.get("resource") == "messages":
                self.calls.append((name, kw))
                return {"success": True, "data": {"messages": [gate]}}
            return super().__call__(name, **kw)

    picks = FakeModel(lambda p: (
        '{"verdict":"green","reasons":[]}' if '"verdict"' in p
        else "Completed" if "Your choice (one option" in p
        else VALID_LZA))
    fake = Gate(tasks=(CLOSED,))
    orq, store = _orq_with_model(TransformWorkspace(fake, WS), picks)
    orq.dispatcher.register("mgn_status", lambda ctx: {
        "servers": [{"host": "mq-01", "lifecycle": "PENDING_INSTALLATION"}]})

    assert orq.run_wave("w-affirm", WaveInputs(migration_job="VmwareMigration",
                                               sizing=RIGHT_SIZED)) == WaveOutcome.ESCALATED
    assert any("would assert work nobody has shown was done" in (e.detail or {}).get("hypothesis", "")
               for e in store.decisions("w-affirm"))
    assert [kw for n, kw in fake.calls if n == "send_message" and kw.get("text") == "Completed"] == []
