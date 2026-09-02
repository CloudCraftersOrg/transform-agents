from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

DecisionKind = Literal[
    "decision",
    "escalation",
    "policy_denial",
    "rollback",
    "finops_variance",
    "hitl",
    "interpreter_convergence",
    "remediation_outcome",
    "modernization_artifact",
]


class WaveStatus(StrEnum):
    PENDING = "PENDING"
    PRECHECK = "PRECHECK"
    INTERPRETING = "INTERPRETING"
    REPLICATING = "REPLICATING"
    TESTING = "TESTING"
    CUTTING_OVER = "CUTTING_OVER"
    PARITY_CHECK = "PARITY_CHECK"
    FINOPS = "FINOPS"
    DONE = "DONE"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


class WaveState(BaseModel):
    wave_id: str
    apps: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    status: WaveStatus = WaveStatus.PENDING
    current_step: str | None = None
    completed_steps: list[str] = Field(default_factory=list)
    updated_at: str | None = None


class DecisionLogEntry(BaseModel):
    wave_id: str
    ts: str
    actor: str
    kind: DecisionKind
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)
