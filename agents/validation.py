from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from agents.model import ModelLike, strip_code_fence

SYSTEM_PROMPT = (
    "You are Validation QA. You decide what to test based on criticality and interpret whether a "
    "diff matters. You issue a green/red verdict against the specification. When in doubt, red: "
    "a false green is the worst possible mistake."
)

TOOLS = ["playwright_run", "api_diff", "schema_diff", "read_spec", "request_rollback"]

Rollback = Callable[[str], object]


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    rollback_requested: bool = False


def issue_verdict(
    model: ModelLike,
    *,
    spec: str,
    diffs: dict,
    criticality: str,
    request_rollback: Rollback | None = None,
) -> Verdict:
    prompt = (
        f"CRITICALITY: {criticality}\nSPECIFICATION:\n{spec}\n\n"
        f"OBSERVED DIFFS:\n{json.dumps(diffs, ensure_ascii=False)}\n\n"
        'Respond with JSON {"verdict": "green"|"red", "reasons": [".."]}.'
    )
    raw = strip_code_fence(model.complete(prompt, system=SYSTEM_PROMPT))
    try:
        data = json.loads(raw)
        green = str(data.get("verdict", "")).lower() == "green"
        reasons = [str(x) for x in data.get("reasons", [])]
    except (json.JSONDecodeError, AttributeError, TypeError):
        green, reasons = False, ["illegible verdict from the model; defaulting to red"]

    verdict = Verdict(ok=green, reasons=reasons)
    if not green and request_rollback is not None:
        request_rollback("; ".join(reasons) or "red verdict")
        verdict.rollback_requested = True
    return verdict


def build_validation(model: ModelLike, *, request_rollback: Rollback | None = None):
    def delegate_validation(objective: str) -> Verdict:
        p = json.loads(objective)
        return issue_verdict(
            model,
            spec=p.get("spec", ""),
            diffs=p.get("diffs", {}),
            criticality=p.get("criticality", "medium"),
            request_rollback=request_rollback,
        )

    return delegate_validation
