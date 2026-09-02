from pathlib import Path

import pytest

from agents.finops import evaluate_deviation
from agents.model import FakeModel
from tools.policy import Action, evaluate_policy
from tools.spec import load_contract

C = load_contract(Path(__file__).resolve().parents[1] / "fixtures" / "vmware-001" / "decision_contract.json")
TAGGED = {"tags": {"app": "vmware-001"}}


def test_contract_loads_and_is_3yr_ri():
    assert C.case_id == "vmware-001"
    assert C.budget.pricing_model == "3yr_ri"
    assert C.budget.ceiling_with_variance == pytest.approx(51.6)


def test_no_arch_trap_here_but_sizing_basis_still_bites():
    # this workload has no ARM constraint -> arm64 is allowed
    assert evaluate_policy(Action("recommend_instance", subject="*", arch="arm64", vcpu=2, ram_gib=4.0), C).allowed
    # but Transform right-sized on peak, so 'average' sizing is denied
    assert not evaluate_policy(Action("set_sizing_basis", sizing_basis="average"), C).allowed


def test_finops_aligns_when_resources_carry_transform_cost():
    # per-server monthly_usd = 3yr annualized total / 12; 2 servers + 2 boot volumes
    resources = [
        {"instance_type": "c7a.medium", "ebs_gib": 30, "monthly_usd": round(227.63 / 12, 2), **TAGGED},
        {"instance_type": "c7a.medium", "ebs_gib": 30, "monthly_usd": round(227.63 / 12, 2), **TAGGED},
    ]
    f = evaluate_deviation(FakeModel(["unused"]), contract=C, launched_resources=resources)
    assert f.within_budget  # ~42.9 <= 51.6


def test_finops_flags_on_demand_launch_as_over_budget():
    # same instances but priced on-demand (no monthly_usd) -> c7a.medium from the table, no RI discount
    resources = [
        {"instance_type": "c7a.medium", "ebs_gib": 30, **TAGGED},
        {"instance_type": "c7a.medium", "ebs_gib": 30, **TAGGED},
    ]
    # budget is priced 3yr_ri, so monthly_run_rate still applies the 3yr factor; force on-demand math
    C_od = C.model_copy(update={"budget": C.budget.model_copy(update={"pricing_model": "on_demand"})})
    f = evaluate_deviation(
        FakeModel(['{"attribution":"launched on-demand instead of 3-year RI"}']),
        contract=C_od, launched_resources=resources,
    )
    assert not f.within_budget and f.attribution
