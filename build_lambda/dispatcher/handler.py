from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

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


def _staging_subnet(ec2) -> str:
    subnets = ec2.describe_subnets()["Subnets"]
    if not subnets:
        raise StepRejected("no subnet available for the MGN staging area")
    # a public subnet lets the replication servers reach the MGN endpoint without a NAT gateway
    return next((s for s in subnets if s.get("MapPublicIpOnLaunch")), subnets[0])["SubnetId"]


def _default_security_group(ec2, subnet_id: str) -> str:
    vpc = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]["VpcId"]
    groups = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [vpc]}, {"Name": "group-name", "Values": ["default"]},
    ])["SecurityGroups"]
    if not groups:
        raise StepRejected(f"no default security group in {vpc}")
    return groups[0]["GroupId"]


def _create_template(*, _mgn, _observed, _subnet, **kw) -> dict:
    """MGN answers a refused template with an empty AccessDenied, so the response metadata is the
    only thing that says why. Carrying it into the error is what makes the escalation actionable."""
    try:
        return _mgn.create_replication_configuration_template(**kw)
    except Exception as e:
        resp = getattr(e, "response", {}) or {}
        raise RuntimeError(
            f"MGN refused the replication template (subnet {_subnet}, before: {_observed}): "
            f"{type(e).__name__} {resp.get('Error')} {resp.get('ResponseMetadata', {}).get('RequestId')}"
        ) from e


def _initialize_mgn(ctx: StepContext, *, mgn=None, ec2=None) -> dict:
    """MGN keeps reporting an account as uninitialized until a replication template exists, so
    InitializeService on its own never unblocks a wave. Re-running this is safe: an existing
    template is reused rather than replaced."""
    c = aws._client("mgn", mgn)
    c.initialize_service()
    try:
        existing = c.describe_replication_configuration_templates().get("items", [])
    except Exception as e:  # noqa: BLE001 - UninitializedAccountException until the first one lands
        existing, observed = [], f"{type(e).__name__}: {e}"
    else:
        observed = f"{len(existing)} template(s)"
    if existing:
        return {"template_id": existing[0]["replicationConfigurationTemplateID"], "created": False}

    e2 = aws._client("ec2", ec2)
    subnet = ctx.params.get("staging_subnet_id") or _staging_subnet(e2)
    groups = ctx.params.get("staging_security_group_ids") or [_default_security_group(e2, subnet)]
    tpl = _create_template(
        associateDefaultSecurityGroup=True,
        bandwidthThrottling=0,
        createPublicIP=False,
        dataPlaneRouting="PRIVATE_IP",
        defaultLargeStagingDiskType="GP3",
        ebsEncryption="DEFAULT",
        # A shared replication server carries every source server in the wave at once. Burstable
        # types are the wrong shape for that: the credit balance drains during initial sync, the
        # instance drops to baseline, and agents start failing to connect. Non-burstable by default.
        replicationServerInstanceType=ctx.params.get("replication_server_type", "m5.large"),
        replicationServersSecurityGroupsIDs=groups,
        stagingAreaSubnetId=subnet,
        stagingAreaTags={"Name": f"{ctx.wave_id}-mgn-staging"},
        useDedicatedReplicationServer=False,
        _mgn=c, _observed=observed, _subnet=subnet,
    )
    return {"template_id": tpl["replicationConfigurationTemplateID"], "created": True,
            "staging_subnet_id": subnet, "before": observed}


# Burstable families earn CPU credits when idle and spend them under load. A replication server
# runs flat out for hours, so it drains to zero and is throttled to baseline for the rest of the
# initial sync - which is when agents start failing to connect.
BURSTABLE_PREFIXES = ("t2.", "t3.", "t3a.", "t4g.")

# How much of a response body is read for the fingerprint. The body itself is never returned.
PROBE_BODY_LIMIT = 64 * 1024

