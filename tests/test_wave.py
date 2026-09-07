from pathlib import Path

from agents.model import FakeModel
from agents.orchestrator import WaveInputs, WaveOutcome, build_orchestrator
from dispatcher.steps import Dispatcher
from state.models import WaveStatus
from state.store import InMemoryStateStore
from tools.hitl import APPROVE_DERIVED_CONTRACT, InMemoryHitlQueue
from tools.spec import load_contract

VALID_LZA = (
    Path(__file__).resolve().parents[1] / "fixtures" / "lza" / "valid_config.yaml"
).read_text(encoding="utf-8")

TAGGED_CHEAP = [{"instance_type": "t3.small", "ebs_gib": 10, "tags": {"app": "billing"}}]

# What the engineer supplies so the cutover has something real to be judged against.
PROBES = [{"name": "billing", "url": "http://billing.internal/health", "expect_status": 200}]
HEALTHY = {"probed": 1, "healthy": 1,
           "results": [{"name": "billing", "ok": True, "status": 200, "fingerprint": "abc"}]}


def _model(verdict: str = "green"):
    def respond(prompt: str) -> str:
        if '"verdict"' in prompt:
            return f'{{"verdict":"{verdict}","reasons":["schema changed"]}}'
        if '"attribution"' in prompt:
            return '{"attribution":"EBS oversized"}'
        return VALID_LZA

    return FakeModel(respond)


def _orq(model):
    store = InMemoryStateStore()
    disp = Dispatcher()
    disp.register("start_replication", lambda ctx: {"replicating": True})
    disp.register("cutover", lambda ctx: {"done": True})
    disp.register("rollback", lambda ctx: {"rolled_back": True})
    disp.register("probe_apps", lambda ctx: HEALTHY)
    hitl = InMemoryHitlQueue(store, lambda: "t0")
    orq = build_orchestrator(
        model, load_contract(), store=store, dispatcher=disp, hitl=hitl, clock=lambda: "t0"
    )
    return orq, store


def test_happy_path_reaches_done():
    orq, store = _orq(_model())
    out = orq.run_wave("w1", WaveInputs(finops_resources=TAGGED_CHEAP, app_probes=PROBES))
    assert out is WaveOutcome.DONE
    wave = store.get_wave("w1")
    assert wave.status is WaveStatus.DONE
    assert wave.completed_steps == [
        "precheck", "interpret", "replicate", "test", "cutover", "parity", "finops"
    ]
    assert any(e.kind == "finops_variance" for e in store.decisions("w1"))


def test_waits_on_replication_then_resumes():
    orq, store = _orq(_model())
    ready = {"v": False}
    out = orq.run_wave("w1", WaveInputs(app_probes=PROBES), replication_ready=lambda _w: ready["v"])
    assert out is WaveOutcome.WAITING
    assert store.get_wave("w1").status is WaveStatus.REPLICATING
    assert store.get_wave("w1").completed_steps == ["precheck", "interpret"]

    ready["v"] = True
    out2 = orq.run_wave(
        "w1", WaveInputs(finops_resources=TAGGED_CHEAP, app_probes=PROBES), replication_ready=lambda _w: True
    )
    assert out2 is WaveOutcome.DONE


def test_red_verdict_rolls_back():
    orq, store = _orq(_model(verdict="red"))
    out = orq.run_wave("w1", WaveInputs(test_diffs={"changed": {"total": [1, "x"]}}))
    assert out is WaveOutcome.ROLLED_BACK
    assert store.get_wave("w1").status is WaveStatus.ROLLED_BACK
    assert "test" not in store.get_wave("w1").completed_steps
    assert "rollback" in [e.kind for e in store.decisions("w1")]


def test_interpreter_failure_escalates():
    def respond(prompt: str) -> str:
        return "global_config: {}" if "OBJECTIVE" in prompt else "{}"

    orq, store = _orq(FakeModel(respond))
    out = orq.run_wave("w1")
    assert out is WaveOutcome.ESCALATED
    assert store.get_wave("w1").status is WaveStatus.ESCALATED
    assert any(e.kind == "escalation" for e in store.decisions("w1"))


