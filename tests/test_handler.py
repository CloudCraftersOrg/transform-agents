import io
import json

import pytest

from dispatcher.handler import build_lambda_dispatcher, handler
from dispatcher.steps import InMemoryLedger, LambdaInvokingDispatcher, StepContext, StepRejected
from tools.spec import load_contract

C = load_contract()
APP = "i-0d6b944117ba6302b"


class _FakeMgn:
    def __init__(self):
        self.calls = []

    def start_replication(self, **kw):
        self.calls.append(("start_replication", kw))

    def start_cutover(self, **kw):
        self.calls.append(("start_cutover", kw))


class _FakeRoute53:
    def __init__(self):
        self.calls = []

    def change_resource_record_sets(self, **kw):
        self.calls.append(kw)
        return {"ChangeInfo": {"Id": "/change/C1"}}


class _FakeDdb:
    """The wave's decision log, as the cutover step reads it server-side."""

    def __init__(self, verdict=None):
        self._verdict = verdict

    def query(self, **kw):
        if self._verdict is None:
            return {"Items": []}
        doc = json.dumps({"detail": {"verdict": self._verdict}})
        return {"Items": [{"doc": {"S": doc}}]}


GREEN_ON_REAL_DIFF = {"step": "test", "ok": True, "judged": 2}


def _dispatcher(verdict=GREEN_ON_REAL_DIFF):
    return build_lambda_dispatcher(
        ledger=InMemoryLedger(), mgn=_FakeMgn(), route53=_FakeRoute53(), codepipeline=None,
        ddb=_FakeDdb(verdict),
    )


def _ctx(step_id, params):
    return StepContext(wave_id="w1", step_id=step_id, contract=C, params=params)


def test_start_replication_calls_mgn_per_server():
    d = _dispatcher()
    out = d.dispatch("start_replication", _ctx("replicate", {"source_server_ids": ["s1", "s2"]}))
    assert out == {"replicating": ["s1", "s2"]}


def test_cutover_flips_dns_then_calls_mgn(monkeypatch):
    monkeypatch.setenv("DECISION_LOG_TABLE", "t-log")
    d = _dispatcher()
    out = d.dispatch(
        "cutover",
        _ctx("cutover", {
            "source_server_ids": ["s1"], "hosted_zone_id": "Z1",
            "record_name": "app.example.com", "target_ip": "10.0.0.9", "ttl": 60,
        }),
    )
    assert out["dns"]["pointed_at"] == "10.0.0.9"


CUTOVER_PARAMS = {
    "source_server_ids": ["s1"], "hosted_zone_id": "Z1",
    "record_name": "app.example.com", "target_ip": "10.0.0.9", "ttl": 60,
}


def test_cutover_is_refused_without_a_green_test_verdict(monkeypatch):
    """The guard lives in the step, not in the order run_wave happens to call things - an agent
    choosing its own sequence must not reach the cutover by never mentioning the test."""
    monkeypatch.setenv("DECISION_LOG_TABLE", "t-log")
    d = _dispatcher(verdict=None)
    with pytest.raises(StepRejected, match="not been shown to work"):
        d.dispatch("cutover", _ctx("cutover", CUTOVER_PARAMS))


def test_cutover_is_refused_when_the_verdict_judged_nothing(monkeypatch):
    """A green verdict on an empty diff is exactly the hole this closes."""
    monkeypatch.setenv("DECISION_LOG_TABLE", "t-log")
    d = _dispatcher(verdict={"step": "test", "ok": True, "judged": 0})
    with pytest.raises(StepRejected, match="not been shown to work"):
        d.dispatch("cutover", _ctx("cutover", CUTOVER_PARAMS))


def test_cutover_is_refused_on_a_red_verdict(monkeypatch):
    monkeypatch.setenv("DECISION_LOG_TABLE", "t-log")
    d = _dispatcher(verdict={"step": "test", "ok": False, "judged": 3})
    with pytest.raises(StepRejected):
        d.dispatch("cutover", _ctx("cutover", CUTOVER_PARAMS))


def test_rollback_keeps_replication_alive():
    d = _dispatcher()
    out = d.dispatch(
        "rollback",
        _ctx("test-rollback", {
            "hosted_zone_id": "Z1", "record_name": "app.example.com", "source_ip": "10.0.0.1",
        }),
    )
    assert out["dns"]["pointed_at"] == "10.0.0.1" and out["replication"] == "kept alive"


def test_dispatch_is_idempotent_across_calls():
    d = _dispatcher()
    p = {"source_server_ids": ["s1"]}
    first = d.dispatch("start_replication", _ctx("replicate", p))
    second = d.dispatch("start_replication", _ctx("replicate", p))
    assert first == second


