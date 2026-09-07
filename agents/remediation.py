from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from agents.model import ModelLike, strip_code_fence
from tools.runbooks import InMemoryRunbookKB, Runbook

SYSTEM_PROMPT = (
    "You are Remediation. You diagnose a failure you've never seen and pick ONE action from the "
    "allow-list to resolve it. Once retries are exhausted, you escalate with a hypothesis. Every "
    "resolution is written as a runbook."
)

TOOLS = [
    "mgn_source_server",
    "mgn_jobs",
    "ssm_run_command",
    "cloudwatch_logs",
    "query_runbooks",
    "write_runbook",
]

# (action, target) -> result. The target comes from the wave - which source server, which
# instance - never from the model, which only chooses among the allow-listed action names.
Apply = Callable[[str, dict], dict]


@dataclass
class Remediation:
    resolved: bool
    action: str | None
    attempts: int
    runbook: str | None = None
    from_runbook: bool = False  # resolved via a known precedent, without calling the model
    notes: list[str] = field(default_factory=list)


def _noop_apply(_action: str, _target: dict) -> dict:
    """The default when nothing is wired. It reports honestly rather than claiming success -
    a remediation loop that always answers "done" is worse than one that cannot act."""
    return {"resolved": False, "detail": "no executor wired"}


def remediate(
    model: ModelLike,
    *,
    failure: str,
    allowed_actions: Sequence[str],
    apply_action: Apply = _noop_apply,
    kb: InMemoryRunbookKB | None = None,
    max_retries: int = 3,
    target: dict | None = None,
) -> Remediation:
    notes: list[str] = []
    known = kb.query(failure) if kb is not None else []

    # learning loop: if this failure class has been seen before, apply the precedent without reasoning
    for i, rb in enumerate(known, start=1):
        if rb.action not in allowed_actions:
            continue
        result = apply_action(rb.action, target or {})
        if result.get("resolved"):
            return Remediation(True, rb.action, i, from_runbook=True, notes=notes)
        notes.append(f"known runbook {i}: {rb.action} did not resolve it")

    base = len(notes)
    for attempt in range(1, max_retries + 1):
        prompt = (
            f"FAILURE:\n{failure}\n\nKNOWN RUNBOOKS (did not resolve it or do not apply): "
            f"{[rb.action for rb in known]}\nALLOWED ACTIONS: {list(allowed_actions)}\n"
            f"PREVIOUS ATTEMPTS: {notes}\n"
            'Respond with JSON {"action": "..", "rationale": ".."}.'
        )
        raw = strip_code_fence(model.complete(prompt, system=SYSTEM_PROMPT))
        try:
            action = str(json.loads(raw)["action"])
        except (json.JSONDecodeError, KeyError, TypeError):
            notes.append(f"attempt {attempt}: illegible response")
            continue
        if action not in allowed_actions:  # allow-list is enforced in code, not in the prompt
            notes.append(f"attempt {attempt}: {action!r} is outside the allow-list")
            continue
        result = apply_action(action, target or {})
        if result.get("resolved"):
            if kb is not None:
                kb.write(Runbook(failure=failure, action=action))
            return Remediation(
                True, action, base + attempt, runbook=f"{failure[:80]} -> {action}", notes=notes
            )
        notes.append(f"attempt {attempt}: {action} did not resolve it ({result.get('detail', '')})")
    return Remediation(False, None, base + max_retries, notes=notes)


def build_remediation(
    model: ModelLike, contract, *, apply_action: Apply = _noop_apply, kb: InMemoryRunbookKB | None = None
):
    def delegate_remediation(objective: str) -> Remediation:
        p = json.loads(objective)
        return remediate(
            model,
            failure=p["failure"],
            allowed_actions=contract.allowed_actions,
            apply_action=apply_action,
            kb=kb,
            max_retries=p.get("max_retries", 3),
            target=p.get("target") or {},
        )

    return delegate_remediation
