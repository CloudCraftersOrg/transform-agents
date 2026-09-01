import json
from pathlib import Path

from agents.extraction import extract_contract
from agents.model import FakeModel

BC = (Path(__file__).resolve().parents[1] / "fixtures" / "fbctf" / "business_case.md").read_text(
    encoding="utf-8"
)

GOOD = json.dumps(
    {
        "case_id": "fbctf-001",
        "source_ref": "s3://x/business_case.md",
        "budget": {"ceiling_monthly_usd": 190, "basis": "as_is_run_rate", "max_variance_pct": 15},
        "constraints": [
            {"id": "c1", "subject": "i-0d6b944117ba6302b", "predicate": "arch_not_allowed",
             "value": "arm64", "source": "HHVM 3.21 has no ARM64 build"},
            {"id": "c2", "subject": "*", "predicate": "min_vcpu", "value": 2,
             "source": "Do not recommend anything below 2 vCPU / 2 GiB"},
            {"id": "c3", "subject": "fbctf-app", "predicate": "modernization_unsupported",
             "value": "hack", "source": "AWS Transform code modernization does not cover PHP/Hack"},
        ],
        "scope": {"excluded": []},
        "steps": ["precheck", "interpret", "replicate", "test", "cutover", "parity", "finops"],
        "feature_flags": {"lza_enabled": False},
    }
)


def test_converges_on_valid_json():
    result, contract = extract_contract(BC, FakeModel([GOOD]))
    assert result.ok and contract is not None
    preds = {c.predicate for c in contract.constraints}
    assert {"arch_not_allowed", "min_vcpu", "modernization_unsupported"} <= preds


def test_regenerates_after_bad_json():
    model = FakeModel(["not json", GOOD])
    result, contract = extract_contract(BC, model)
    assert result.ok and result.iterations == 2 and contract is not None
    assert "failed validation" in model.prompts[1]


def test_unknown_predicate_rejected_then_gives_up():
    bad = GOOD.replace('"min_vcpu"', '"max_gpu"')
    result, contract = extract_contract(BC, FakeModel([bad]), max_iter=3)
    assert not result.ok and contract is None and result.iterations == 3


def test_constraint_without_source_is_rejected():
    bad = GOOD.replace('"HHVM 3.21 has no ARM64 build"', '"   "')
    result, _ = extract_contract(BC, FakeModel([bad, GOOD]))
    assert result.ok and result.iterations == 2


def test_code_fence_is_stripped():
    result, contract = extract_contract(BC, FakeModel([f"```json\n{GOOD}\n```"]))
    assert result.ok and contract is not None
