from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from state.models import DecisionLogEntry
from state.store import StateStore

APPROVE_DERIVED_CONTRACT = "approve_derived_contract"


@dataclass
class HitlTask:
    wave_id: str
    kind: str
    payload: dict
    status: str = "PENDING"  # PENDING | APPROVED | REJECTED


class InMemoryHitlQueue:
    """Local stand-in for the Transform MCP's `submit_hitl_task`, for decoupled runs.
    The only programmed human gate is approving the derived contract."""

    def __init__(self, store: StateStore, clock: Callable[[], str]) -> None:
        self.store = store
        self._clock = clock
        self._tasks: list[HitlTask] = []

    def submit(self, wave_id: str, kind: str, payload: dict) -> HitlTask:
        task = HitlTask(wave_id=wave_id, kind=kind, payload=payload)
        self._tasks.append(task)
        self.store.append_decision(
            DecisionLogEntry(
                wave_id=wave_id,
                ts=self._clock(),
                actor="system",
                kind="hitl",
                summary=f"pending human task: {kind}",
                detail=payload,
            )
        )
        return task

    def pending(self) -> list[HitlTask]:
        return [t for t in self._tasks if t.status == "PENDING"]

    def latest(self, wave_id: str, kind: str) -> HitlTask | None:
        matches = [t for t in self._tasks if t.wave_id == wave_id and t.kind == kind]
        return matches[-1] if matches else None

    def resolve(self, task: HitlTask, approved: bool) -> HitlTask:
        task.status = "APPROVED" if approved else "REJECTED"
        self.store.append_decision(
            DecisionLogEntry(
                wave_id=task.wave_id,
                ts=self._clock(),
                actor="human",
                kind="hitl",
                summary=f"task {task.kind}: {task.status}",
                detail=task.payload,
            )
        )
        return task
