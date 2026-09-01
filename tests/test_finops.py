from agents.finops import evaluate_deviation
from agents.model import FakeModel
from tools.spec import load_contract

C = load_contract()  # ceiling 190, variance 15% -> 218.5


def test_within_budget_does_not_call_model():
    model = FakeModel(["SHOULD NOT BE CALLED"])
    f = evaluate_deviation(
        model, contract=C,
        launched_resources=[{"instance_type": "t3.medium", "ebs_gib": 30, "tags": {"app": "billing"}}],
    )
    assert f.within_budget and model.prompts == []


def test_over_budget_untagged_is_unattributable():
    f = evaluate_deviation(
        FakeModel(["x"]), contract=C,
        launched_resources=[{"instance_type": "m5.large", "ebs_gib": 2000, "tags": {}}],
    )
    assert not f.within_budget and f.unattributable and f.attribution is None


def test_over_budget_tagged_gets_attribution():
    f = evaluate_deviation(
        FakeModel(['{"attribution":"EBS oversized in billing"}']), contract=C,
        launched_resources=[{"instance_type": "m5.large", "ebs_gib": 2000, "tags": {"app": "billing"}}],
    )
    assert not f.within_budget and f.attribution == "EBS oversized in billing"


def test_over_budget_illegible_attribution_not_fabricated():
    f = evaluate_deviation(
        FakeModel(["no json"]), contract=C,
        launched_resources=[{"instance_type": "m5.large", "ebs_gib": 2000, "tags": {"app": "billing"}}],
    )
    assert f.unattributable and f.attribution is None


def test_no_launched_resources_is_a_finding_not_a_crash():
    f = evaluate_deviation(FakeModel(["x"]), contract=C, launched_resources=[])
    assert f.unattributable and f.actual_usd == 0.0 and "not computed" in f.notes[0]
