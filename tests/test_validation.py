import json

from agents.model import FakeModel
from agents.validation import build_validation, issue_verdict

SPEC = "billing responds 200 and the /invoice schema does not change"


def test_green_verdict_no_rollback():
    v = issue_verdict(
        FakeModel(['{"verdict":"green","reasons":[]}']),
        spec=SPEC, diffs={"added": {}}, criticality="high",
    )
    assert v.ok and not v.rollback_requested


def test_red_verdict_triggers_rollback():
    calls: list[str] = []
    v = issue_verdict(
        FakeModel(['{"verdict":"red","reasons":["the /invoice schema changed"]}']),
        spec=SPEC, diffs={"changed": {"total": ["int", "str"]}}, criticality="high",
        request_rollback=calls.append,
    )
    assert not v.ok and v.rollback_requested and calls


def test_illegible_verdict_defaults_to_red():
    v = issue_verdict(FakeModel(["not json"]), spec=SPEC, diffs={}, criticality="high")
    assert not v.ok


def test_build_validation_parses_json_objective():
    fn = build_validation(FakeModel(['{"verdict":"green","reasons":[]}']))
    assert fn(json.dumps({"spec": SPEC, "diffs": {}, "criticality": "medium"})).ok
