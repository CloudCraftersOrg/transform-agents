from __future__ import annotations

import json
from typing import Any

from strands import tool

from agents.orchestrator import (
    APP_PROBE_SPEC,
    WaveInputs,
    carries_stop_signal,
    claims_unverifiable_work,
    manual_prerequisite,
)
from agents.prompt import SYSTEM_PROMPT
from tools.policy import Action
from tools.transform_mcp import TransformBusy

__all__ = ["SYSTEM_PROMPT", "build_tools"]

# The gate an open-ended question is filed under, and the only one `ask_engineer` can reach.
#
# Which human gate a question satisfies is not the model's to choose. The console renders a
# sanity-check form for `app_probe_spec` and a proceed/refuse decision for the cutover gate, so a
# question filed under the wrong one is answered with the wrong instrument - and a model that can
# name any gate can mark any of them as merely asked. The wave opens `app_probe_spec` itself, from
# the step that needs it.
ENGINEER_QUESTION = "engineer_question"

# The tool surface the Orchestrator agent reasons over. Two rules shape it:
#
#   1. Every guard lives here, inside the tool that performs the effect - never in the order the
#      agent happens to call things. An agent picking its own sequence must not be able to reach a
#      destructive action by not mentioning the check that precedes it.
#   2. `run_migration_wave` stays a tool rather than being dissolved into the loop. It carries the
#      state machine and the resume point the scheduler depends on; the agent decides *when* to run
#      a wave, not how a wave is sequenced internally.



