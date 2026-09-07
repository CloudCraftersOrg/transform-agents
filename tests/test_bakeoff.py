from agents.model import FakeModel
from evals.bakeoff.run import load_cases, score, tool_catalog, validate_cases


def test_case_file_well_formed():
    cases = load_cases()
    assert len(cases) >= 20
    assert validate_cases(cases) == []
    assert len({c["id"] for c in cases}) == len(cases)


def test_every_tool_the_agent_actually_has_is_exercised():
    """The catalog is derived from the deployed tools, so a tool added without a case fails here
    instead of quietly lowering what the score covers."""
    covered = {c["expected_tool"] for c in load_cases()}
    assert covered == set(tool_catalog())


def test_the_catalog_is_the_agent_s_own_tools_not_a_second_list():
    from agents.tools import build_tools

    assert set(tool_catalog()) == {t.tool_name for t in build_tools(None)}


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
    report = score(FakeModel(["escalate", "migration_status"]), cases)
    assert report.correct == 1
    assert report.by_tool["escalate"] == (1, 2)
