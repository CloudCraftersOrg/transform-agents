import pytest

from dispatcher.steps import Dispatcher, StepContext, StepRejected
from tools.policy import Action
from tools.spec import load_contract

C = load_contract()
APP = "i-0d6b944117ba6302b"


def _dispatcher():
    d = Dispatcher()
    calls = {"n": 0}

    def fn(_ctx):
        calls["n"] += 1
        return {"ran": calls["n"]}

    d.register("cutover", fn)
    return d, calls


def test_idempotent_by_wave_and_step():
    d, calls = _dispatcher()
    ctx = StepContext(wave_id="w1", step_id="s1", contract=C)
    assert d.dispatch("cutover", ctx) == {"ran": 1}
    assert d.dispatch("cutover", ctx) == {"ran": 1}
    assert calls["n"] == 1


def test_server_side_policy_reeval_rejects():
    d, _ = _dispatcher()
    ctx = StepContext(wave_id="w1", step_id="s1", contract=C)
    guard = Action("recommend_instance", subject=APP, arch="arm64")
    with pytest.raises(StepRejected):
        d.dispatch("cutover", ctx, guard=guard)