def build_tools(orq, workspace=None, plan_job: dict | None = None) -> list:
    """Bind the tool surface to one orchestrator, workspace and Transform job."""
    jid = (plan_job or {}).get("jobId", "")
    jname = (plan_job or {}).get("jobName", "")

    @tool
    def migration_status(wave_id: str) -> str:
        """Where the migration actually stands: the wave's state machine position, the AWS
        Transform job status and what it is waiting on, and a replication summary from MGN."""
        wave = orq.store.get_wave(wave_id)
        out: dict[str, Any] = {
            "wave": {
                "id": wave_id,
                "status": str(wave.status) if wave else "UNKNOWN",
                "current_step": getattr(wave, "current_step", None),
                "completed": getattr(wave, "completed_steps", []),
            },
            "recent_decisions": [
                {"kind": e.kind, "summary": e.summary} for e in orq.store.decisions(wave_id)[-8:]
            ],
        }
        if workspace is not None and jid:
            try:
                pending = workspace.pending_interaction(jid)
                out["transform"] = {
                    "job": jname,
                    "asking": (pending or {}).get("text", "")[:600] or None,
                    "options": [o.get("value") for o in (pending or {}).get("options", [])],
                }
            except Exception as e:  # noqa: BLE001 - a read failure is information, not a crash
                out["transform"] = {"error": f"{type(e).__name__}: {e}"[:200]}
        out["mgn"] = _replication_summary(orq)
        return json.dumps(out, default=str)

    @tool
    def waves_in_flight() -> str:
        """Every migration wave still running, newest first. Start here when you were woken up
        with no wave in mind - you cannot resume what you have not enumerated."""
        ids = orq.store.waves_in_flight()
        return json.dumps({
            "in_flight": ids,
            "note": "none in flight means there is nothing to resume; do not invent a wave id",
        })

    @tool
    def mgn_replication_health() -> str:
        """Replication state straight from AWS Application Migration Service, per source server:
        which are replicating, which are stalled and with what error. This is the ground truth when
        AWS Transform's own progress report disagrees with reality."""
        return json.dumps(_replication_summary(orq, detailed=True), default=str)

    @tool
    def run_migration_wave(wave_id: str, approve_gates: str = "") -> str:
        """Run or resume a full migration wave. Deterministic and resumable: it walks the wave's
        steps, enforces the decision contract, and returns WAITING when a long wait or a human gate
        is reached. Call it again later to continue. `approve_gates` is a comma-separated list of
        human approvals to apply on this run (for example proceed_wave_cutover)."""
        gates = [g.strip() for g in approve_gates.split(",") if g.strip()]
        inputs = WaveInputs(
            migration_job=jname, approved_gates=gates,
            app_probes=_probe_spec(orq, wave_id),
        )
        outcome = orq.run_wave(wave_id, inputs)
        return json.dumps({"wave_id": wave_id, "outcome": str(outcome),
                           "approved": gates}, default=str)

    @tool
    def sanity_check_apps(wave_id: str) -> str:
        """Probe the applications this wave moves and report which are healthy. Run before the
        migration it becomes the baseline; run after, it is what the cutover is judged against.
        Needs the probe spec from the engineer first - use ask_engineer if it is missing."""
        spec = _probe_spec(orq, wave_id)
        if not spec:
            return json.dumps({
                "probed": 0,
                "missing": f"no {APP_PROBE_SPEC} yet - ask the engineer for a URL per application, "
                           "the expected response, and a Secrets Manager ARN for any login",
            })
        result = orq.dispatch(
            wave_id, f"probe-{orq.clock()[:16]}", "probe_apps",
            guard=Action("dispatch_step", step="probe_apps"), params={"apps": spec},
        )
        return json.dumps(result, default=str)

    @tool
    def answer_transform(wave_id: str, message: str) -> str:
        """Reply to AWS Transform in the migration job's conversation. Refuses to send anything
        that would accept a destructive, irreversible or safety-skipping option, or that would
        claim a prerequisite only a person can satisfy has been done."""
        if workspace is None or not jid:
            return json.dumps({"sent": False, "reason": "no AWS Transform job is wired"})
        signal = carries_stop_signal(message)
        if signal:
            return json.dumps({
                "sent": False,
                "refused": f"that message accepts a destructive, irreversible or safety-skipping "
                           f"action ({signal!r}); a person has to decide it",
            })
        claim = claims_unverifiable_work(message)
        if claim:
            return json.dumps({
                "sent": False,
                "refused": f"that message claims work you cannot do ({claim!r}). You have no tool "
                           "that installs an agent, tags a resource or configures a machine, so "
                           "saying it happened would be false and AWS Transform would act on it. "
                           "Ask the engineer, or escalate.",
            })
        pending = workspace.pending_interaction(jid) or {}
        need = manual_prerequisite(pending.get("text") or "")
        if need:
            return json.dumps({
                "sent": False,
                "refused": f"AWS Transform is waiting on a manual step first: {need}. "
                           "Replying would claim work that has not been done.",
            })
        try:
            workspace.send_message(jid, message, skip_polling=True)
        except TransformBusy as e:
            return json.dumps({"sent": False, "busy": True, "detail": str(e)[:200]})
        orq._log(wave_id, "decision", f"{jname}: told AWS Transform {message[:120]!r}",
                 {"job_id": jid})
        return json.dumps({"sent": True, "note": "AWS Transform replies asynchronously; read it "
                                                 "back with migration_status"})

    @tool
    def diagnose_and_remediate(wave_id: str, failure: str, host: str = "") -> str:
        """Hand a failure to the Remediation specialist, which diagnoses it and applies a fix
        through the policy-checked dispatcher. Use it when something is stuck and you have already
        read the actual state. `host` is the hostname as `mgn_replication_health` reports it - the
        tool resolves it to the MGN source server itself, so you never carry an opaque id."""
        target = {"wave_id": wave_id}
        if host:
            resolved = _source_server_for(orq, host)
            if resolved:
                target["source_server_id"] = resolved
            else:
                return json.dumps({
                    "resolved": False,
                    "detail": f"{host!r} is not a source server MGN knows about; check the name "
                              "against mgn_replication_health before retrying",
                })
        result = orq.delegate(
            "remediation", json.dumps({"failure": failure, "target": target}), wave_id=wave_id
        )
        resolved = getattr(result, "resolved", False)
        orq._log(
            wave_id, "remediation_outcome",
            f"remediation {'resolved' if resolved else 'did not resolve'}: {failure[:120]}",
            {"resolved": resolved, "from_runbook": getattr(result, "from_runbook", False)},
        )
        return json.dumps({
            "resolved": resolved,
            "action": getattr(result, "action", None),
            "from_runbook": getattr(result, "from_runbook", False),
            "attempts": getattr(result, "attempts", None),
            "actions": [str(a) for a in getattr(result, "actions", [])][:5],
        }, default=str)

    @tool
    def ask_engineer(wave_id: str, question: str) -> str:
        """Ask the engineer for something only they know - application URLs, what a healthy
        response looks like, a Secrets Manager ARN for a login. Never ask for a credential itself:
        the answer is persisted in the decision log and rendered in the console."""
        payload = {"question": question}
        before = orq.hitl.pending()
        orq.hitl.submit(wave_id, ENGINEER_QUESTION, payload)
        # The queue returns the open task when the same question is asked again, so the ask is
        # idempotent - but the log was not, and a scheduled agent that re-asks every five minutes
        # wrote a line each time. A record of asking something nobody was asked is noise.
        if len(orq.hitl.pending()) == len(before):
            return json.dumps({"asked": False, "already_open": True, "gate": ENGINEER_QUESTION,
                               "note": "you already asked this and it has not been answered; "
                                       "do something else or stop"})
        orq._log(wave_id, "hitl", f"asked the engineer: {question[:200]}",
                 {"gate": ENGINEER_QUESTION})
        return json.dumps({"asked": True, "gate": ENGINEER_QUESTION,
                           "note": "the wave continues; the answer arrives on a later run"})

    @tool
    def escalate(wave_id: str, context: str, hypothesis: str) -> str:
        """Hand the migration to a person with a diagnosis. Use it when your tools cannot clear the
        blocker. This is a correct outcome, not a failure - but say precisely what is blocked and
        what you already tried."""
        orq.escalate(wave_id, context=context, hypothesis=hypothesis, attempts=1)
        return json.dumps({"escalated": True, "context": context[:300]})

    return [
        waves_in_flight, migration_status, mgn_replication_health, run_migration_wave,
        sanity_check_apps, answer_transform, diagnose_and_remediate, ask_engineer, escalate,
    ]


def _source_server_for(orq, host: str) -> str | None:
    """Hostname -> MGN source server id. The model reasons in hostnames because that is what the
    status tools report; carrying an opaque id between calls is a reliable way to lose it."""
    summary = _replication_summary(orq, detailed=True)
    for s in summary.get("servers") or []:
        if (s.get("host") or "").lower() == host.lower():
            return s.get("source_server_id")
    return None


def _probe_spec(orq, wave_id: str) -> list[dict]:
    """The engineer's answer, read back from the decision log so it survives re-invocations."""
    from agents.runtime import _answered_probe_spec

    return _answered_probe_spec(orq.store, wave_id)


def _replication_summary(orq, detailed: bool = False) -> dict:
    """MGN's own view. Read through the dispatcher, so the Orchestrator keeps holding no AWS API."""
    try:
        result = orq.dispatch(
            "-", f"mgn-health-{orq.clock()[:16]}", "mgn_status",
            guard=Action("dispatch_step", step="mgn_status"),
        )
    except Exception as e:  # noqa: BLE001 - a read failure is information
        return {"error": f"{type(e).__name__}: {e}"[:200]}
    if not detailed:
        result.pop("servers", None)
    return result