def test_contract_approval_gate_blocks_until_approved():
    orq, _ = _orq(_model())
    assert orq.run_wave("w1", require_contract_approval=True) is WaveOutcome.WAITING
    assert len(orq.hitl.pending()) == 1
    assert orq.run_wave("w1", require_contract_approval=True) is WaveOutcome.WAITING

    orq.hitl.resolve(orq.hitl.latest("w1", APPROVE_DERIVED_CONTRACT), approved=True)
    out = orq.run_wave(
        "w1", WaveInputs(finops_resources=TAGGED_CHEAP, app_probes=PROBES), require_contract_approval=True
    )
    assert out is WaveOutcome.DONE


def test_rejected_contract_escalates():
    orq, _ = _orq(_model())
    orq.run_wave("w1", require_contract_approval=True)
    orq.hitl.resolve(orq.hitl.latest("w1", APPROVE_DERIVED_CONTRACT), approved=False)
    assert orq.run_wave("w1", require_contract_approval=True) is WaveOutcome.ESCALATED


_VALID_IAC = """
resource "aws_ecr_repository" "app" { name = "svc" }
resource "aws_ecs_task_definition" "app" {
  family                = "svc"
  container_definitions = "[{\\"image\\":\\"svc:latest\\"}]"
}
resource "aws_ecs_service" "app" { name = "svc" cluster = "prod" }
"""


def test_wave_generates_a_verified_modernization_artifact():
    def respond(prompt: str) -> str:
        if '"verdict"' in prompt:
            return '{"verdict":"green","reasons":[]}'
        if "Terraform HCL" in prompt:
            return _VALID_IAC
        return VALID_LZA

    orq, store = _orq(FakeModel(respond))
    out = orq.run_wave(
        "w1", WaveInputs(finops_resources=TAGGED_CHEAP, app_probes=PROBES, modernization_target="container")
    )
    assert out is WaveOutcome.DONE
    arts = [e for e in store.decisions("w1") if e.kind == "modernization_artifact"]
    assert len(arts) == 1 and arts[0].detail["verified"] is True and arts[0].detail["iac"]


def test_wave_reports_when_the_target_cannot_be_modernized():
    orq, store = _orq(_model())
    out = orq.run_wave(
        "w1",
        WaveInputs(
            finops_resources=TAGGED_CHEAP,
            app_probes=PROBES,
            modernization_target="hack",
            modernization_subject="fbctf-app",
        ),
    )
    assert out is WaveOutcome.DONE  # lift-and-shift still completes
    kinds = [e.kind for e in store.decisions("w1")]
    assert "escalation" in kinds  # deliverable 5: "Transform can't modernize this"
    assert not any(e.kind == "modernization_artifact" for e in store.decisions("w1"))


def test_server_side_step_rejection_escalates_not_crashes(monkeypatch):
    from dispatcher.steps import StepRejected

    orq, store = _orq(_model())

    def boom(*_a, **_k):
        raise StepRejected("policy said no")

    monkeypatch.setattr(orq, "_run_step", boom)
    out = orq.run_wave("w1")
    assert out is WaveOutcome.ESCALATED
    assert store.get_wave("w1").status is WaveStatus.ESCALATED
    assert any(e.kind == "escalation" for e in store.decisions("w1"))


def test_the_cutover_is_refused_when_there_is_nothing_to_judge():
    """The gap this closes: issue_verdict on an empty diff returns green, so a wave with no
    baseline and no diffs used to approve a cutover it had never tested."""
    orq, store = _orq(_model())
    out = orq.run_wave("w-blind", WaveInputs(finops_resources=TAGGED_CHEAP))

    assert out is WaveOutcome.ESCALATED
    summaries = [e.summary for e in store.decisions("w-blind")]
    assert any("cutover cannot be judged" in s for s in summaries)
    assert store.get_wave("w-blind").status is not WaveStatus.DONE
    # and it never reached the cutover step
    assert "cutover" not in store.get_wave("w-blind").completed_steps


def test_the_engineer_is_asked_only_when_nothing_could_be_derived():
    """Asking is the fallback, not the first move. When no application answers on a public address
    there is nothing to derive, and only then is a person the answer."""
    orq, store = _orq(_model())
    orq.dispatcher.register("discover_probe_targets", lambda ctx: {"candidates": []})
    orq.run_wave("w-ask", WaveInputs(finops_resources=TAGGED_CHEAP))
    hitl = [e for e in store.decisions("w-ask") if e.kind == "hitl"]
    assert any("no application in this wave answered on a public address" in e.summary
               for e in hitl)
    # the task itself still carries what a person has to supply, references and not credentials
    assert any("Secrets Manager" in (e.detail or {}).get("needed", "") for e in hitl)


