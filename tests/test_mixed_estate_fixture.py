from pathlib import Path

import pytest

from tools.policy import Action, evaluate_policy
from tools.spec import load_contract

C = load_contract(
    Path(__file__).resolve().parents[1] / "fixtures" / "mixed-estate-001" / "decision_contract.json"
)


def test_contract_loads_with_real_transform_costing():
    assert C.case_id == "mixed-estate-001"
    assert C.budget.pricing_model == "3yr_ri"
    assert C.budget.ceiling_monthly_usd == 587  # 3yr RI infra incl. FSx, from the Financial Summary
    assert "provisional" in C.budget.basis  # every server had a "vCPU missing" warning


def test_unidentified_windows_box_is_out_of_scope():
    d = evaluate_policy(Action("dispatch_step", subject="EC2AMAZ-HVB61P6", step="cutover"), C)
    assert not d.allowed and "out of scope" in d.reasons[0]


def test_identified_servers_are_in_scope():
    for name in ("mq-01", "catalog-svc-01", "EC2AMAZ-K9LQ97Q"):
        assert evaluate_policy(Action("dispatch_step", subject=name, step="cutover"), C).allowed


def test_peak_sizing_is_required_estate_wide():
    assert not evaluate_policy(Action("set_sizing_basis", sizing_basis="average"), C).allowed
    assert evaluate_policy(Action("set_sizing_basis", sizing_basis="peak_with_headroom"), C).allowed


def test_no_modernization_block_on_this_estate():
    # no modernization_unsupported constraint -> the container artifact path is available
    d = evaluate_policy(Action("modernize", subject="catalog-svc-01", modernization_target="container"), C)
    assert d.allowed and not d.escalate


def test_ceiling_with_variance():
    assert C.budget.ceiling_with_variance == pytest.approx(587 * 1.2)