# Three symptoms of one cause: a shared replication server that cannot keep up. Pairing is the
# handshake before the connection, so an overloaded server fails at whichever the agent reached
# first; NOT_CONVERGING is the same server losing the race against the source's write rate once
# replication is already running.
SATURATION_SYMPTOMS = (
    "FAILED_TO_CONNECT_AGENT_TO_REPLICATION_SERVER",
    "FAILED_TO_PAIR_REPLICATION_SERVER_WITH_AGENT",
    "NOT_CONVERGING",
)


def _servers_the_server_cannot_serve(mgn) -> list[str]:
    """Source servers whose replication is failing in a way an undersized shared
    replication server explains: cannot connect, cannot pair, or cannot keep up."""
    out: list[str] = []
    token = None
    while True:
        kw = {"maxResults": 100}
        if token:
            kw["nextToken"] = token
        page = mgn.describe_source_servers(**kw)
        for s in page.get("items", []):
            info = s.get("dataReplicationInfo") or {}
            if (info.get("dataReplicationError") or {}).get("error") in SATURATION_SYMPTOMS:
                hints = (s.get("sourceProperties") or {}).get("identificationHints") or {}
                out.append(hints.get("hostname") or s.get("sourceServerID", "?"))
        token = page.get("nextToken")
        if not token:
            return out


# A VMware import creates one MGN source server per VM in the inventory file. When the agents are
# then installed, they register as *new* source servers with their own identity instead of attaching
# to those records. The wave keeps tracking the placeholders, which will never get an agent, while
# the machines that actually replicated sit outside every application - so AWS Transform counts
# "0 of 12 agents detected" with twelve servers finished beside it.
PLACEHOLDER_TAG = "AWSTransform"


def _reconcile_wave_inventory(ctx: StepContext, *, mgn=None) -> dict:
    """Put the source servers that actually replicated into the applications the wave tracks, and
    take the placeholders out. Refuses anything it cannot prove.

    The guards are the point, because this rewrites what the wave is:
      - a server is only added where its own twin already sits, matched on hostname
      - only a record MGN never saw an agent on, tagged as service-created, is removed
      - a replicating server is never removed, whatever its tags say
      - an application is never emptied, and an ambiguous match is refused rather than guessed
    """
    c = aws._client("mgn", mgn)
    servers = _all_source_servers(c)
    placeholders = [s for s in servers if _is_placeholder(s)]
    live = {_hostname(s).lower(): s for s in servers if not _is_placeholder(s)}
    if not any(p.get("applicationID") for p in placeholders):
        return {"reconciled": False,
                "reason": "no service-created placeholder is left in an application"}

    add: dict[str, list[str]] = {}
    drop: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for p in placeholders:
        app = p.get("applicationID")
        if app is None:
            continue  # already taken out of the wave by an earlier run
        host = (p.get("tags") or {}).get("hostname") or p.get("userProvidedID") or ""
        twin = _twin(host, live)
        if twin is None:
            unmatched.append(host or p["sourceServerID"])
            continue
        # MGN answers AccessDenied - not a conflict - when a server is associated twice, so a
        # half-applied run could never be finished by repeating it. The delta is against what the
        # application holds right now, which also makes the step safe to re-run.
        if twin.get("applicationID") != app:
            add.setdefault(app, []).append(twin["sourceServerID"])
        drop.setdefault(app, []).append(p["sourceServerID"])

    if unmatched:
        return {"reconciled": False, "unmatched": unmatched,
                "reason": "every placeholder must map to exactly one replicating server before "
                          "the wave is rewritten; these do not, so nothing was changed"}

    if ctx.params.get("dry_run"):
        return {"reconciled": False, "dry_run": True, "would_add": add, "would_remove": drop}

    # Added first: between the two calls an application is over-full, never empty.
    for app, ids in add.items():
        if ids:
            c.associate_source_servers(applicationID=app, sourceServerIDs=ids)
    for app, ids in drop.items():
        c.disassociate_source_servers(applicationID=app, sourceServerIDs=ids)
    return {"reconciled": True, "added": add, "removed": drop,
            "note": "the wave now tracks the servers that replicated; AWS Transform recounts on "
                    "its next status check"}