def test_the_targets_are_derived_from_the_estate_rather_than_asked_for():
    """An MGN application groups source servers, a source server carries the instance it registered
    from, EC2 knows its public address. Asking for URLs the agent can derive is a gate that stalls
    on nothing."""
    probed = {}
    orq, store = _orq(_model())
    orq.dispatcher.register("discover_probe_targets", lambda ctx: {"candidates": [
        {"name": "app_contoso_catalog", "url": "http://198.51.100.7/", "expect_status": 200,
         "instance": "i-1", "host": "catalog-01", "status": 200}],
        "not_serving": ["nfs-01"]})
    orq.dispatcher.register("probe_apps", lambda ctx: probed.update(ctx.params) or {
        "probed": 1, "healthy": 1,
        "results": [{"name": "app_contoso_catalog", "ok": True, "status": 200, "fingerprint": "a"}]})

    orq.run_wave("w-derive", WaveInputs(finops_resources=TAGGED_CHEAP))

    # it probed what it derived, and it did not ask
    assert probed["apps"] == [{"name": "app_contoso_catalog", "url": "http://198.51.100.7/",
                               "expect_status": 200}]
    assert not [e for e in store.decisions("w-derive")
                if (e.detail or {}).get("gate") == "app_probe_spec"
                and (e.detail or {}).get("source") != "derived"]


def test_a_derived_spec_is_recorded_as_derived():
    """A person reading the record has to be able to see that nobody supplied these."""
    orq, store = _orq(_model())
    orq.dispatcher.register("discover_probe_targets", lambda ctx: {"candidates": [
        {"name": "app", "url": "http://198.51.100.7/", "expect_status": 200}], "sides": ["source"]})
    orq.dispatcher.register("probe_apps", lambda ctx: {
        "probed": 1, "healthy": 1,
        "results": [{"name": "app", "ok": True, "status": 200, "fingerprint": "a"}]})

    orq.run_wave("w-mark", WaveInputs(finops_resources=TAGGED_CHEAP))

    marks = [e for e in store.decisions("w-mark") if (e.detail or {}).get("source") == "derived"]
    assert marks and "resolved where to sanity-check" in marks[0].summary


def test_the_estate_decides_where_and_the_engineer_decides_what_healthy_means():
    """The engineer's address used to win. That is what made the gate vacuous: a pinned source IP
    survived into the after-probe, which then compared the source with itself. The estate now
    decides *where* an application answers, and the engineer's knowledge - the text a healthy page
    contains, a login reference - rides along."""
    probed = {}
    orq, _ = _orq(_model())
    orq.dispatcher.register("discover_probe_targets", lambda ctx: {"candidates": [
        {"name": "billing", "url": "http://198.51.100.7/", "expect_status": 200,
         "side": "source"}], "sides": ["source"]})
    orq.dispatcher.register("probe_apps", lambda ctx: probed.update(ctx.params) or {
        "probed": 1, "healthy": 1,
        "results": [{"name": "billing", "ok": True, "status": 200, "fingerprint": "a"}]})

    orq.run_wave("w-given", WaveInputs(
        finops_resources=TAGGED_CHEAP,
        app_probes=[{"name": "billing", "url": "http://203.0.113.9/stale",
                     "expect_contains": "Invoices", "secret_arn": "arn:aws:secretsmanager:x"}]))

    app = probed["apps"][0]
    assert app["url"] == "http://198.51.100.7/"          # resolved, not the pinned one
    assert app["expect_contains"] == "Invoices"          # what only the engineer knows
    assert app["secret_arn"] == "arn:aws:secretsmanager:x"


