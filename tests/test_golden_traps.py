"""The three traps from section 6 of the plan, as acceptance criteria for the policy engine."""

from tools.policy import Action, evaluate_policy
from tools.spec import load_contract

C = load_contract()
APP = "i-0d6b944117ba6302b"
WEB = "i-0422e26203cb709b2"


def test_trap_sizing_floor():
    # 1.2% CPU / 17% RAM -> an agent without policy proposes t3.micro. Correct: 2 vCPU / 2 GiB floor.
    assert not evaluate_policy(Action("recommend_instance", subject=APP, vcpu=1, ram_gib=1.0), C).allowed
    assert not evaluate_policy(Action("set_sizing_basis", sizing_basis="average"), C).allowed


def test_trap_graviton_partial():
    # Graviton for everything -> ARM64 on both tiers. Correct: web nginx yes, app HHVM no.
    assert evaluate_policy(Action("recommend_instance", subject=WEB, arch="arm64", vcpu=2, ram_gib=2.0), C).allowed
    assert not evaluate_policy(Action("recommend_instance", subject=APP, arch="arm64", vcpu=2, ram_gib=2.0), C).allowed


def test_trap_hack_modernization_escalates():
    # "Modernize the code" -> Transform does not cover PHP/Hack. Correct: the agent escalates, not invents.
    d = evaluate_policy(Action("modernize", subject="fbctf-app", modernization_target="hack"), C)
    assert not d.allowed
    assert d.escalate