def test_handler_reevaluates_policy_and_returns_structured_rejection():
    event = {
        "wave_id": "w1", "step_id": "s1", "step": "cutover",
        "params": {"source_server_ids": ["s1"], "hosted_zone_id": "Z1",
                   "record_name": "a", "target_ip": "10.0.0.9"},
        "contract": C.model_dump(mode="json"),
        "guard": {"type": "recommend_instance", "subject": APP, "arch": "arm64"},
    }
    out = handler(event)
    assert out["ok"] is False and out["rejected"] is True and "ARM64" in out["reason"]


# --- LambdaInvokingDispatcher: the Runtime-side adapter ---


class _FakeLambda:
    def __init__(self, body: dict, function_error: str | None = None):
        self._body = body
        self._function_error = function_error
        self.invoked: list[tuple[str, dict]] = []

    def invoke(self, FunctionName, Payload):  # matches the boto3 kwargs
        self.invoked.append((FunctionName, json.loads(Payload)))
        resp = {"Payload": io.BytesIO(json.dumps(self._body).encode())}
        if self._function_error:
            resp["FunctionError"] = self._function_error
        return resp


def test_lambda_dispatcher_returns_the_step_result():
    lam = _FakeLambda({"ok": True, "result": {"replicating": ["s1"]}})
    d = LambdaInvokingDispatcher("transform-agents-step-dispatcher", client=lam)
    out = d.dispatch("start_replication", _ctx("replicate", {"source_server_ids": ["s1"]}))
    assert out == {"replicating": ["s1"]}
    name, event = lam.invoked[0]
    assert name == "transform-agents-step-dispatcher"
    assert event["step"] == "start_replication" and event["wave_id"] == "w1"
    assert event["contract"]["case_id"] == "fbctf-001"


def test_lambda_dispatcher_reraises_step_rejection():
    lam = _FakeLambda({"ok": False, "rejected": True, "reason": "architecture arm64 not allowed"})
    d = LambdaInvokingDispatcher("arn:aws:lambda:us-east-1:111122223333:function:x", client=lam)
    try:
        d.dispatch("cutover", _ctx("cutover", {}))
        raise AssertionError("expected StepRejected")
    except StepRejected as e:
        assert "arm64" in str(e)


def test_lambda_dispatcher_surfaces_function_error_for_remediation():
    # a failed step is the remediation agent's input, not a crash: it comes back as {"error": ...}
    lam = _FakeLambda({"errorMessage": "boom"}, function_error="Unhandled")
    d = LambdaInvokingDispatcher("x", client=lam)
    result = d.dispatch("cutover", _ctx("cutover", {}))
    assert "boom" in result["error"]


class _InitMgn:
    def __init__(self, templates=()):
        self.templates, self.created, self.initialized = list(templates), [], False

    def initialize_service(self):
        self.initialized = True

    def describe_replication_configuration_templates(self):
        return {"items": self.templates}

    def create_replication_configuration_template(self, **kw):
        self.created.append(kw)
        return {"replicationConfigurationTemplateID": "tpl-new"}


class _FakeEc2:
    def describe_subnets(self, SubnetIds=None):
        pub = {"SubnetId": "subnet-pub", "VpcId": "vpc-1", "MapPublicIpOnLaunch": True}
        priv = {"SubnetId": "subnet-priv", "VpcId": "vpc-1", "MapPublicIpOnLaunch": False}
        return {"Subnets": [priv, pub] if SubnetIds is None else [pub]}

    def describe_security_groups(self, Filters):
        return {"SecurityGroups": [{"GroupId": "sg-default"}]}


def _init_ctx():
    return StepContext(wave_id="w1", step_id="blocker-initialize_mgn", contract=C, params={})


def test_initializing_mgn_creates_the_template_that_marks_the_account_ready():
    from dispatcher.handler import _initialize_mgn

    mgn, ec2 = _InitMgn(), _FakeEc2()
    out = _initialize_mgn(_init_ctx(), mgn=mgn, ec2=ec2)
    assert mgn.initialized and out["created"] and out["template_id"] == "tpl-new"
    # the staging area has to reach the MGN endpoint, so a public subnet wins over a private one
    assert mgn.created[0]["stagingAreaSubnetId"] == "subnet-pub"
    assert mgn.created[0]["replicationServersSecurityGroupsIDs"] == ["sg-default"]
    # a shared replication server carries the whole wave: burstable types drain their CPU credits
    # mid-sync and agents then fail to connect, so the default must not be a t-family instance
    assert not mgn.created[0]["replicationServerInstanceType"].startswith("t")


def test_the_replication_server_type_can_be_overridden_per_wave():
    from dispatcher.handler import _initialize_mgn

    mgn = _InitMgn()
    ctx = StepContext(wave_id="w1", step_id="blocker", contract=C,
                      params={"replication_server_type": "m5.2xlarge"})
    _initialize_mgn(ctx, mgn=mgn, ec2=_FakeEc2())
    assert mgn.created[0]["replicationServerInstanceType"] == "m5.2xlarge"


