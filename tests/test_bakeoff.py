from agents.model import FakeModel
from agents.orchestrator import TOOL_CATALOG
from evals.bakeoff.run import load_cases, score, validate_cases


def test_case_file_well_formed():
    cases = load_cases()
    assert len(cases) >= 20
    assert validate_cases(cases) == []
    assert len({c["id"] for c in cases}) == len(cases)


def test_every_tool_has_at_least_one_case():
    covered = {c["expected_tool"] for c in load_cases()}
    assert covered == set(TOOL_CATALOG)


def test_score_perfect_with_oracle_model():
    cases = load_cases()
    by_prompt = {c["prompt"]: c["expected_tool"] for c in cases}
    report = score(FakeModel(lambda p: by_prompt[p]), cases)
    assert report.accuracy == 1.0


def test_score_counts_per_tool():
    cases = [
        {"id": "a", "prompt": "p1", "expected_tool": "escalate"},
        {"id": "b", "prompt": "p2", "expected_tool": "escalate"},
    ]
    report = score(FakeModel(["escalate", "evaluate_policy"]), cases)
    assert report.correct == 1
    assert report.by_tool["escalate"] == (1, 2)
