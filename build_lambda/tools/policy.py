from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from tools.spec import Constraint, DecisionContract

ActionType = Literal[
    "recommend_instance",
    "modernize",
    "dispatch_step",
    "set_sizing_basis",
    "estimate_cost",
]


@dataclass(frozen=True)
class Action:
    type: ActionType
    subject: str = "*"
    arch: str | None = None
    vcpu: int | None = None
    ram_gib: float | None = None
    sizing_basis: str | None = None
    modernization_target: str | None = None
    est_monthly_usd: float | None = None
    step: str | None = None
    window: str | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    escalate: bool = False

    def __bool__(self) -> bool:
        return self.allowed


def _cite(c: Constraint) -> str:
    return f"[{c.id}] {c.predicate}={c.value!r} :: {c.source}"


def evaluate_policy(action: Action, contract: DecisionContract) -> Decision:
    reasons: list[str] = []
    escalate = False

    for c in contract.constraints_for(action.subject):
        p = c.predicate
        if p == "min_vcpu" and action.vcpu is not None and action.vcpu < int(c.value):
            reasons.append(f"vcpu {action.vcpu} < minimum {c.value} - {_cite(c)}")
        elif p == "min_ram_gib" and action.ram_gib is not None and action.ram_gib < float(c.value):
            reasons.append(f"ram {action.ram_gib} GiB < minimum {c.value} - {_cite(c)}")
        elif p == "arch_not_allowed" and action.arch is not None and action.arch == c.value:
            reasons.append(f"architecture {action.arch} not allowed - {_cite(c)}")
        elif p == "sizing_basis" and action.sizing_basis is not None and action.sizing_basis != c.value:
            reasons.append(f"sizing basis {action.sizing_basis!r} != required {c.value!r} - {_cite(c)}")
        elif (
            p == "modernization_unsupported"
            and action.modernization_target is not None
            and action.modernization_target == c.value
        ):
            reasons.append(f"modernization of {action.modernization_target!r} not supported - {_cite(c)}")
            escalate = True
        elif p == "out_of_scope" and c.subject not in ("*",) and action.subject == c.subject:
            reasons.append(f"resource out of scope - {_cite(c)}")
        elif p == "blackout_window" and action.window is not None and action.window == c.value:
            reasons.append(f"blackout window {action.window!r} - {_cite(c)}")

    if action.subject in contract.scope.excluded:
        reasons.append(f"{action.subject} is in scope.excluded")

    if action.est_monthly_usd is not None:
        limit = contract.budget.ceiling_with_variance
        if action.est_monthly_usd > limit:
            reasons.append(
                f"estimated cost ${action.est_monthly_usd:.2f} > ceiling+variance ${limit:.2f} "
                f"(ceiling ${contract.budget.ceiling_monthly_usd:.2f}, "
                f"variance {contract.budget.max_variance_pct:.0f}%)"
            )

    return Decision(allowed=not reasons, reasons=reasons, escalate=escalate)
