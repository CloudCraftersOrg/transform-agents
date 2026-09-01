import pytest
from pydantic import ValidationError

from tools.spec import Constraint, load_contract


def test_fixture_loads():
    c = load_contract()
    assert c.case_id == "fbctf-001"
    assert c.feature_flags.lza_enabled is False
    assert c.steps[0] == "precheck"


def test_unknown_predicate_rejected():
    with pytest.raises(ValidationError):
        Constraint(id="x", subject="*", predicate="max_gpu", value=1, source="...")


def test_empty_source_rejected():
    with pytest.raises(ValidationError):
        Constraint(id="x", subject="*", predicate="min_vcpu", value=2, source="")


def test_ceiling_with_variance():
    assert load_contract().budget.ceiling_with_variance == pytest.approx(218.5)


def test_constraints_for_wildcard_only():
    preds = {c.predicate for c in load_contract().constraints_for("i-0422e26203cb709b2")}
    assert "min_vcpu" in preds
    assert "arch_not_allowed" not in preds
