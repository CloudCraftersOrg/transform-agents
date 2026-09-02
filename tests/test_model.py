from agents.model import BedrockModel, FakeModel, strip_code_fence


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


class _FakeConverse:
    def __init__(self):
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": "delegate_finops"}]}}}


def test_bedrockmodel_converse_shape():
    fake = _FakeConverse()
    m = BedrockModel("amazon.nova-lite-v1:0", client=fake, max_tokens=64)
    assert m.complete("pick a tool", system="you are the orchestrator") == "delegate_finops"
    (call,) = fake.calls
    assert call["modelId"] == "amazon.nova-lite-v1:0"
    assert call["messages"] == [{"role": "user", "content": [{"text": "pick a tool"}]}]
    assert call["system"] == [{"text": "you are the orchestrator"}]
    assert call["inferenceConfig"]["maxTokens"] == 64
