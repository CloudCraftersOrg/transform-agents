import pytest

from tools.policy import Action, evaluate_policy
from tools.spec import load_contract

C = load_contract()
WEB = "i-0422e26203cb709b2"
APP = "i-0d6b944117ba6302b"

CASES = [
    ("t3.micro below the floor", Action("recommend_instance", subject=APP, vcpu=1, ram_gib=1.0), False),
    ("2 vCPU / 2 GiB at the floor", Action("recommend_instance", subject=APP, vcpu=2, ram_gib=2.0), True),
    ("arm64 on the HHVM app", Action("recommend_instance", subject=APP, arch="arm64", vcpu=2, ram_gib=4.0), False),
    ("arm64 on the nginx web tier", Action("recommend_instance", subject=WEB, arch="arm64", vcpu=2, ram_gib=2.0), True),
    ("sizing by average", Action("set_sizing_basis", sizing_basis="average"), False),
    ("sizing by peak with headroom", Action("set_sizing_basis", sizing_basis="peak_with_headroom"), True),
    ("modernize Hack", Action("modernize", subject="fbctf-app", modernization_target="hack"), False),
    ("cost over ceiling+variance", Action("estimate_cost", est_monthly_usd=230.0), False),
    ("cost within variance", Action("estimate_cost", est_monthly_usd=210.0), True),
]


@pytest.mark.parametrize("name,action,allowed", CASES, ids=[c[0] for c in CASES])
def test_policy_table(name, action, allowed):
    assert evaluate_policy(action, C).allowed is allowed


def test_denial_carries_source_citation():
    d = evaluate_policy(Action("recommend_instance", subject=APP, arch="arm64"), C)
    assert not d.allowed
    assert "HHVM 3.21 has no ARM64 build" in d.reasons[0]
