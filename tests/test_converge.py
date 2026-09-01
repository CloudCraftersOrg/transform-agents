from agents.trust import converge
from tools.validators import ValidationOutcome


def test_converges_on_third_try():
    n = {"i": 0}

    def gen(_errors):
        n["i"] += 1
        return f"try{n['i']}"

    def val(v):
        return ValidationOutcome(v == "try3", None if v == "try3" else "nope")

    r = converge(gen, val, max_iter=5)
    assert r.ok and r.iterations == 3 and r.value == "try3"


def test_exhausts_budget():
    r = converge(lambda _e: "x", lambda _v: ValidationOutcome(False, "bad"), max_iter=4)
    assert not r.ok and r.iterations == 4 and len(r.errors) == 4


def test_errors_are_fed_back_to_generator():
    seen: list[list[str]] = []

    def gen(errors):
        seen.append(list(errors))
        return "x"

    converge(gen, lambda _v: ValidationOutcome(False, "e"), max_iter=3)
    assert seen == [[], ["e"], ["e", "e"]]
