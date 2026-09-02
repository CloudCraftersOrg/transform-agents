from __future__ import annotations

import os

from dispatcher.steps import Dispatcher, DynamoDbLedger, Ledger, StepContext, StepRejected
from tools import aws
from tools.policy import Action
from tools.spec import DecisionContract

# Real deterministic wave steps. Each reads `ctx.params` and calls one AWS API. Kept thin on
# purpose - all judgment lives in the agents, all these do is the idempotent effect.


def _deploy_lza(ctx: StepContext, *, codepipeline=None) -> dict:
    c = aws._client("codepipeline", codepipeline)
    resp = c.start_pipeline_execution(name=ctx.params["pipeline_name"])
    return {"pipeline_execution_id": resp["pipelineExecutionId"]}


def _start_replication(ctx: StepContext, *, mgn=None) -> dict:
    c = aws._client("mgn", mgn)
    for sid in ctx.params["source_server_ids"]:
        c.start_replication(sourceServerID=sid)
    return {"replicating": ctx.params["source_server_ids"]}


def _launch_test(ctx: StepContext, *, mgn=None) -> dict:
    c = aws._client("mgn", mgn)
    job = c.start_test(sourceServerIDs=ctx.params["source_server_ids"])
    return {"job_id": job["job"]["jobID"]}


def _dns_flip(ctx: StepContext, ip_key: str, *, route53=None) -> dict:
    c = aws._client("route53", route53)
    resp = c.change_resource_record_sets(
        HostedZoneId=ctx.params["hosted_zone_id"],
        ChangeBatch={
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": ctx.params["record_name"],
                        "Type": "A",
                        "TTL": ctx.params.get("ttl", 60),
                        "ResourceRecords": [{"Value": ctx.params[ip_key]}],
                    },
                }
            ]
        },
    )
    return {"change_id": resp["ChangeInfo"]["Id"], "pointed_at": ctx.params[ip_key]}


def _cutover(ctx: StepContext, *, route53=None, mgn=None) -> dict:
    dns = _dns_flip(ctx, "target_ip", route53=route53)
    aws._client("mgn", mgn).start_cutover(sourceServerIDs=ctx.params["source_server_ids"])
    return {"dns": dns, "cutover": ctx.params["source_server_ids"]}


def _rollback(ctx: StepContext, *, route53=None) -> dict:
    # flip DNS back to source; MGN replication stays alive on purpose, so the wave is reversible
    return {"dns": _dns_flip(ctx, "source_ip", route53=route53), "replication": "kept alive"}


def _finalize(ctx: StepContext, *, mgn=None) -> dict:
    c = aws._client("mgn", mgn)
    c.finalize_cutover(sourceServerIDs=ctx.params["source_server_ids"])
    return {"finalized": ctx.params["source_server_ids"]}


def build_lambda_dispatcher(
    *, ledger: Ledger | None = None, mgn=None, route53=None, codepipeline=None
) -> Dispatcher:
    d = Dispatcher(ledger=ledger)
    d.register("deploy_lza", lambda ctx: _deploy_lza(ctx, codepipeline=codepipeline))
    d.register("start_replication", lambda ctx: _start_replication(ctx, mgn=mgn))
    d.register("launch_test", lambda ctx: _launch_test(ctx, mgn=mgn))
    d.register("cutover", lambda ctx: _cutover(ctx, route53=route53, mgn=mgn))
    d.register("rollback", lambda ctx: _rollback(ctx, route53=route53))
    d.register("finalize", lambda ctx: _finalize(ctx, mgn=mgn))
    return d


def handler(event: dict, context=None) -> dict:
    """Lambda entry. event: {wave_id, step_id, step, params, contract, guard?}.
    Re-evaluates policy server-side (the Orchestrator is not trusted), then runs the step
    idempotently by (wave_id, step_id)."""
    contract = DecisionContract.model_validate(event["contract"])
    guard = Action(**event["guard"]) if event.get("guard") else None
    ledger_table = os.environ.get("STEP_LEDGER_TABLE")
    ledger = DynamoDbLedger(ledger_table) if ledger_table else None

    dispatcher = build_lambda_dispatcher(ledger=ledger)
    ctx = StepContext(
        wave_id=event["wave_id"],
        step_id=event["step_id"],
        contract=contract,
        params=event.get("params", {}),
    )
    try:
        result = dispatcher.dispatch(event["step"], ctx, guard=guard)
    except StepRejected as e:
        # structured so the caller re-raises StepRejected cleanly instead of parsing a stack trace
        return {"ok": False, "rejected": True, "reason": str(e),
                "wave_id": ctx.wave_id, "step_id": ctx.step_id}
    return {"ok": True, "wave_id": ctx.wave_id, "step_id": ctx.step_id, "result": result}