def test_initializing_mgn_twice_reuses_the_existing_template():
    from dispatcher.handler import _initialize_mgn

    mgn = _InitMgn(templates=[{"replicationConfigurationTemplateID": "tpl-old"}])
    out = _initialize_mgn(_init_ctx(), mgn=mgn, ec2=_FakeEc2())
    assert out == {"template_id": "tpl-old", "created": False}
    assert mgn.created == []


class _TplMgn:
    def __init__(self, instance_type="t3.small", failing=(), templates=True):
        self.instance_type, self.failing, self.updated = instance_type, list(failing), []
        self._templates = templates

    def describe_replication_configuration_templates(self):
        if not self._templates:
            return {"items": []}
        return {"items": [{"replicationConfigurationTemplateID": "rct-1",
                           "replicationServerInstanceType": self.instance_type}]}

    def describe_source_servers(self, **kw):
        items = []
        for h in self.failing:
            # alternate the two symptoms of the same overload
            err = ("FAILED_TO_CONNECT_AGENT_TO_REPLICATION_SERVER"
                   if len(items) % 2 == 0 else "FAILED_TO_PAIR_REPLICATION_SERVER_WITH_AGENT")
            items.append({
                "sourceProperties": {"identificationHints": {"hostname": h}},
                "dataReplicationInfo": {"dataReplicationError": {"error": err}},
            })
        items.append({"sourceProperties": {"identificationHints": {"hostname": "healthy-01"}},
                      "dataReplicationInfo": {"dataReplicationState": "CONTINUOUS"}})
        return {"items": items}

    def update_replication_configuration_template(self, **kw):
        self.updated.append(kw)
        return {"replicationConfigurationTemplateID": "rct-1"}


def _resize_ctx(**params):
    return StepContext(wave_id="w1", step_id="stall-resize-4", contract=C, params=params)


def test_a_burstable_replication_server_is_resized_when_agents_cannot_connect():
    from dispatcher.handler import _resize_replication_server

    mgn = _TplMgn(instance_type="t3.small", failing=["cache-01", "nfs-01"])
    out = _resize_replication_server(_resize_ctx(), mgn=mgn)
    assert out["resized"] and out["from"] == "t3.small" and out["to"] == "m5.large"
    assert out["stalled"] == ["cache-01", "nfs-01"]
    assert mgn.updated[0]["replicationServerInstanceType"] == "m5.large"


def test_a_stall_with_no_saturation_symptom_is_not_blamed_on_the_server_size():
    from dispatcher.handler import _resize_replication_server

    mgn = _TplMgn(instance_type="t3.small", failing=[])
    out = _resize_replication_server(_resize_ctx(), mgn=mgn)
    assert out["resized"] is False and "symptom the server size explains" in out["reason"]
    assert mgn.updated == []


def test_a_non_burstable_replication_server_is_left_alone():
    from dispatcher.handler import _resize_replication_server

    mgn = _TplMgn(instance_type="m5.large", failing=["cache-01"])
    out = _resize_replication_server(_resize_ctx(), mgn=mgn)
    assert out["resized"] is False and "already non-burstable" in out["reason"]
    assert mgn.updated == []


def test_the_resize_target_is_overridable_and_never_burstable_by_default():
    from dispatcher.handler import BURSTABLE_PREFIXES, _resize_replication_server

    mgn = _TplMgn(instance_type="t3.small", failing=["cache-01"])
    out = _resize_replication_server(_resize_ctx(replication_server_type="c5.xlarge"), mgn=mgn)
    assert out["to"] == "c5.xlarge"
    assert not out["to"].startswith(BURSTABLE_PREFIXES)


class _RemediationAws:
    def __init__(self):
        self.retried, self.commands = [], []

    def retry_data_replication(self, **kw):
        self.retried.append(kw)
        return {"sourceServer": {"sourceServerID": kw["sourceServerID"]}}

    def send_command(self, **kw):
        self.commands.append(kw)
        return {"Command": {"CommandId": "cmd-1"}}


def test_resync_volume_asks_mgn_to_restart_replication():
    from dispatcher.handler import _apply_remediation

    a = _RemediationAws()
    out = _apply_remediation(
        StepContext(wave_id="w1", step_id="r", contract=C,
                    params={"action": "resync_volume", "source_server_id": "s-1"}),
        mgn=a, ssm=a)
    assert out["resolved"] and a.retried[0]["sourceServerID"] == "s-1"


def test_restart_replication_agent_goes_through_ssm():
    from dispatcher.handler import _apply_remediation

    a = _RemediationAws()
    out = _apply_remediation(
        StepContext(wave_id="w1", step_id="r", contract=C,
                    params={"action": "restart_replication_agent", "instance_id": "i-1"}),
        mgn=a, ssm=a)
    assert out["resolved"] and out["command_id"] == "cmd-1"
    assert a.commands[0]["InstanceIds"] == ["i-1"]
    assert "aws-replication-agent" in a.commands[0]["Parameters"]["commands"][0]