def _all_source_servers(mgn) -> list[dict]:
    out, token = [], None
    while True:
        kw = {"maxResults": 100}
        if token:
            kw["nextToken"] = token
        page = mgn.describe_source_servers(**kw)
        out.extend(page.get("items", []))
        token = page.get("nextToken")
        if not token:
            return out


def _is_placeholder(s: dict) -> bool:
    """A record the service invented and no agent ever reached. The lifecycle test is not
    redundant with the tag: a real server can carry the same tag once the wave adopts it, and
    removing one that is replicating would drop a finished machine out of the migration."""
    return ((s.get("lifeCycle") or {}).get("state") == "PENDING_INSTALLATION"
            and (s.get("tags") or {}).get("CreatedBy") == PLACEHOLDER_TAG
            and not (s.get("dataReplicationInfo") or {}).get("dataReplicationState"))


def _hostname(s: dict) -> str:
    hints = (s.get("sourceProperties") or {}).get("identificationHints") or {}
    return hints.get("hostname") or s.get("userProvidedID") or s.get("sourceServerID") or ""


def _twin(host: str, live: dict) -> dict | None:
    """The replicating server a placeholder stands for. An exact hostname wins; otherwise the
    short name, because the two sides disagree on the suffix - the import records
    `EC2AMAZ-K9LQ97Q.WORKGROUP` where the agent reports `EC2AMAZ-K9LQ97Q`. Two candidates for one
    short name is an ambiguity, and ambiguity is refused."""
    host = host.lower()
    if host in live:
        return live[host]
    short = host.split(".")[0]
    if not short:
        return None
    hits = [s for k, s in live.items() if k.split(".")[0] == short]
    return hits[0] if len(hits) == 1 else None


def _mgn_status(ctx: StepContext, *, mgn=None) -> dict:
    """What MGN itself reports, per source server. AWS Transform narrates its own progress; this is
    the infrastructure's answer, and the two do disagree."""
    c = aws._client("mgn", mgn)
    servers, token = [], None
    while True:
        kw = {"maxResults": 100}
        if token:
            kw["nextToken"] = token
        page = c.describe_source_servers(**kw)
        for s in page.get("items", []):
            info = s.get("dataReplicationInfo") or {}
            hints = (s.get("sourceProperties") or {}).get("identificationHints") or {}
            disks = info.get("replicatedDisks") or []
            total = sum(d.get("totalStorageBytes") or 0 for d in disks)
            done = sum(d.get("replicatedStorageBytes") or 0 for d in disks)
            servers.append({
                "host": hints.get("hostname") or s.get("sourceServerID"),
                "source_server_id": s.get("sourceServerID"),
                "state": info.get("dataReplicationState"),
                "lifecycle": (s.get("lifeCycle") or {}).get("state"),
                "percent": round(done / total * 100, 1) if total else None,
                "error": (info.get("dataReplicationError") or {}).get("error"),
                "eta": info.get("etaDateTime"),
            })
        token = page.get("nextToken")
        if not token:
            break

    by_state: dict[str, int] = {}
    for s in servers:
        by_state[s["state"] or s["lifecycle"] or "UNKNOWN"] = (
            by_state.get(s["state"] or s["lifecycle"] or "UNKNOWN", 0) + 1
        )
    stalled = [s for s in servers if s.get("error")]
    return {
        "total": len(servers),
        "by_state": by_state,
        "stalled": [{"host": s["host"], "error": s["error"]} for s in stalled],
        "servers": servers,
    }