def test_an_engineers_entry_is_all_there_is_when_nothing_resolves():
    """A private-only estate answers on no public address. Then the engineer's own entry, address
    included, is the only thing to go on."""
    probed = {}
    orq, _ = _orq(_model())
    orq.dispatcher.register("discover_probe_targets", lambda ctx: {"candidates": []})
    orq.dispatcher.register("probe_apps", lambda ctx: probed.update(ctx.params) or {
        "probed": 1, "healthy": 1,
        "results": [{"name": "given", "ok": True, "status": 200, "fingerprint": "a"}]})

    orq.run_wave("w-fallback", WaveInputs(
        finops_resources=TAGGED_CHEAP,
        app_probes=[{"name": "given", "url": "http://203.0.113.9/health"}]))

    assert probed["apps"] == [{"name": "given", "url": "http://203.0.113.9/health"}]


def test_a_verdict_is_refused_when_every_application_still_answers_on_its_source():
    """The after-probe would compare the source with itself, come back identical, and hand the
    cutover a green verdict the migration never earned."""
    orq, store = _orq(_model())
    orq.dispatcher.register("discover_probe_targets", lambda ctx: {"candidates": [
        {"name": "app", "url": "http://198.51.100.7/", "expect_status": 200,
         "side": "source"}], "sides": ["source"]})
    orq.dispatcher.register("probe_apps", lambda ctx: {
        "probed": 1, "healthy": 1,
        "results": [{"name": "app", "ok": True, "status": 200, "fingerprint": "a"}]})

    out = orq.run_wave("w-source-only", WaveInputs(finops_resources=TAGGED_CHEAP))

    assert out == WaveOutcome.ESCALATED
    assert any("compare the source with itself" in (e.detail or {}).get("hypothesis", "")
               for e in store.decisions("w-source-only"))


def test_a_migrated_instance_makes_the_comparison_real():
    seen = []
    orq, _ = _orq(_model())
    sides = iter(["source", "migrated"])

    def _discover(ctx):
        side = next(sides, "migrated")
        return {"candidates": [{"name": "app", "expect_status": 200, "side": side,
                                "url": f"http://198.51.100.{'7' if side == 'source' else '8'}/"}],
                "sides": [side]}

    orq.dispatcher.register("discover_probe_targets", _discover)
    orq.dispatcher.register("probe_apps", lambda ctx: seen.append(ctx.params["apps"][0]["url"]) or {
        "probed": 1, "healthy": 1,
        "results": [{"name": "app", "ok": True, "status": 200,
                     "fingerprint": "a" if len(seen) == 1 else "b"}]})

    orq.run_wave("w-real", WaveInputs(finops_resources=TAGGED_CHEAP))

    assert seen == ["http://198.51.100.7/", "http://198.51.100.8/"]


def test_a_broken_app_after_migration_is_a_real_diff_not_an_empty_one():
    healthy = {"probed": 2, "healthy": 2, "results": [
        {"name": "billing", "ok": True, "status": 200, "fingerprint": "aaa"},
        {"name": "catalog", "ok": True, "status": 200, "fingerprint": "bbb"}]}
    broken = {"probed": 2, "healthy": 1, "results": [
        {"name": "billing", "ok": True, "status": 200, "fingerprint": "aaa"},
        {"name": "catalog", "ok": False, "status": 502, "error": "Bad Gateway"}]}

    orq, store = _orq(_model(verdict="red"))
    calls = {"n": 0}

    def _probe(ctx):
        calls["n"] += 1
        return healthy if calls["n"] == 1 else broken

    orq.dispatcher.register("probe_apps", _probe)
    out = orq.run_wave("w-broken", WaveInputs(finops_resources=TAGGED_CHEAP, app_probes=PROBES))

    # the QA agent saw a real regression and rolled back
    assert out is WaveOutcome.ROLLED_BACK
    diffs = [(e.detail or {}).get("app_diffs") for e in store.decisions("w-broken")]
    diffs = [x for x in diffs if x]
    assert diffs and "catalog" in diffs[0]
    assert diffs[0]["catalog"]["now"]["status"] == 502


def test_the_baseline_survives_a_resumed_wave():
    """A resumed wave must compare against the original observation, not re-measure a system that
    has already been migrated."""
    orq, _ = _orq(_model())
    orq.run_wave("w-resume", WaveInputs(finops_resources=TAGGED_CHEAP, app_probes=PROBES))
    first = orq._baseline("w-resume")
    orq.run_wave("w-resume", WaveInputs(finops_resources=TAGGED_CHEAP, app_probes=PROBES))
    assert orq._baseline("w-resume") == first