def test_an_action_with_no_executor_says_so_instead_of_claiming_success():
    """A remediation loop that always answers 'done' is worse than one that cannot act."""
    from dispatcher.handler import _apply_remediation

    a = _RemediationAws()
    out = _apply_remediation(
        StepContext(wave_id="w1", step_id="r", contract=C,
                    params={"action": "increase_replication_timeout"}),
        mgn=a, ssm=a)
    assert out["resolved"] is False and "no executor" in out["detail"]
    assert a.retried == [] and a.commands == []


def test_an_action_without_a_target_cannot_guess_one():
    from dispatcher.handler import _apply_remediation

    a = _RemediationAws()
    out = _apply_remediation(
        StepContext(wave_id="w1", step_id="r", contract=C, params={"action": "resync_volume"}),
        mgn=a, ssm=a)
    assert out["resolved"] is False and "needs a source_server_id" in out["detail"]


def test_not_converging_now_counts_as_a_saturation_symptom():
    """Same undersized replication server, third symptom - it kept slipping past a narrower filter."""
    from dispatcher.handler import SATURATION_SYMPTOMS, _resize_replication_server

    mgn = _TplMgn(instance_type="t3.small", failing=["EC2AMAZ-K9LQ97Q"])

    def _servers(**kw):
        return {"items": [{
            "sourceProperties": {"identificationHints": {"hostname": "EC2AMAZ-K9LQ97Q"}},
            "dataReplicationInfo": {"dataReplicationError": {"error": "NOT_CONVERGING"}}}]}

    mgn.describe_source_servers = _servers
    out = _resize_replication_server(_resize_ctx(), mgn=mgn)
    assert "NOT_CONVERGING" in SATURATION_SYMPTOMS
    assert out["resized"] and out["stalled"] == ["EC2AMAZ-K9LQ97Q"]


# --- reconciling what the wave tracks with what actually replicated -------------------------
# A VMware import leaves the wave tracking placeholders that will never get an agent while the
# machines that finished sit outside every application. This step swaps them. It rewrites what the
# wave *is*, so almost all of it is refusals.

def _mgn_inventory(pairs, extra=()):
    """pairs: (app, placeholder_host, twin_host). extra: source servers as-is."""
    items = []
    for app, ph, twin in pairs:
        items.append({"sourceServerID": f"s-ph-{ph}", "applicationID": app,
                      "lifeCycle": {"state": "PENDING_INSTALLATION"},
                      "tags": {"hostname": ph, "CreatedBy": "AWSTransform"},
                      "sourceProperties": {"identificationHints": {}}})
        if twin:
            items.append({"sourceServerID": f"s-live-{twin}",
                          "lifeCycle": {"state": "READY_FOR_TEST"},
                          "dataReplicationInfo": {"dataReplicationState": "CONTINUOUS"},
                          "sourceProperties": {"identificationHints": {"hostname": twin}}})
    items.extend(extra)
    return items


class _Mgn:
    def __init__(self, items):
        self._items = items
        self.associated, self.disassociated = [], []

    def describe_source_servers(self, **kw):
        return {"items": self._items}

    def associate_source_servers(self, applicationID, sourceServerIDs):
        self.associated.append((applicationID, sourceServerIDs))

    def disassociate_source_servers(self, applicationID, sourceServerIDs):
        self.disassociated.append((applicationID, sourceServerIDs))


def _reconcile(items, **params):
    from dispatcher.handler import _reconcile_wave_inventory

    mgn = _Mgn(items)
    return _reconcile_wave_inventory(_ctx("reconcile_wave_inventory", params), mgn=mgn), mgn


def test_the_replicating_server_joins_the_application_its_placeholder_sits_in():
    out, mgn = _reconcile(_mgn_inventory([("app-1", "mq-01", "mq-01")]))
    assert out["reconciled"] is True
    assert mgn.associated == [("app-1", ["s-live-mq-01"])]
    assert mgn.disassociated == [("app-1", ["s-ph-mq-01"])]


def test_servers_are_added_before_placeholders_are_removed():
    """Between the two calls the application is over-full. Empty would drop it out of the wave."""
    out, mgn = _reconcile(_mgn_inventory([("app-1", "mq-01", "mq-01")]))
    assert out["reconciled"] and mgn.associated and mgn.disassociated


