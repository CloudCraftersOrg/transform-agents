from pathlib import Path

from tools.policy import Action, evaluate_policy
from tools.spec import load_contract

C = load_contract(
    Path(__file__).resolve().parents[1] / "fixtures" / "discovery-001" / "decision_contract.json"
)


def test_contract_loads():
    assert C.case_id == "discovery-001"
    assert C.budget.pricing_model == "3yr_ri"
    assert {c.predicate for c in C.constraints} == {"sizing_basis", "min_ram_gib", "out_of_scope"}


def test_unidentified_windows_box_is_out_of_scope():
    d = evaluate_policy(Action("dispatch_step", subject="EC2AMAZ-HVB61P6", step="cutover"), C)
    assert not d.allowed and "out of scope" in d.reasons[0]


def test_identified_servers_are_in_scope():
    assert evaluate_policy(Action("dispatch_step", subject="catalog-svc-01", step="cutover"), C).allowed


def test_average_sizing_is_denied_estate_wide():
    assert not evaluate_policy(Action("set_sizing_basis", sizing_basis="average"), C).allowed
