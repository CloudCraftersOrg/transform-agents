from pathlib import Path

import pytest

from tools.finops import monthly_run_rate
from tools.policy import Action, evaluate_policy
from tools.spec import load_contract
from tools.transform_ingest import (
    INSTANCE_SPEC,
    load_business_case,
    migration_plan_to_business_case,
    plan_sizing_findings,
    read_migration_plan,
)

PLAN = Path(__file__).resolve().parents[1] / "fixtures" / "wave-0" / "plan"
C = load_contract(Path(__file__).resolve().parents[1] / "fixtures" / "wave-0" / "decision_contract.json")
PKG = read_migration_plan(PLAN)


def test_reads_the_wave_and_every_server():
    assert PKG.wave == "Wave 0"
    assert len(PKG.servers) == 12
    mq = next(s for s in PKG.servers if s["name"] == "mq-01")
    assert mq["instance_type"] == "t3a.nano"
    assert mq["cores"] == 2 and mq["ram_gib"] == 1.86
    assert mq["move_group"] == "Infra-MoveGroup"


def test_reads_apps_move_groups_databases():
    assert {a["name"] for a in PKG.apps} == {"Contoso Scoreboard", "Project Nami", "Contoso Catalog"}
    assert {a["strategy"] for a in PKG.apps} == {"rehost"}
    assert PKG.move_groups["app_contoso_scoreboard"] == "Infra-MoveGroup"
    assert {d["name"] for d in PKG.databases} == {"MSSQLSERVER", "SSRS", "XE"}


def test_citations_and_audit_carry_provenance():
    assert len(PKG.citations) == 12
    any_cite = next(iter(PKG.citations.values()))
    assert "server_inventory.csv" in any_cite["source"]
    assert PKG.audit["validator_results"]["wave_plan_validation"] == "pass"
    assert PKG.audit["config"]["seed"] == 42


def test_plan_undersizes_nine_of_its_own_servers():
    findings = {f["server"] for f in plan_sizing_findings(PKG)}
    assert len(findings) == 9
    assert "mq-01" in findings and "ip-10-50-10-208.ec2.internal" in findings
    # the three the plan sized correctly
    assert findings.isdisjoint(
        {"EC2AMAZ-HVB61P6.WORKGROUP", "ip-10-40-0-108.ec2.internal", "ip-10-40-10-96.ec2.internal"}
    )


@pytest.mark.parametrize(
    "server,instance",
    [("mq-01", "t3a.nano"), ("ci-01", "t3a.micro"), ("ip-10-50-10-208.ec2.internal", "m7a.medium")],
)
def test_policy_refuses_the_planned_instance(server, instance):
    vcpu, gib = INSTANCE_SPEC[instance]
    d = evaluate_policy(Action("recommend_instance", subject=server, vcpu=vcpu, ram_gib=gib), C)
    assert not d.allowed and d.reasons


def test_policy_accepts_a_right_sized_instance():
    d = evaluate_policy(Action("recommend_instance", subject="mq-01", vcpu=2, ram_gib=2.0), C)
    assert d.allowed


def test_unidentified_windows_box_is_out_of_scope():
    d = evaluate_policy(
        Action("dispatch_step", subject="EC2AMAZ-HVB61P6.WORKGROUP", step="cutover"), C
    )
    assert not d.allowed and "out of scope" in d.reasons[0]


def test_modernization_stays_open_the_plan_only_says_rehost():
    d = evaluate_policy(
        Action("modernize", subject="catalog-svc-01", modernization_target="container"), C
    )
    assert d.allowed and not d.escalate


def test_run_rate_of_the_plan_as_written_fits_the_ceiling():
    resources = [{"instance_type": s["instance_type"]} for s in PKG.servers]
    assert monthly_run_rate(resources, pricing_model="on_demand") == pytest.approx(258.56, abs=0.5)
    assert C.budget.ceiling_monthly_usd == 259


def test_business_case_is_auto_detected_and_carries_the_findings():
    bc = load_business_case(PLAN)
    assert bc.startswith("# AWS Transform Migration Plan - Wave 0")
    assert "Plan sizing shortfalls (auto-derived, 9 of 12 servers)" in bc
    assert "wave_plan_validation: pass" in bc
    assert bc == migration_plan_to_business_case(PKG)


def test_committed_business_case_matches_the_plan():
    committed = (PLAN.parent / "business_case.md").read_text(encoding="utf-8")
    assert committed == migration_plan_to_business_case(PKG)
