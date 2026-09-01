from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from tools.validators import ValidationOutcome

Generator = Callable[[list[str]], str]
Validator = Callable[[str], ValidationOutcome]


@dataclass
class ConvergeResult:
    ok: bool
    value: str | None
    iterations: int
    errors: list[str] = field(default_factory=list)


def converge(generate: Generator, validate: Validator, max_iter: int = 5) -> ConvergeResult:
    """generate -> validate against a deterministic oracle -> regenerate with the error as context."""
    errors: list[str] = []
    value: str | None = None
    for i in range(1, max_iter + 1):
        value = generate(errors)
        outcome = validate(value)
        if outcome.ok:
            return ConvergeResult(True, value, i, errors)
        errors.append(outcome.error or "invalido")
    return ConvergeResult(False, value, max_iter, errors)