def test_a_suffix_only_difference_still_matches():
    """The import records EC2AMAZ-K9LQ97Q.WORKGROUP; the agent reports EC2AMAZ-K9LQ97Q."""
    out, mgn = _reconcile(_mgn_inventory([("app-1", "EC2AMAZ-K9LQ97Q.WORKGROUP", "EC2AMAZ-K9LQ97Q")]))
    assert out["reconciled"] is True
    assert mgn.associated == [("app-1", ["s-live-EC2AMAZ-K9LQ97Q"])]


def test_nothing_changes_when_one_placeholder_has_no_twin():
    """All or nothing: a partial swap leaves the wave tracking a mixture nobody can reason about."""
    out, mgn = _reconcile(_mgn_inventory([("app-1", "mq-01", "mq-01"), ("app-1", "ghost-01", None)]))
    assert out["reconciled"] is False
    assert out["unmatched"] == ["ghost-01"]
    assert mgn.associated == [] and mgn.disassociated == []


def test_an_ambiguous_short_name_is_refused_rather_than_guessed():
    twins = [
        {"sourceServerID": "s-live-a", "lifeCycle": {"state": "READY_FOR_TEST"},
         "dataReplicationInfo": {"dataReplicationState": "CONTINUOUS"},
         "sourceProperties": {"identificationHints": {"hostname": "web-01.prod"}}},
        {"sourceServerID": "s-live-b", "lifeCycle": {"state": "READY_FOR_TEST"},
         "dataReplicationInfo": {"dataReplicationState": "CONTINUOUS"},
         "sourceProperties": {"identificationHints": {"hostname": "web-01.dr"}}},
    ]
    out, mgn = _reconcile(_mgn_inventory([("app-1", "web-01.corp", None)], extra=twins))
    assert out["reconciled"] is False and mgn.associated == []


def test_a_replicating_server_is_never_treated_as_a_placeholder():
    """Tags outlive their meaning. Removing a finished machine would drop it out of the wave."""
    from dispatcher.handler import _is_placeholder

    assert not _is_placeholder({
        "lifeCycle": {"state": "READY_FOR_TEST"},
        "tags": {"CreatedBy": "AWSTransform"},
        "dataReplicationInfo": {"dataReplicationState": "CONTINUOUS"}})
    assert _is_placeholder({
        "lifeCycle": {"state": "PENDING_INSTALLATION"}, "tags": {"CreatedBy": "AWSTransform"}})
    assert not _is_placeholder({
        "lifeCycle": {"state": "PENDING_INSTALLATION"}, "tags": {"CreatedBy": "someone-else"}})


def test_dry_run_reports_the_swap_without_making_it():
    out, mgn = _reconcile(_mgn_inventory([("app-1", "mq-01", "mq-01")]), dry_run=True)
    assert out["dry_run"] is True
    assert out["would_add"] == {"app-1": ["s-live-mq-01"]}
    assert mgn.associated == [] and mgn.disassociated == []


def test_with_no_placeholders_it_does_nothing():
    out, mgn = _reconcile([])
    assert out["reconciled"] is False and mgn.associated == []


def test_a_half_applied_run_can_be_finished_by_repeating_it():
    """MGN answers AccessDenied - not a conflict - when a server is associated twice, so a run
    that died halfway could never be completed by re-running it. The delta is against what the
    application holds now."""
    items = _mgn_inventory([("app-1", "mq-01", "mq-01"), ("app-1", "nfs-01", "nfs-01")])
    for i in items:
        if i["sourceServerID"] == "s-live-mq-01":
            i["applicationID"] = "app-1"          # the first half already landed
    out, mgn = _reconcile(items)
    assert out["reconciled"] is True
    assert mgn.associated == [("app-1", ["s-live-nfs-01"])]          # only the missing one
    assert mgn.disassociated == [("app-1", ["s-ph-mq-01", "s-ph-nfs-01"])]


def test_a_placeholder_already_out_of_every_application_is_left_alone():
    """After a successful run the placeholders still exist, just orphaned. Re-running must report
    nothing to do rather than calling them unmatched forever."""
    items = _mgn_inventory([("app-1", "mq-01", "mq-01")])
    for i in items:
        if i["sourceServerID"] == "s-ph-mq-01":
            i["applicationID"] = None
        if i["sourceServerID"] == "s-live-mq-01":
            i["applicationID"] = "app-1"
    out, mgn = _reconcile(items)
    assert out["reconciled"] is False
    assert "no service-created placeholder is left in an application" in out["reason"]
    assert mgn.associated == [] and mgn.disassociated == []


# --- the login the sanity check resolves at the moment of use ----------------------------------
# The engineer supplies a Secrets Manager reference, never a credential. A reference that cannot be
# read used to fall back to an unauthenticated request: 401, recorded as the application being
# unhealthy, on the gate that decides the cutover.

class _Secrets:
    def __init__(self, value=None, raises=None):
        self._value, self._raises = value, raises

    def get_secret_value(self, SecretId):  # boto3 casing
        if self._raises:
            raise self._raises
        return {"SecretString": self._value}


