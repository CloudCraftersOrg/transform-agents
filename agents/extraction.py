from __future__ import annotations

from pydantic import ValidationError

from agents.model import ModelLike, strip_code_fence
from agents.trust import ConvergeResult, converge
from tools.spec import PREDICATES, DecisionContract
from tools.validators import ValidationOutcome

SYSTEM_PROMPT = (
    "You derive a decision contract from a business case written in prose. Closed predicate "
    "vocabulary: " + ", ".join(sorted(PREDICATES)) + ". Every constraint carries a 'source' with "
    "the literal quote it came from. Return only the contract's JSON, no explanation."
)


def _oracle(text: str) -> ValidationOutcome:
    try:
        contract = DecisionContract.model_validate_json(text)
    except ValidationError as e:
        return ValidationOutcome(False, str(e))
    empty = [c.id for c in contract.constraints if not c.source.strip()]
    if empty:
        return ValidationOutcome(False, f"constraints without a source: {empty}")
    return ValidationOutcome(True)


def _prompt(business_case: str, errors: list[str]) -> str:
    parts = [f"BUSINESS CASE:\n{business_case}"]
    if errors:
        parts.append(f"The previous attempt failed validation: {errors[-1]}\nFix it.")
    return "\n\n".join(parts)


def extract_contract(
    business_case: str, model: ModelLike, max_iter: int = 5
) -> tuple[ConvergeResult, DecisionContract | None]:
    """The system's critical step: reading prose and deriving constraints is judgment.
    Deterministic, schema-validated output, inside the Trust loop."""

    def generate(errors: list[str]) -> str:
        return strip_code_fence(model.complete(_prompt(business_case, errors), system=SYSTEM_PROMPT))

    result = converge(generate, _oracle, max_iter=max_iter)
    contract = (
        DecisionContract.model_validate_json(result.value)
        if result.ok and result.value
        else None
    )
    return result, contract
