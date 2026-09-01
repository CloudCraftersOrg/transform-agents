from agents.model import FakeModel, strip_code_fence


def test_strip_plain_passthrough():
    assert strip_code_fence("  hello  ") == "hello"


def test_strip_json_fence():
    assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_bare_fence():
    assert strip_code_fence("```\nx: 1\n```") == "x: 1"


def test_fakemodel_repeats_last_and_records_prompts():
    m = FakeModel(["a", "b"])
    assert [m.complete("p1"), m.complete("p2"), m.complete("p3")] == ["a", "b", "b"]
    assert m.prompts == ["p1", "p2", "p3"]
