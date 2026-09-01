import pytest

from tools.aws import cloudwatch_logs, mgn_counts, mgn_jobs, ssm_run_command


class _FakeMgn:
    def __init__(self, items=None, jobs=None):
        self._items = items or []
        self._jobs = jobs or []

    def describe_source_servers(self, filters):
        wanted = set(filters["sourceServerIDs"])
        return {"items": [s for s in self._items if s["sourceServerID"] in wanted]}

    def describe_jobs(self, filters, maxResults):  # matches the boto3 kwargs
        return {"items": self._jobs[:maxResults]}


def test_mgn_counts_aggregates_states():
    mgn = _FakeMgn(
        [
            {"sourceServerID": "s1", "dataReplicationInfo": {"dataReplicationState": "CONTINUOUS"},
             "lifeCycle": {"state": "READY_FOR_CUTOVER"}},
            {"sourceServerID": "s2", "dataReplicationInfo": {"dataReplicationState": "INITIAL_SYNC"},
             "lifeCycle": {"state": "NOT_READY"}},
            {"sourceServerID": "s3", "dataReplicationInfo": {"dataReplicationState": "STALLED"},
             "lifeCycle": {"state": "NOT_READY"}},
        ]
    )
    c = mgn_counts(["s1", "s2", "s3"], client=mgn)
    assert c.total == 3 and c.replicating == 2 and c.ready == 1 and c.failed == 1


def test_mgn_jobs_flattens_per_server_launch_status():
    mgn = _FakeMgn(jobs=[
        {"jobID": "j1", "type": "LAUNCH", "status": "COMPLETED", "initiatedBy": "START_TEST",
         "participatingServers": [
             {"sourceServerID": "s1", "launchStatus": "LAUNCHED"},
             {"sourceServerID": "s2", "launchStatus": "FAILED"},
         ]},
    ])
    jobs = mgn_jobs(client=mgn)
    assert jobs[0]["jobID"] == "j1"
    assert jobs[0]["servers"][1] == {"sourceServerID": "s2", "launchStatus": "FAILED"}


class _FakeLogs:
    def get_log_events(self, **_kw):
        return {"events": [{"message": "line one"}, {"message": "line two"}]}


def test_cloudwatch_logs_joins_messages():
    assert cloudwatch_logs("g", "s", client=_FakeLogs()) == "line one\nline two"


class _FakeSsm:
    def send_command(self, **kw):
        self.seen = kw
        return {"Command": {"CommandId": "cmd-1", "Status": "Pending"}}


def test_ssm_run_command_enforces_allow_list():
    ssm = _FakeSsm()
    out = ssm_run_command(
        "i-1", "restart_replication_agent", {}, allowed_documents=["restart_replication_agent"], client=ssm
    )
    assert out == {"command_id": "cmd-1", "status": "Pending"}
    with pytest.raises(PermissionError):
        ssm_run_command("i-1", "delete_everything", {}, allowed_documents=["restart_replication_agent"], client=ssm)