def _resize_replication_server(ctx: StepContext, *, mgn=None) -> dict:
    """One shared replication server carries the whole wave. On a burstable type its CPU credits
    drain mid-sync and agents start failing to connect. Moves the template to a non-burstable type,
    but only when that exact signature is present - it is a remediation, not a default upgrade."""
    c = aws._client("mgn", mgn)
    templates = c.describe_replication_configuration_templates().get("items", [])
    if not templates:
        return {"resized": False, "reason": "no replication configuration template exists"}

    tpl = templates[0]
    current = tpl.get("replicationServerInstanceType", "")
    if not current.startswith(BURSTABLE_PREFIXES):
        return {"resized": False, "instance_type": current,
                "reason": f"replication server is already non-burstable ({current})"}

    stalled = _servers_the_server_cannot_serve(c)
    if not stalled:
        return {"resized": False, "instance_type": current,
                "reason": "no source server shows a symptom the server size explains"}

    target = ctx.params.get("replication_server_type", "m5.large")
    c.update_replication_configuration_template(
        replicationConfigurationTemplateID=tpl["replicationConfigurationTemplateID"],
        replicationServerInstanceType=target,
    )
    return {"resized": True, "from": current, "to": target, "stalled": stalled,
            "note": "applies to replication servers launched from here on"}


def _resolve_login(secret_arn: str, secrets) -> tuple[tuple[str, str] | None, str]:
    """Credentials are never carried in the wave inputs or the decision log - the engineer supplies
    a Secrets Manager reference and the value is read here, at the moment of use.

    Returns the login and, when there isn't one, why. A secret that cannot be read used to fall
    back to an unauthenticated request, which comes back 401 and is recorded as the application
    being unhealthy - so a wrong key name in the secret reads as a broken app on the gate that
    decides the cutover."""
    if not secret_arn:
        return None, ""
    try:
        raw = secrets.get_secret_value(SecretId=secret_arn).get("SecretString") or "{}"
    except Exception as e:  # noqa: BLE001 - the reason is the point
        return None, f"{type(e).__name__}: {e}"[:200]
    try:
        creds = json.loads(raw)
    except ValueError:
        return None, "the secret is not JSON; it needs {\"username\": ..., \"password\": ...}"
    user, password = creds.get("username"), creds.get("password")
    if user and password:
        return (user, password), ""
    missing = [k for k in ("username", "password") if not creds.get(k)]
    return None, f"the secret has no {' and no '.join(missing)}; found keys {sorted(creds)}"


def _probe_one(app: dict, secrets) -> dict:
    """One application, reduced to signals that are safe to persist: the response code, how long it
    took, and a fingerprint of the body. Never the body, never the credential."""
    import base64
    import hashlib
    import urllib.error
    import urllib.request

    name, url = app.get("name") or app.get("url", "?"), app.get("url", "")
    result: dict = {"name": name, "url": url}
    req = urllib.request.Request(url, headers={"User-Agent": "transform-agents/sanity-check"})
    login, why = _resolve_login(app.get("secret_arn", ""), secrets)
    if login:
        token = base64.b64encode(f"{login[0]}:{login[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        result["authenticated"] = True
    elif why:
        # Probed anyway - an unauthenticated 401 is still an observation - but the reason travels
        # with it, so nobody reads a misconfigured secret as a failing application.
        result["login_error"] = why

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=app.get("timeout", 10)) as resp:
            body = resp.read(PROBE_BODY_LIMIT)
            result["status"] = resp.status
    except urllib.error.HTTPError as e:
        body, result["status"] = b"", e.code
    except Exception as e:  # noqa: BLE001 - an unreachable app is a finding, not a crash
        result["status"], result["error"] = None, f"{type(e).__name__}: {e}"[:200]
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
        result["ok"] = False
        return result

    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    result["fingerprint"] = hashlib.sha256(body).hexdigest()[:16]
    result["bytes"] = len(body)
    expected = app.get("expect_status", 200)
    contains = app.get("expect_contains", "")
    result["ok"] = result["status"] == expected and (
        not contains or contains.encode() in body
    )
    if contains:
        result["expect_contains_met"] = contains.encode() in body
    return result


