from __future__ import annotations

from dataclasses import dataclass

# Deterministic wrappers over AWS APIs (MGN, CloudWatch Logs, SSM). Idempotent by design, no model
# reasoning. Every function takes an optional `client` so tests inject a fake and prod stays wiring.

_REPLICATING = {"INITIATING", "INITIAL_SYNC", "RESCAN", "CONTINUOUS", "CREATING_SNAPSHOT"}
_READY_LIFECYCLE = {"READY_FOR_TEST", "READY_FOR_CUTOVER", "TESTING", "CUTTING_OVER"}
_FAILED = {"STALLED", "DISCONNECTED", "STOPPED"}


@dataclass(frozen=True)
class MgnCounts:
    total: int
    replicating: int
    ready: int
    failed: int


def _client(service: str, client=None):
    if client is not None:
        return client
    import boto3

    return boto3.client(service)


def mgn_source_servers(source_server_ids: list[str], *, client=None) -> list[dict]:
    c = _client("mgn", client)
    resp = c.describe_source_servers(filters={"sourceServerIDs": source_server_ids})
    return resp.get("items", [])


def mgn_source_server(source_server_id: str, *, client=None) -> dict | None:
    items = mgn_source_servers([source_server_id], client=client)
    return items[0] if items else None


def mgn_counts(source_server_ids: list[str], *, client=None) -> MgnCounts:
    """Aggregate MGN replication state into the digest the Orchestrator's `estado_de_ola` needs."""
    servers = mgn_source_servers(source_server_ids, client=client)
    replicating = ready = failed = 0
    for s in servers:
        repl = (s.get("dataReplicationInfo") or {}).get("dataReplicationState", "")
        life = (s.get("lifeCycle") or {}).get("state", "")
        if repl in _REPLICATING:
            replicating += 1
        if life in _READY_LIFECYCLE:
            ready += 1
        if repl in _FAILED or life == "DISCONNECTED":
            failed += 1
    return MgnCounts(total=len(servers), replicating=replicating, ready=ready, failed=failed)


def cloudwatch_logs(
    log_group: str, log_stream: str, *, limit: int = 200, client=None
) -> str:
    c = _client("logs", client)
    resp = c.get_log_events(
        logGroupName=log_group, logStreamName=log_stream, limit=limit, startFromHead=False
    )
    return "\n".join(e.get("message", "") for e in resp.get("events", []))


def ssm_run_command(
    instance_id: str,
    document: str,
    parameters: dict[str, list[str]],
    *,
    allowed_documents: list[str],
    client=None,
) -> dict:
    """The only tool that writes. `document` must be in `allowed_documents` (from
    `contract.allowed_actions`), enforced here in code - never from a prompt."""
    if document not in allowed_documents:
        raise PermissionError(f"SSM document {document!r} is not in the allow-list")
    c = _client("ssm", client)
    resp = c.send_command(
        InstanceIds=[instance_id], DocumentName=document, Parameters=parameters
    )
    return {"command_id": resp["Command"]["CommandId"], "status": resp["Command"]["Status"]}


def mgn_jobs(*, client=None, limit: int = 20) -> list[dict]:
    """Recent MGN jobs (test / cutover / launch) with per-server launch status - the real failure
    signal for a rehost. A CodePipeline wrapper only belongs here if LZA is turned on."""
    c = _client("mgn", client)
    resp = c.describe_jobs(filters={}, maxResults=limit)
    jobs = []
    for j in resp.get("items", []):
        jobs.append(
            {
                "jobID": j.get("jobID"),
                "type": j.get("type"),
                "status": j.get("status"),
                "initiatedBy": j.get("initiatedBy"),
                "servers": [
                    {"sourceServerID": p.get("sourceServerID"), "launchStatus": p.get("launchStatus")}
                    for p in j.get("participatingServers", [])
                ],
            }
        )
    return jobs
