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


# The oracle's contract, stated for the model: without it a real model invents wrapper keys and
# burns the iteration budget on schema instead of on the business case.
CONTRACT_SCHEMA = """Emit exactly this JSON object at the top level - no wrapper key, no prose:

{
  "case_id": "<short slug>",
  "source_ref": "<where the business case came from>",
  "budget": {"ceiling_monthly_usd": <number>, "basis": "<how you got it>",
             "pricing_model": "on_demand|1yr_ri|3yr_ri", "max_variance_pct": <number>},
  "constraints": [
    {"id": "c1", "subject": "<server name or *>", "predicate": "<one of the vocabulary>",
     "value": <number or string>, "source": "<literal quote from the business case>"}
  ],
  "scope": {"excluded": []},
  "steps": ["precheck", "interpret", "replicate", "test", "cutover", "parity", "finops"],
  "feature_flags": {"lza_enabled": false},
  "allowed_actions": ["restart_replication_agent", "resync_volume",
                      "increase_replication_timeout", "reinstall_mgn_agent"]
}"""


def _prompt(business_case: str, errors: list[str]) -> str:
    parts = [f"BUSINESS CASE:\n{business_case}", f"SCHEMA:\n{CONTRACT_SCHEMA}"]
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