# What a web application looks like from outside. Only 80 and 443: the sanity check is about
# whether the application answers the way it did before, and anything it is reached on is one of
# these two. Probing a wider range would be scanning, not checking.
WEB_PORTS = ((80, "http"), (443, "https"))

# Short on purpose. This is "is anything listening", not "is it healthy" - the sanity check itself
# uses the generous timeout. A host that needs longer than this to say hello is not the one whose
# behaviour we are about to compare before and after.
DISCOVERY_TIMEOUT = 3


def _discover_probe_targets(ctx: StepContext, *, mgn=None, ec2=None) -> dict:
    """Work out what to sanity-check from the estate itself, instead of asking a person.

    The chain is already there: an MGN application groups source servers, a source server carries
    the EC2 instance the agent registered from, and EC2 knows its public address. A candidate is
    kept only if it actually answers - discovery by observation, so a machine that is not serving
    the application never becomes something the cutover is judged on.

    Returns candidates, never a verdict. An estate with no publicly reachable application yields
    nothing and the engineer is still asked; that is the honest outcome, not a failure."""
    m = aws._client("mgn", mgn)
    e = aws._client("ec2", ec2)

    apps = {a["applicationID"]: a.get("name") or a["applicationID"]
            for a in m.list_applications(maxResults=100).get("items", [])}
    # The migrated instance when there is one, the source otherwise. This is what makes the
    # before/after diff mean anything: probing the source address on both runs compares a machine
    # with itself, comes back identical, and hands the cutover a green verdict it never earned.
    by_instance: dict[str, tuple[str, str, str]] = {}
    for s in _all_source_servers(m):
        app = s.get("applicationID")
        if app not in apps:
            continue
        launched = (s.get("launchedInstance") or {}).get("ec2InstanceID")
        source = ((s.get("sourceProperties") or {}).get("identificationHints") or {}
                  ).get("awsInstanceID")
        instance = launched or source
        if instance:
            by_instance[instance] = (apps[app], _hostname(s),
                                     "migrated" if launched else "source")
    if not by_instance:
        return {"candidates": [], "reason": "no source server in an application carries an EC2 "
                                            "instance id"}

    addressed = []
    for page in _describe_instances(e, list(by_instance)):
        for res in page.get("Reservations", []):
            for inst in res.get("Instances", []):
                ip = inst.get("PublicIpAddress")
                if not ip:
                    continue
                app, host, side = by_instance[inst["InstanceId"]]
                addressed.append({"app": app, "host": host, "instance": inst["InstanceId"],
                                  "ip": ip, "side": side})

    # In parallel and with a short timeout. Most of a wave is not web servers, and a silent host
    # costs the full timeout on every port - done in sequence, seven quiet machines outlast the
    # caller. Order is restored afterwards so the result does not depend on who answered first.
    if not addressed:
        return {"candidates": [], "addressed": 0, "not_serving": [],
                "reason": "no server in an application has a public address"}

    candidates, silent = [], []
    with ThreadPoolExecutor(max_workers=min(16, len(addressed))) as pool:
        for a, hit in zip(addressed, pool.map(_first_answering, [x["ip"] for x in addressed])):
            if hit is None:
                silent.append(a["host"])
                continue
            candidates.append({"name": a["app"], "url": hit["url"], "expect_status": 200,
                               "instance": a["instance"], "host": a["host"],
                               "side": a["side"], "status": hit["status"]})
    sides = sorted({c["side"] for c in candidates})
    return {"candidates": candidates, "addressed": len(addressed),
            "not_serving": silent, "sides": sides,
            "note": "derived from MGN application membership and the instances' public addresses; "
                    "only hosts that answered are listed. `side` says whether an address is the "
                    "source or the migrated instance - the same application resolves to different "
                    "machines before and after a launch, which is what the diff compares"}


def _describe_instances(ec2, ids: list[str]) -> list[dict]:
    """In pages of fifty, so a large wave does not exceed the request limit."""
    return [ec2.describe_instances(InstanceIds=ids[i:i + 50]) for i in range(0, len(ids), 50)]


