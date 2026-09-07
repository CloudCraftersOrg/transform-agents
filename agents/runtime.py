from __future__ import annotations

import os
from datetime import UTC, datetime

from agents.model import BedrockModel
from agents.orchestrator import WaveInputs, build_orchestrator
from dispatcher.handler import build_lambda_dispatcher
from dispatcher.steps import DynamoDbLedger, LambdaInvokingDispatcher
from state.models import DecisionLogEntry
from state.store import DynamoDbStateStore
from tools.hitl import DynamoDbHitlQueue, InMemoryHitlQueue
from tools.notify import sns_notifier
from tools.spec import DecisionContract
from tools.trace import fail, note

# The deterministic entrypoints, kept as functions rather than a server: `BedrockAgentCoreApp` in
# agents/agentic.py owns /invocations and /ping now, and delegates here for the payloads that are
# not a conversation - the scheduler's resume, a direct wave run, and the console's reads.

def _clock() -> str:
    return datetime.now(UTC).isoformat()


def _model():
    return BedrockModel(
        os.environ.get("ORCHESTRATOR_MODEL_ID", "amazon.nova-lite-v1:0"),
        region=os.environ.get("AWS_REGION", "us-east-1"),
        guardrail_id=os.environ.get("BEDROCK_GUARDRAIL_ID"),
    )


def _build(contract: DecisionContract, workspace=None):
    store = DynamoDbStateStore(
        os.environ["WAVE_STATE_TABLE"], os.environ["DECISION_LOG_TABLE"]
    )
    model = _model()
    # STEP_DISPATCHER_FUNCTION set -> invoke the separate step Lambda (name or ARN, both work).
    # Not set -> run the steps in-process (single-container mode), still policy-checked + idempotent.
    fn = os.environ.get("STEP_DISPATCHER_FUNCTION")
    if fn:
        dispatcher = LambdaInvokingDispatcher(fn)
    else:
        ledger_table = os.environ.get("STEP_LEDGER_TABLE")
        dispatcher = build_lambda_dispatcher(
            ledger=DynamoDbLedger(ledger_table) if ledger_table else None
        )
    # Durable when the table is configured: an in-memory queue dies with the invocation, so
    # nothing could answer "what is the agent waiting on" without replaying the decision log.
    hitl_table = os.environ.get("HITL_TASK_TABLE")
    hitl = (
        DynamoDbHitlQueue(hitl_table, store, _clock) if hitl_table
        else InMemoryHitlQueue(store, _clock)
    )
    return build_orchestrator(
        model, contract, store=store, dispatcher=dispatcher, hitl=hitl, clock=_clock,
        workspace=workspace, notify=sns_notifier(),
    )


PLAN_JOB = os.environ.get("TRANSFORM_PLAN_JOB", "VmwareMigration")
CONTAINER_JOB = os.environ.get("TRANSFORM_CONTAINER_JOB", "SourceCodeContainerization")

# How many in-flight waves one scheduled invocation will drive before handing back.
RESUME_BATCH = int(os.environ.get("RESUME_BATCH", "2"))


WORKSPACE_IDS = [w for w in os.environ.get("TRANSFORM_WORKSPACE_IDS", "").split(",") if w.strip()]


def _visible_workspaces(call) -> list[dict]:
    """Transform scopes workspaces to collaborators (assumed-role sessions), so a headless runtime
    sees none of a human's workspaces. TRANSFORM_WORKSPACE_IDS names them explicitly when the
    listing comes back empty; the estate inside them is still read from the service."""
    from tools.transform_mcp import TransformWorkspace

    listed = TransformWorkspace.list_workspaces(call)
    note(f"workspaces visible to this identity: {[w.get('name') for w in listed]}")
    if listed or not WORKSPACE_IDS:
        return listed
    note(f"falling back to TRANSFORM_WORKSPACE_IDS={WORKSPACE_IDS}")
    return [{"id": wid.strip(), "name": wid.strip()} for wid in WORKSPACE_IDS]