def test_a_well_formed_secret_resolves_to_a_login():
    from dispatcher.handler import _resolve_login

    login, why = _resolve_login("arn:x", _Secrets('{"username": "svc", "password": "p"}'))
    assert login == ("svc", "p") and why == ""


def test_the_wrong_key_names_are_named_rather_than_guessed_at():
    from dispatcher.handler import _resolve_login

    login, why = _resolve_login("arn:x", _Secrets('{"user": "svc", "pass": "p"}'))
    assert login is None
    assert "no username and no password" in why and "'pass'" in why and "'user'" in why


def test_a_half_filled_secret_says_which_half():
    from dispatcher.handler import _resolve_login

    _login, why = _resolve_login("arn:x", _Secrets('{"username": "svc"}'))
    assert "no password" in why and "no username" not in why


def test_a_secret_that_is_not_json_says_what_shape_it_needs():
    from dispatcher.handler import _resolve_login

    _login, why = _resolve_login("arn:x", _Secrets("hunter2"))
    assert "not JSON" in why and "username" in why


def test_a_denied_or_missing_secret_is_reported_not_swallowed():
    from dispatcher.handler import _resolve_login

    _login, why = _resolve_login("arn:x", _Secrets(raises=RuntimeError("AccessDeniedException")))
    assert "AccessDenied" in why


def test_no_reference_is_not_an_error():
    from dispatcher.handler import _resolve_login

    assert _resolve_login("", _Secrets()) == (None, "")


def test_the_reason_travels_with_the_probe_result():
    from dispatcher.handler import _probe_one

    out = _probe_one({"name": "app", "url": "http://127.0.0.1:1/", "secret_arn": "arn:x"},
                     _Secrets('{"user": "svc"}'))
    assert out["ok"] is False
    assert "no username and no password" in out["login_error"]
    assert "authenticated" not in out


def test_the_credential_never_reaches_the_result():
    from dispatcher.handler import _probe_one

    out = _probe_one({"name": "app", "url": "http://127.0.0.1:1/", "secret_arn": "arn:x"},
                     _Secrets('{"username": "svc", "password": "hunter2"}'))
    assert "hunter2" not in json.dumps(out) and "svc" not in json.dumps(out)
    assert out["authenticated"] is True


# --- finding what to sanity-check without asking a person --------------------------------------

class _Mgn2:
    def __init__(self, servers, apps):
        self._servers, self._apps = servers, apps

    def describe_source_servers(self, **kw):
        return {"items": self._servers}

    def list_applications(self, **kw):
        return {"items": self._apps}


class _Ec2:
    def __init__(self, instances):
        self._instances = instances
        self.asked = []

    def describe_instances(self, InstanceIds):  # boto3 casing
        self.asked.append(list(InstanceIds))
        return {"Reservations": [{"Instances": [
            {"InstanceId": i, **self._instances[i]} for i in InstanceIds if i in self._instances]}]}


def _server(sid, app, instance, host):
    return {"sourceServerID": sid, "applicationID": app,
            "lifeCycle": {"state": "READY_FOR_TEST"},
            "sourceProperties": {"identificationHints": {"hostname": host,
                                                         "awsInstanceID": instance}}}


def _discover(monkeypatch, servers, apps, instances, answering=()):
    from dispatcher import handler

    monkeypatch.setattr(handler, "_first_answering",
                        lambda ip: {"url": f"http://{ip}/", "status": 200} if ip in answering
                        else None)
    ec2 = _Ec2(instances)
    out = handler._discover_probe_targets(
        _ctx("discover", {}), mgn=_Mgn2(servers, apps), ec2=ec2)
    return out, ec2


def test_only_hosts_that_answer_become_something_the_cutover_is_judged_on(monkeypatch):
    out, _ = _discover(
        monkeypatch,
        [_server("s1", "app-1", "i-1", "web-01"), _server("s2", "app-1", "i-2", "nfs-01")],
        [{"applicationID": "app-1", "name": "app_catalog"}],
        {"i-1": {"PublicIpAddress": "198.51.100.7"}, "i-2": {"PublicIpAddress": "198.51.100.8"}},
        answering={"198.51.100.7"})
    assert [c["url"] for c in out["candidates"]] == ["http://198.51.100.7/"]
    assert out["candidates"][0]["name"] == "app_catalog"
    assert out["not_serving"] == ["nfs-01"]


def test_a_server_with_no_public_address_is_not_probed(monkeypatch):
    out, ec2 = _discover(
        monkeypatch, [_server("s1", "app-1", "i-1", "internal-01")],
        [{"applicationID": "app-1", "name": "app_x"}], {"i-1": {}})
    assert out["candidates"] == [] and out["addressed"] == 0
    assert ec2.asked == [["i-1"]]