def _first_answering(ip: str) -> dict | None:
    """The first scheme the host answers on, or nothing. A redirect or an auth challenge counts:
    the application is there, and what matters is that it answers the same way afterwards."""
    import urllib.error
    import urllib.request

    for port, scheme in WEB_PORTS:
        url = f"{scheme}://{ip}/" if port in (80, 443) else f"{scheme}://{ip}:{port}/"
        req = urllib.request.Request(url, headers={"User-Agent": "transform-agents/discovery"})
        try:
            with urllib.request.urlopen(req, timeout=DISCOVERY_TIMEOUT) as resp:
                return {"url": url, "status": resp.status}
        except urllib.error.HTTPError as err:
            return {"url": url, "status": err.code}
        except Exception:  # noqa: BLE001, S112 - not serving on this port is the ordinary case
            continue
    return None


def _probe_apps(ctx: StepContext, *, secrets=None) -> dict:
    """Sanity-check every application the wave will move. Run once before the migration for a
    baseline and again after, so the QA agent compares two real observations instead of nothing."""
    apps = ctx.params.get("apps") or []
    if not apps:
        return {"probed": 0, "reason": "no application probe spec was supplied"}
    client = aws._client("secretsmanager", secrets)
    results = [_probe_one(a, client) for a in apps]
    return {
        "probed": len(results),
        "healthy": sum(1 for r in results if r.get("ok")),
        "results": results,
    }


# What Remediation is allowed to actually do, and how. An action the contract permits but that has
# no implementation here is reported as such rather than silently reported as applied - a
# remediation loop that always answers "done" is worse than one that cannot act at all.
REMEDIATION_EXECUTORS = ("restart_replication_agent", "resync_volume")


def _apply_remediation(ctx: StepContext, *, mgn=None, ssm=None) -> dict:
    """Execute one allow-listed remediation. The action name comes from the contract's
    `allowed_actions`; the target comes from the wave, never from the model."""
    action = ctx.params.get("action", "")
    if action not in REMEDIATION_EXECUTORS:
        return {"resolved": False,
                "detail": f"{action!r} has no executor; implemented: {list(REMEDIATION_EXECUTORS)}"}

    if action == "resync_volume":
        sid = ctx.params.get("source_server_id")
        if not sid:
            return {"resolved": False, "detail": "resync_volume needs a source_server_id"}
        aws._client("mgn", mgn).retry_data_replication(sourceServerID=sid)
        return {"resolved": True, "action": action, "source_server_id": sid,
                "detail": "asked MGN to restart data replication for this server"}

    instance = ctx.params.get("instance_id")
    if not instance:
        return {"resolved": False, "detail": "restart_replication_agent needs an instance_id"}
    sent = aws._client("ssm", ssm).send_command(
        InstanceIds=[instance],
        DocumentName="AWS-RunShellScript",
        Comment="transform-agents: restart the MGN replication agent",
        Parameters={"commands": ["systemctl restart aws-replication-agent",
                                 "systemctl is-active aws-replication-agent"]},
    )
    return {"resolved": True, "action": action, "instance_id": instance,
            "command_id": sent["Command"]["CommandId"],
            "detail": "restart issued; the agent re-registers with MGN on its next check-in"}


def _start_replication(ctx: StepContext, *, mgn=None) -> dict:
    c = aws._client("mgn", mgn)
    for sid in ctx.params["source_server_ids"]:
        c.start_replication(sourceServerID=sid)
    return {"replicating": ctx.params["source_server_ids"]}


def _launch_test(ctx: StepContext, *, mgn=None) -> dict:
    c = aws._client("mgn", mgn)
    job = c.start_test(sourceServerIDs=ctx.params["source_server_ids"])
    return {"job_id": job["job"]["jobID"]}


