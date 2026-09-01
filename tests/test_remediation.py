from agents.model import FakeModel
from agents.remediation import remediate
from tools.runbooks import InMemoryRunbookKB

ALLOWED = ["restart_replication_agent", "resync_volume"]


def test_resolves_on_first_valid_action():
    applied: list[str] = []

    def apply_action(a):
        applied.append(a)
        return {"resolved": True}

    r = remediate(
        FakeModel(['{"action":"resync_volume","rationale":"checksum"}']),
        failure="checksum mismatch on vol-1", allowed_actions=ALLOWED, apply_action=apply_action,
    )
    assert r.resolved and r.action == "resync_volume" and r.runbook and applied == ["resync_volume"]


def test_action_outside_whitelist_is_refused():
    r = remediate(
        FakeModel(['{"action":"delete_account","rationale":"nuke"}']),
        failure="x", allowed_actions=ALLOWED, apply_action=lambda a: {"resolved": True}, max_retries=2,
    )
    assert not r.resolved and r.attempts == 2 and "allow-list" in r.notes[0]


def test_retries_then_gives_up():
    r = remediate(
        FakeModel(['{"action":"resync_volume","rationale":"y"}']),
        failure="x", allowed_actions=ALLOWED,
        apply_action=lambda a: {"resolved": False, "detail": "still bad"}, max_retries=3,
    )
    assert not r.resolved and r.attempts == 3 and len(r.notes) == 3


def test_illegible_response_counts_as_attempt():
    r = remediate(FakeModel(["no json"]), failure="x", allowed_actions=ALLOWED, max_retries=2)
    assert not r.resolved and len(r.notes) == 2


def test_learning_loop_second_occurrence_skips_the_model():
    kb = InMemoryRunbookKB()

    def apply_action(a):
        return {"resolved": a == "resync_volume"}

    first = remediate(
        FakeModel(['{"action":"resync_volume","rationale":"checksum mismatch"}']),
        failure="checksum mismatch on vol-1", allowed_actions=ALLOWED, apply_action=apply_action, kb=kb,
    )
    assert first.resolved and not first.from_runbook and len(kb) == 1

    model_should_not_be_called = FakeModel(["SHOULD NOT BE CALLED"])
    second = remediate(
        model_should_not_be_called,
        failure="checksum mismatch on vol-9", allowed_actions=ALLOWED, apply_action=apply_action, kb=kb,
    )
    assert second.resolved and second.from_runbook and second.attempts == 1
    assert model_should_not_be_called.prompts == []
    assert len(kb) == 1  # the runbook is not duplicated when resolved via precedent
