from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from state.models import DecisionLogEntry
from state.store import StateStore

Notifier = Callable[[DecisionLogEntry], None]


@dataclass(frozen=True)
class Escalation:
    wave_id: str
    context: str
    hypothesis: str
    attempts: int


def escalate(
    store: StateStore, ts: str, esc: Escalation, notify: Notifier | None = None
) -> DecisionLogEntry:
    """The 'I'm stuck' exit: writes to the decision_log and notifies. Every escalation is a
    scorecard data point, not an error."""
    entry = DecisionLogEntry(
        wave_id=esc.wave_id,
        ts=ts,
        actor="agent",
        kind="escalation",
        summary=f"stuck after {esc.attempts} attempt(s): {esc.context}",
        detail={"hypothesis": esc.hypothesis, "attempts": esc.attempts, "context": esc.context},
    )
    store.append_decision(entry)
    if notify is not None:
        notify(entry)
    return entry
