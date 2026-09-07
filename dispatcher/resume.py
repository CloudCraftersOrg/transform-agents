from __future__ import annotations

import json
import os
from datetime import UTC, datetime

# EventBridge Scheduler cannot reach AgentCore on its own: the universal target
# `aws-sdk:bedrockagentcore:invokeAgentRuntime` is accepted by Terraform (it is just a string) but
# the invocation never arrives - the schedule fires, the role is correct, and no request reaches the
# runtime. This Lambda is a native scheduler target, and its only job is to make that one call.
#
# It ships in the same zip as the step dispatcher with a different handler. That is deliberate: the
# separation that matters is the IAM role, not the artifact. This one can wake the agent and nothing
# else; the dispatcher can mutate MGN and Route 53 and cannot wake the agent.
#
# HARNESS_ARN set -> InvokeHarness, and the agent decides what to do with the tick. Otherwise the
# deterministic runtime, which is handed the resume action and walks the in-flight waves itself.

TIMEOUT_NOTE = (
    "the agent drives as many waves as it can in one turn, so this Lambda's timeout has to exceed "
    "the harness's own timeoutSeconds, not just the network round trip"
)

# Ticks share a session so the agent remembers what it already tried. That memory is about its own
# actions, never about the state of the world: between two ticks a wave can be un-escalated, a
# replication can finish, a person can answer. An agent that recalls "nothing was in flight" and
# skips the read will sleep through the migration restarting, which is exactly what happened once.
RESUME_PROMPT = (
    "You have been woken up on a schedule. Nobody is watching. "
    "Call `waves_in_flight` now. Its answer five minutes ago says nothing about now - waves are "
    "started, finished and un-escalated between ticks, so never answer this from memory, however "
    "many times you have already checked. "
    "Then, for every wave it names, call `run_migration_wave`. That is the only tool that moves a "
    "wave: `migration_status` and `mgn_replication_health` only read, and a turn that ends having "
    "only read has done nothing. Reading first is right; stopping there is not. "
    "Answer whatever AWS Transform is waiting on. If replication is stalled, diagnose it and "
    "remediate rather than reporting it. Ask the engineer only for what nobody but a person can "
    "supply, and escalate only when your tools genuinely cannot clear the blocker. Use what you "
    "remember to avoid repeating a fix that did not work, not to avoid looking. "
    "If the tool says nothing is in flight, say so and stop."
)


def _session_id() -> str:
    """Stable within a day, so consecutive ticks share the harness's memory and the agent knows
    what it already tried. Rotated daily so one session does not grow without bound. AgentCore
    rejects a session id shorter than 33 characters, silently."""
    return f"transform-agents-resume-{datetime.now(UTC).date().isoformat()}"


def handler(event: dict | None = None, context=None) -> dict:
    """Poke the agent to resume whatever waves are still in flight."""
    harness = os.environ.get("HARNESS_ARN")
    result = _via_harness(harness) if harness else _via_runtime()

    # Logged rather than swallowed: a scheduled run nobody watches is only useful if its outcome
    # reaches CloudWatch.
    print(f"AGENT    scheduler                -                  resume -> "
          f"{json.dumps(result, default=str)[:900]}", flush=True)
    return result


def _via_harness(arn: str) -> dict:
    from agents.harness import invoke

    try:
        turn = invoke(RESUME_PROMPT, _session_id(), harness_arn=arn)
    except Exception as e:  # noqa: BLE001 - a failed tick must not kill the schedule
        return {"ok": False, "via": "harness", "error": f"{type(e).__name__}: {e}"[:600]}
    return {"ok": not turn["errors"], "via": "harness", **turn}


def _via_runtime() -> dict:
    import boto3

    arn = os.environ.get("AGENT_RUNTIME_ARN")
    if not arn:
        return {"ok": False, "error": "neither HARNESS_ARN nor AGENT_RUNTIME_ARN is set"}

    resp = boto3.client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=arn,
        payload=json.dumps({"action": "resume"}).encode(),
        contentType="application/json",
        accept="application/json",
    )
    raw = resp["response"].read()
    try:
        body = json.loads(raw)
    except ValueError:
        body = {"raw": raw.decode(errors="replace")[:2000]}
    return {"ok": True, "via": "runtime", "status": resp.get("statusCode"), "result": body}
