from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Closed vocabulary. A new predicate is added here; the orchestrator is never rewritten.
PREDICATES = frozenset({
    "min_vcpu",
    "min_ram_gib",
    "arch_not_allowed",
    "modernization_unsupported",
    "blackout_window",
    "out_of_scope",
    "sizing_basis",
})

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT_PATH = REPO_ROOT / "fixtures" / "fbctf" / "decision_contract.json"


class SizingBasis(StrEnum):
    PEAK_WITH_HEADROOM = "peak_with_headroom"
    AVERAGE = "average"


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ceiling_monthly_usd: float = Field(gt=0)
    basis: str
    max_variance_pct: float = Field(default=0.0, ge=0)

    @property
    def ceiling_with_variance(self) -> float:
        return self.ceiling_monthly_usd * (1 + self.max_variance_pct / 100)


class Constraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    subject: str
    predicate: str
    value: Any
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def _known_predicate(self) -> Constraint:
        if self.predicate not in PREDICATES:
            raise ValueError(f"unknown predicate {self.predicate!r}; extend PREDICATES first")
        return self


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    excluded: list[str] = Field(default_factory=list)


class FeatureFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lza_enabled: bool = False


class DecisionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    source_ref: str
    budget: Budget
    constraints: list[Constraint] = Field(default_factory=list)
    scope: Scope = Field(default_factory=Scope)
    steps: list[str]
    feature_flags: FeatureFlags = Field(default_factory=FeatureFlags)
    allowed_actions: list[str] = Field(default_factory=list)  # Remediation's allow-list

    def constraints_for(self, subject: str) -> list[Constraint]:
        return [c for c in self.constraints if c.subject in ("*", subject)]


def load_contract(path: str | Path = DEFAULT_CONTRACT_PATH) -> DecisionContract:
    return DecisionContract.model_validate_json(Path(path).read_text(encoding="utf-8"))