def _workspaces(call):
    """Locate the workspace that owns each job by name. Nothing about the estate is configured."""
    from tools.transform_mcp import TransformWorkspace

    for w in _visible_workspaces(call):
        ws = TransformWorkspace(call, w["id"])
        job = ws.job_by_name(PLAN_JOB)
        if job is None:
            continue
        # One workspace owns the migration. The containerization job is only driven when it lives
        # in that same workspace - the agent does not wander into someone else's engagement.
        container = ws if ws.job_by_name(CONTAINER_JOB) else None
        note(f"migration workspace: {w.get('name')} ({w['id']}) job={job.get('jobName')} "
             f"containerization_here={container is not None}")
        return ws, job, container
    return None, None, None


def _contract_key(job_id: str) -> str:
    """Waves come and go; the plan they are derived from does not. Keying the cache on the job is
    what makes a re-run reuse the same contract instead of asking the model to invent it again."""
    return f"contract#{job_id}"


def _cached_contract(store, *keys: str):
    """Deriving the contract costs a full MCP walk plus a Trust loop, and the model does not
    produce a byte-identical answer twice. Read the first one back on every later run."""
    for key in keys:
        for e in store.decisions(key):
            blob = (e.detail or {}).get("derived_contract")
            if blob:
                return DecisionContract.model_validate(blob)
    return None


def engineer_notes(store, wave_id: str) -> str:
    """What the engineer last said about this wave, in their own words. Reaches the QA verdict as
    the description the before/after diff is judged against - "the leaderboard lists teams" is
    something only a person can say, and it is a sentence rather than a set of fields."""
    if store is None or not wave_id:
        return ""
    for e in reversed(store.decisions(wave_id)):
        reply = (e.detail or {}).get("engineer_reply")
        if reply:
            return reply
    return ""


def _answered_probe_spec(store, wave_id: str) -> list[dict]:
    """One definition of "has the engineer answered yet", shared with the agent's own tool."""
    if store is None or not wave_id:
        return []
    for e in store.decisions(wave_id):
        spec = (e.detail or {}).get("app_probes")
        if spec:
            return spec
    return []


def _from_workspace(payload: dict, call, store=None):
    """Derive the contract and the wave's inputs from the live Transform workspace."""
    from agents.extraction import extract_contract
    from tools.transform_ingest import (
        migration_plan_to_business_case,
        read_migration_plan_from_mcp,
        wave_sizing,
        wave_step_params,
    )

    plan_ws, plan_job, container_ws = _workspaces(call)
    if plan_ws is None:
        raise RuntimeError(f"no Transform workspace exposes a job matching {PLAN_JOB!r}")
    pkg = read_migration_plan_from_mcp(plan_ws, plan_job["jobId"])
    key = _contract_key(plan_job["jobId"])
    contract = _cached_contract(store, payload["wave_id"], key) if store is not None else None
    if contract is not None:
        note(f"reusing the contract derived from {plan_job.get('jobName')}")
    else:
        result, contract = extract_contract(migration_plan_to_business_case(pkg), _model())
        if contract is None:
            raise RuntimeError(f"contract did not converge: {(result.errors or ['?'])[-1][:300]}")
        if store is not None:
            entry = DecisionLogEntry(
                wave_id=payload["wave_id"], ts=_clock(), actor="agent", kind="decision",
                summary=f"contract derived from {plan_job.get('jobName')}",
                detail={"derived_contract": contract.model_dump(mode="json")},
            )
            store.append_decision(entry)
            store.append_decision(entry.model_copy(update={"wave_id": key}))
    inputs = WaveInputs(
        interpret_objective=f"landing zone for {pkg.wave}: {len(pkg.servers)} servers, "
        f"{len(pkg.apps)} applications",
        step_params=wave_step_params(pkg),
        sizing=wave_sizing(pkg),
        # The engineer's sanity-check spec, read back from the decision log. Without this the
        # deterministic path ran with no probes and the test gate escalated for want of an answer
        # that had already been given - the agentic path read it and this one did not.
        app_probes=_answered_probe_spec(store, payload["wave_id"]),
        test_spec=engineer_notes(store, payload["wave_id"]),
        approved_gates=payload.get("approve") or [],
        modernization_target=payload.get("modernization_target", "container"),
        modernization_subject=pkg.servers[0]["name"] if pkg.servers else "*",
        modernization_job=CONTAINER_JOB if container_ws is not None else "",
        migration_job=plan_job.get("jobName") or PLAN_JOB,
    )
    return contract, inputs, plan_ws


