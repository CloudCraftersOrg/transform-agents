from __future__ import annotations

import json
from dataclasses import dataclass, field

from agents.model import ModelLike, strip_code_fence
from tools.finops import monthly_run_rate
from tools.spec import DecisionContract

SYSTEM_PROMPT = (
    "You are FinOps. You validate the actual run-rate at cutover against the baseline and "
    "attribute root cause to any deviation. If the spend is not attributable, you report it as a "
    "finding instead of fabricating an attribution."
)

TOOLS = ["run_rate_actual", "cost_explorer", "cloudwatch_metrics", "read_spec"]


@dataclass
class Finding:
    within_budget: bool
    actual_usd: float
    baseline_usd: float
    variance_pct: float
    unattributable: bool = False
    attribution: str | None = None
    notes: list[str] = field(default_factory=list)


def evaluate_deviation(
    model: ModelLike, *, contract: DecisionContract, launched_resources: list[dict]
) -> Finding:
    baseline = contract.budget.ceiling_monthly_usd
    if not launched_resources:
        return Finding(
            True, 0.0, baseline, 0.0, unattributable=True,
            notes=["no launched resources reported; run-rate not computed"],
        )
    actual = monthly_run_rate(launched_resources)
    variance = round((actual - baseline) / baseline * 100, 1) if baseline else 0.0

    if actual <= contract.budget.ceiling_with_variance:
        return Finding(True, actual, baseline, variance, notes=["within ceiling+variance"])

    if any(not r.get("tags") for r in launched_resources):
        return Finding(
            False, actual, baseline, variance, unattributable=True,
            notes=["EC2/EBS untagged: the deviation is not attributable by component"],
        )

    prompt = (
        f"BASELINE ${baseline}/month. ACTUAL ${actual}/month ({variance:+.1f}%).\n"
        f"LAUNCHED RESOURCES: {json.dumps(launched_resources, ensure_ascii=False)}\n"
        'Attribute the root cause. Respond with JSON {"attribution": ".."}.'
    )
    raw = strip_code_fence(model.complete(prompt, system=SYSTEM_PROMPT))
    try:
        attribution = str(json.loads(raw)["attribution"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return Finding(
            False, actual, baseline, variance, unattributable=True,
            notes=["illegible attribution; not fabricating a cause"],
        )
    return Finding(False, actual, baseline, variance, attribution=attribution)


def build_finops(model: ModelLike, contract: DecisionContract):
    def delegate_finops(objective: str) -> Finding:
        p = json.loads(objective)
        return evaluate_deviation(model, contract=contract, launched_resources=p.get("resources", []))

    return delegate_finops