def test_a_server_outside_every_application_is_not_a_candidate(monkeypatch):
    """The wave is the applications. A machine nobody grouped is not part of what is being moved."""
    out, _ = _discover(
        monkeypatch, [_server("s1", None, "i-1", "stray-01")],
        [{"applicationID": "app-1", "name": "app_x"}],
        {"i-1": {"PublicIpAddress": "198.51.100.9"}}, answering={"198.51.100.9"})
    assert out["candidates"] == []


def test_an_estate_with_nothing_to_go_on_says_so_rather_than_guessing(monkeypatch):
    out, _ = _discover(monkeypatch, [], [], {})
    assert out["candidates"] == [] and "no source server" in out["reason"]


def test_instances_are_described_in_pages(monkeypatch):
    servers = [_server(f"s{n}", "app-1", f"i-{n}", f"h{n}") for n in range(120)]
    _out, ec2 = _discover(monkeypatch, servers, [{"applicationID": "app-1", "name": "a"}], {})
    assert [len(page) for page in ec2.asked] == [50, 50, 20]


def test_silent_hosts_do_not_outlast_the_caller(monkeypatch):
    """Sequentially, seven quiet machines at two ports each exceed the invoking client's timeout.
    The wall clock has to track the slowest host, not their sum."""
    import time as _time

    from dispatcher import handler

    def _slow(_ip):
        _time.sleep(0.3)

    monkeypatch.setattr(handler, "_first_answering", _slow)
    servers = [_server(f"s{n}", "app-1", f"i-{n}", f"h{n}") for n in range(10)]
    instances = {f"i-{n}": {"PublicIpAddress": f"198.51.100.{n}"} for n in range(10)}
    started = _time.monotonic()
    out = handler._discover_probe_targets(
        _ctx("discover", {}), mgn=_Mgn2(servers, [{"applicationID": "app-1", "name": "a"}]),
        ec2=_Ec2(instances))
    elapsed = _time.monotonic() - started
    assert len(out["not_serving"]) == 10
    assert elapsed < 10 * 0.3, f"probed in sequence: {elapsed:.1f}s"


def test_the_reported_order_does_not_depend_on_who_answered_first(monkeypatch):
    from dispatcher import handler

    monkeypatch.setattr(handler, "_first_answering",
                        lambda ip: {"url": f"http://{ip}/", "status": 200})
    servers = [_server(f"s{n}", "app-1", f"i-{n}", f"h{n}") for n in range(5)]
    instances = {f"i-{n}": {"PublicIpAddress": f"198.51.100.{n}"} for n in range(5)}
    out = handler._discover_probe_targets(
        _ctx("discover", {}), mgn=_Mgn2(servers, [{"applicationID": "app-1", "name": "a"}]),
        ec2=_Ec2(instances))
    assert [c["host"] for c in out["candidates"]] == [f"h{n}" for n in range(5)]


# --- discarding a test instance ---------------------------------------------------------------
# A test instance exists to be probed and then thrown away. The ones left running hold vCPU the
# cutover needs - a whole wave failed to launch for want of it.

class _MgnTerm:
    def __init__(self, servers):
        self._servers = servers
        self.terminated = None

    def describe_source_servers(self, **kw):
        return {"items": self._servers}

    def terminate_target_instances(self, sourceServerIDs):  # boto3 casing
        self.terminated = list(sourceServerIDs)
        return {"job": {"jobID": "mgnjob-term"}}


def _srv(sid, instance=None, cutover=False):
    life = {"state": "TESTING", "lastCutover": {"initiated": {"jobID": "j"} if cutover else {}}}
    s = {"sourceServerID": sid, "lifeCycle": life,
         "sourceProperties": {"identificationHints": {"hostname": sid}}}
    if instance:
        s["launchedInstance"] = {"ec2InstanceID": instance}
    return s


def _terminate(servers, ids=None):
    from dispatcher.handler import _terminate_test_instances

    mgn = _MgnTerm(servers)
    out = _terminate_test_instances(_ctx("terminate", {"source_server_ids": ids or []}), mgn=mgn)
    return out, mgn


def test_a_running_test_instance_is_discarded():
    out, mgn = _terminate([_srv("s-1", "i-1"), _srv("s-2", "i-2")])
    assert out["terminated"] == 2 and sorted(mgn.terminated) == ["s-1", "s-2"]


def test_a_server_with_no_test_instance_is_left_alone():
    _out, mgn = _terminate([_srv("s-1"), _srv("s-2", "i-2")])
    assert mgn.terminated == ["s-2"]


def test_a_cutover_instance_is_never_discarded_as_if_it_were_a_rehearsal():
    """After a cutover the instance is the migration. Terminating it would undo the move."""
    out, mgn = _terminate([_srv("s-1", "i-1", cutover=True), _srv("s-2", "i-2")])
    assert mgn.terminated == ["s-2"]
    assert out["protected"] == ["s-1"]