ASK_SYSTEM = (
    "You are the operator interface of an autonomous AWS migration agent. Answer ONLY from the "
    "record supplied below: the wave's decision log, its decision contract, and the live state of "
    "the AWS Transform job. If the record does not contain the answer, say exactly what is missing "
    "instead of inferring it. Be concise and concrete - name the wave, the step and the reason. "
    "Never invent a server name, an instance type, a status or a number."
)


def _ask(question: str, wave_id: str, plan_ws, plan_job) -> dict:
    """Answer a question about the migration from what the agent actually recorded. The decision
    log is the ground truth; the model only puts it into words."""
    store = DynamoDbStateStore(os.environ["WAVE_STATE_TABLE"], os.environ["DECISION_LOG_TABLE"])
    jid = plan_job["jobId"]
    wave = store.get_wave(wave_id) if wave_id else None
    log = store.decisions(wave_id) if wave_id else []
    status = (plan_job.get("statusDetails") or {}).get("status", "")
    pending = plan_ws.pending_interaction(jid)
    contract = _cached_contract(store, wave_id, _contract_key(jid)) if wave_id else None

    record = [f"AWS Transform job: {plan_job.get('jobName')} status={status}"]
    if pending:
        record.append(f"The job is waiting on this question:{chr(10)}{pending.get('text', '')[:1500]}")
        record.append(f"Options it offers: {[o.get('value') for o in pending.get('options', [])]}")
    if wave is not None:
        record.append(
            f"Wave {wave.wave_id}: status={wave.status} current_step={wave.current_step} "
            f"completed={wave.completed_steps}"
        )
    if contract is not None:
        record.append(
            f"Decision contract {contract.case_id}: budget ceiling "
            f"{contract.budget.ceiling_monthly_usd} USD/month, {len(contract.constraints)} "
            f"constraints, e.g. "
            + "; ".join(f"{c.subject} {c.predicate} {c.value}" for c in contract.constraints[:6])
        )
    if log:
        record.append("Decision log, oldest first:")
        record.extend(f"- [{e.kind}] {e.summary}" for e in log[-40:])
    else:
        record.append("No decision log entries for this wave.")

    body = chr(10).join(record)
    answer = _model().complete(f"{body}{chr(10)}{chr(10)}Question: {question}", system=ASK_SYSTEM)
    note(f"answered a question about {wave_id or 'the job'}: {question[:100]}")
    return {
        "question": question,
        "answer": answer.strip(),
        "grounded_on": {
            "wave_id": wave_id,
            "decisions": len(log),
            "job_status": status,
            "has_contract": contract is not None,
            "job_is_asking": bool(pending),
        },
    }