def _terminate_test_instances(ctx: StepContext, *, mgn=None) -> dict:
    """Throw away the instances a test launch created.

    Part of the test cycle, not a rollback: a test instance exists to be probed and then discarded,
    and the ones left running hold vCPU the cutover needs. Refuses to touch a server whose instance
    came from a cutover - those are the migration, not a rehearsal."""
    c = aws._client("mgn", mgn)
    wanted = set(ctx.params.get("source_server_ids") or [])
    disposable, protected = [], []
    for s in _all_source_servers(c):
        sid = s["sourceServerID"]
        if wanted and sid not in wanted:
            continue
        if not ((s.get("launchedInstance") or {}).get("ec2InstanceID")):
            continue
        if (s.get("lifeCycle") or {}).get("lastCutover", {}).get("initiated"):
            protected.append(sid)
            continue
        disposable.append(sid)
    if not disposable:
        return {"terminated": 0, "protected": protected,
                "reason": "no test instance is running for these servers"}
    job = c.terminate_target_instances(sourceServerIDs=disposable)
    return {"terminated": len(disposable), "source_server_ids": disposable,
            "protected": protected, "job_id": job["job"]["jobID"]}


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


def _green_test_verdict(wave_id: str, ddb=None) -> dict | None:
    """The wave's own record of having tested the migration. Read server-side for the same reason
    policy is: a caller that skipped the test - an agent choosing its own order, a hand-rolled
    invocation - must not be able to reach the cutover by simply not mentioning it."""
    table = os.environ.get("DECISION_LOG_TABLE")
    if not table:
        return None
    client = aws._client("dynamodb", ddb)
    kwargs: dict = {
        "TableName": table,
        "KeyConditionExpression": "wave_id = :w",
        "ExpressionAttributeValues": {":w": {"S": wave_id}},
    }
    while True:
        resp = client.query(**kwargs)
        for item in resp.get("Items", []):
            v = (json.loads(item["doc"]["S"]).get("detail") or {}).get("verdict") or {}
            if v.get("step") == "test" and v.get("ok") and (v.get("judged") or 0) > 0:
                return v
        if "LastEvaluatedKey" not in resp:
            return None
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _cutover(ctx: StepContext, *, route53=None, mgn=None, ddb=None) -> dict:
    if not _green_test_verdict(ctx.wave_id, ddb):
        raise StepRejected(
            "no green test verdict judged on a real diff exists for this wave - the migration has "
            "not been shown to work, so the cutover is refused"
        )
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
    *, ledger: Ledger | None = None, mgn=None, route53=None, codepipeline=None, ec2=None,
    secrets=None, ddb=None, ssm=None,
) -> Dispatcher:
    d = Dispatcher(ledger=ledger)
    d.register("deploy_lza", lambda ctx: _deploy_lza(ctx, codepipeline=codepipeline))
    d.register("initialize_mgn", lambda ctx: _initialize_mgn(ctx, mgn=mgn, ec2=ec2))
    d.register("resize_replication_server", lambda ctx: _resize_replication_server(ctx, mgn=mgn))
    d.register("probe_apps", lambda ctx: _probe_apps(ctx, secrets=secrets))
    d.register("discover_probe_targets",
               lambda ctx: _discover_probe_targets(ctx, mgn=mgn, ec2=ec2))
    d.register("mgn_status", lambda ctx: _mgn_status(ctx, mgn=mgn))
    d.register("reconcile_wave_inventory",
               lambda ctx: _reconcile_wave_inventory(ctx, mgn=mgn))
    d.register("apply_remediation", lambda ctx: _apply_remediation(ctx, mgn=mgn, ssm=ssm))
    d.register("start_replication", lambda ctx: _start_replication(ctx, mgn=mgn))
    d.register("launch_test", lambda ctx: _launch_test(ctx, mgn=mgn))
    d.register("terminate_test_instances",
               lambda ctx: _terminate_test_instances(ctx, mgn=mgn))
    d.register("cutover", lambda ctx: _cutover(ctx, route53=route53, mgn=mgn, ddb=ddb))
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