def test_only_the_named_servers_are_touched():
    out, mgn = _terminate([_srv("s-1", "i-1"), _srv("s-2", "i-2")], ids=["s-2"])
    assert mgn.terminated == ["s-2"] and out["terminated"] == 1


def test_nothing_to_discard_is_not_an_error():
    out, mgn = _terminate([_srv("s-1")])
    assert out["terminated"] == 0 and mgn.terminated is None


def test_the_migrated_instance_is_what_an_application_resolves_to_once_it_exists(monkeypatch):
    """Probing the source address on both runs compares a machine with itself, comes back
    identical, and hands the cutover a green verdict it never earned."""
    from dispatcher import handler

    monkeypatch.setattr(handler, "_first_answering",
                        lambda ip: {"url": f"http://{ip}/", "status": 200})
    src = _server("s1", "app-1", "i-source", "web-01")
    out, _ = _discover(monkeypatch, [src], [{"applicationID": "app-1", "name": "app_x"}],
                       {"i-source": {"PublicIpAddress": "198.51.100.7"}},
                       answering={"198.51.100.7"})
    assert out["candidates"][0]["side"] == "source"
    assert out["candidates"][0]["url"] == "http://198.51.100.7/"

    launched = {**src, "launchedInstance": {"ec2InstanceID": "i-migrated"}}
    out, _ = _discover(monkeypatch, [launched], [{"applicationID": "app-1", "name": "app_x"}],
                       {"i-migrated": {"PublicIpAddress": "198.51.100.9"}},
                       answering={"198.51.100.9"})
    assert out["candidates"][0]["side"] == "migrated"
    assert out["candidates"][0]["url"] == "http://198.51.100.9/"
    assert out["sides"] == ["migrated"]


# Silence used to be one thing. It is at least three, and they call for opposite responses: a
# service waiting on a dependency, a service that is not running, and a network dropping traffic.
class _OpenSocket:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _reaches(monkeypatch, outcome):
    import socket

    def _connect(*_a, **_k):
        if outcome == "open":
            return _OpenSocket()
        raise outcome
    monkeypatch.setattr(socket, "create_connection", _connect)


def test_a_service_hanging_on_a_dependency_is_not_reported_as_a_dead_one(monkeypatch):
    """The real incident: the migrated web servers accepted connections and never replied, because
    the database they wanted was still in the old VPC. That is indistinguishable from a dead
    service unless the connection and the reply are asked about separately."""
    import urllib.request

    from dispatcher import handler
    _reaches(monkeypatch, "open")

    def _hang(*_a, **_k):
        raise TimeoutError
    monkeypatch.setattr(urllib.request, "urlopen", _hang)

    out = handler._first_answering("198.51.100.7")
    assert "status" not in out
    assert "never replied" in out["why"]
    assert "dependency" in out["why"]


def test_a_refused_connection_says_nothing_is_listening(monkeypatch):
    from dispatcher import handler
    _reaches(monkeypatch, ConnectionRefusedError())
    assert "nothing is listening" in handler._first_answering("198.51.100.8")["why"]


def test_a_dropped_packet_points_at_the_network_rather_than_the_service(monkeypatch):
    from dispatcher import handler
    _reaches(monkeypatch, TimeoutError())
    why = handler._first_answering("198.51.100.9")["why"]
    assert "dropping traffic" in why
    assert "listening" not in why


def test_an_unroutable_address_is_reported_as_unreachable(monkeypatch):
    from dispatcher import handler
    _reaches(monkeypatch, OSError("no route to host"))
    assert "could not be reached" in handler._first_answering("198.51.100.10")["why"]


def test_a_host_that_answers_still_reports_its_status(monkeypatch):
    import urllib.request

    from dispatcher import handler
    _reaches(monkeypatch, "open")

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    assert handler._first_answering("198.51.100.11")["status"] == 200


def test_discovery_records_why_each_silent_host_said_nothing(monkeypatch):
    from dispatcher import handler
    monkeypatch.setattr(
        handler, "_first_answering",
        lambda ip: {"url": f"http://{ip}/", "status": 200} if ip.endswith(".7")
        else {"why": "refused the connection - nothing is listening"})
    out = handler._discover_probe_targets(
        _ctx("discover", {}),
        mgn=_Mgn2([_server("s1", "app-1", "i-1", "web-01"),
                   _server("s2", "app-1", "i-2", "nfs-01")],
                  [{"applicationID": "app-1", "name": "app_catalog"}]),
        ec2=_Ec2({"i-1": {"PublicIpAddress": "198.51.100.7"},
                  "i-2": {"PublicIpAddress": "198.51.100.8"}}))
    assert out["not_serving"] == ["nfs-01"]
    assert out["not_serving_why"]["nfs-01"].startswith("refused")
