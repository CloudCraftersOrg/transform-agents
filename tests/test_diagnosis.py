from agents.model import FakeModel
from evals.scorecard import diagnosis_precision, load_failure_catalog
from tools.spec import load_contract


def test_catalog_is_populated():
    failures = load_failure_catalog()
    assert len(failures) >= 3
    assert all({"id", "text", "expected_action"} <= f.keys() for f in failures)


def test_diagnosis_precision_perfect_with_oracle_model():
    catalog = load_failure_catalog()
    by_text = {f["text"]: f["expected_action"] for f in catalog}

    def respond(prompt: str) -> str:
        for text, action in by_text.items():
            if text in prompt:
                # 'escalate' is not a valid SSM action; emit something off-list so remediate gives up
                return f'{{"action":"{action if action != "escalate" else "nope"}","rationale":"r"}}'
        return '{"action":"nope","rationale":"r"}'

    result = diagnosis_precision(FakeModel(respond), load_contract())
    assert result is not None and result.precision == 1.0


def test_diagnosis_precision_counts_a_wrong_call():
    result = diagnosis_precision(
        FakeModel(['{"action":"resync_volume","rationale":"always the same"}']), load_contract()
    )
    # only the checksum-mismatch failure (f2) expects resync_volume; the rest are wrong
    assert result is not None and 0 < result.precision < 1