def invoke(payload: dict) -> dict:
    """payload: {wave_id, contract?, inputs?, require_contract_approval?}. Without a contract the
    Orchestrator derives everything from the Transform workspace. Called for both the first run and
    every EventBridge re-invocation - `run_wave` is resumable, so they're the same call."""
    from tools.transform_mcp import make_transform_client, mcp_call

    # The scheduler fires with no wave in mind: it re-invokes whatever is still in flight. A wave
    # that finished, failed or escalated is left alone - escalated means a person owes an answer.
    if payload.get("action") == "resume":
        store = DynamoDbStateStore(
            os.environ["WAVE_STATE_TABLE"], os.environ["DECISION_LOG_TABLE"]
        )
        in_flight = store.waves_in_flight()
        # Bounded on purpose: one invocation has a request timeout, and each wave costs a full MCP
        # walk. Newest first, so a live wave is never starved by a pile of stale ones; the schedule
        # fires again in five minutes for the rest.
        batch = in_flight[:RESUME_BATCH]
        note(f"resume: {len(in_flight)} wave(s) in flight, taking {len(batch)} {batch}")
        resumed = {}
        for wid in batch:
            try:
                resumed[wid] = invoke({"wave_id": wid})["outcome"]
            except Exception as e:  # noqa: BLE001 - one stuck wave must not stop the others
                fail(f"resume {wid}: {type(e).__name__}: {e}")
                resumed[wid] = f"ERROR: {type(e).__name__}"
        return {"action": "resume", "in_flight": len(in_flight), "resumed": resumed}

    if payload.get("contract"):
        contract = DecisionContract.model_validate(payload["contract"])
        inputs = WaveInputs(**payload["inputs"]) if payload.get("inputs") else None
        orq = _build(contract)
        outcome = orq.run_wave(
            payload["wave_id"], inputs,
            require_contract_approval=payload.get("require_contract_approval", False),
        )
        return {"wave_id": payload["wave_id"], "outcome": str(outcome)}

    with make_transform_client() as client:
        call = mcp_call(client)

        # Read-only view of the agent's own chat thread. Sending anything is what makes a
        # conversation single-flight, so introspection must never write.
        if payload.get("read_chat"):
            plan_ws, plan_job, _ = _workspaces(call)
            jid = plan_job["jobId"]
            msgs = plan_ws.messages(jid)
            pending = plan_ws.pending_interaction(jid)
            arts = plan_ws.walk_artifacts(jid)
            return {
                "job_id": jid,
                "job_status": plan_ws.job_status(jid, detailed=True),
                # the agent's own records sort ahead of the generated outputs, so list them first
                "artifacts": sorted(
                    ((a.get("fileMetadata") or {}).get("path") or a.get("fileName") or "")
                    for a in arts
                )[:40],
                "messages": len(msgs),
                "pending_interaction": pending,
                "thread": [
                    {"origin": m.get("messageOrigin"),
                     "type": (m.get("processingInfo") or {}).get("messageType"),
                     "options": [o.get("label") or o.get("value")
                                 for i in (m.get("interactions") or [])
                                 for o in (i.get("options") or [])][:12],
                     "text": (m.get("text") or "")[:400]}
                    for m in msgs[:8]
                ],
            }

        # Relay a message into the job's chat as the agent. A Transform conversation is async and
        # single-flight: the message is posted and the reply is read on the next poll, not awaited
        # here - blocking on it is what times out the caller.
        if payload.get("say"):
            from tools.transform_mcp import TransformBusy

            plan_ws, plan_job, _ = _workspaces(call)
            jid = plan_job["jobId"]
            note(f"relaying to {plan_job.get('jobName')}: {payload['say'][:120]}")
            before = plan_ws.latest_reply(jid)
            try:
                plan_ws.send_message(jid, payload["say"], skip_polling=True)
            except TransformBusy as e:
                return {"sent": False, "busy": True, "detail": str(e)[:300]}
            return {"sent": True, "processing": True, "reply_before": before[:400]}

        if payload.get("ask"):
            plan_ws, plan_job, _ = _workspaces(call)
            return _ask(payload["ask"], payload.get("wave_id", ""), plan_ws, plan_job)

        store = DynamoDbStateStore(
            os.environ["WAVE_STATE_TABLE"], os.environ["DECISION_LOG_TABLE"]
        )
        contract, inputs, plan_ws = _from_workspace(payload, call, store)
        orq = _build(contract, workspace=plan_ws)
        outcome = orq.run_wave(
            payload["wave_id"], inputs,
            require_contract_approval=payload.get("require_contract_approval", False),
        )
        return {
            "wave_id": payload["wave_id"],
            "outcome": str(outcome),
            "contract": contract.case_id,
            "constraints": len(contract.constraints),
        }
