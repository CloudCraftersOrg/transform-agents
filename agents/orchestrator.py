from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum, StrEnum

from agents.finops import build_finops, evaluate_deviation
from agents.interpreter import build_interpreter
from agents.model import ModelLike
from agents.remediation import build_remediation
from agents.validation import build_validation, issue_verdict
from dispatcher.steps import Dispatcher, StepContext, StepRejected
from state.models import DecisionLogEntry, WaveState, WaveStatus
from state.store import StateStore
from state.transitions import assert_legal
from tools.escalation import Escalation, escalate
from tools.hitl import APPROVE_DERIVED_CONTRACT, InMemoryHitlQueue
from tools.policy import Action, Decision, evaluate_policy
from tools.runbooks import InMemoryRunbookKB
from tools.spec import DecisionContract

SYSTEM_PROMPT = (
    "You are the Orchestrator. You decide what step comes next and with what authority. You have "
    "no direct AWS APIs. Before dispatching any step you call evaluate_policy. The wave carries "
    "its step list from the specification: you never reason about feature flags or know they exist."
)

TOOL_CATALOG = {
    "delegate_interpreter": "generate/regenerate valid configuration (LZA, IaC) until it passes the validator",
    "delegate_remediation": "diagnose and resolve an infrastructure failure never seen before",
    "delegate_validation": "decide what to test, interpret diffs, issue a green/red verdict, trigger rollback",
    "delegate_finops": "attribute root cause to a run-rate deviation; report if the cost is not attributable",
    "evaluate_policy": "check a proposed action against the decision contract before dispatching",
    "wave_status": "aggregated digest of the wave's progress: replicating, ready, failed",
    "execute_step": "dispatch a deterministic wave step (start_replication, cutover, rollback, finalize)",
    "submit_hitl_task": "request human approval; the only gate: the derived contract",
    "escalate": "report exhausted iterations or that the tool does not cover the case",
}

TOOLS = list(TOOL_CATALOG)

_STEP_STATUS = {
    "precheck": WaveStatus.PRECHECK,
    "interpret": WaveStatus.INTERPRETING,
    "replicate": WaveStatus.REPLICATING,
    "test": WaveStatus.TESTING,
    "cutover": WaveStatus.CUTTING_OVER,
    "parity": WaveStatus.PARITY_CHECK,
    "finops": WaveStatus.FINOPS,
}


class WaveOutcome(StrEnum):
    WAITING = "WAITING"  # long wait (replication) or human gate: re-invoke later
    ROLLED_BACK = "ROLLED_BACK"
    ESCALATED = "ESCALATED"
    DONE = "DONE"


class _Signal(Enum):
    CONTINUE = 1
    WAIT = 2
    ROLLBACK = 3
    ESCALATE = 4


@dataclass
class WaveInputs:
    interpret_objective: str = "minimal landing zone for the wave"
    test_spec: str = ""
    test_diffs: dict = field(default_factory=dict)
    test_criticality: str = "medium"
    parity_diffs: dict = field(default_factory=dict)
    finops_resources: list[dict] = field(default_factory=list)


def _route_system() -> str:
    catalog = "\n".join(f"- {n}: {d}" for n, d in TOOL_CATALOG.items())
    return (
        "You are the Orchestrator. Pick ONE tool for the described situation.\n"
        f"Tools:\n{catalog}\nReply with only the exact name."
    )


def route(model: ModelLike, situation: str) -> str:
    """Agentic surface: given a situation, pick a tool from the catalog."""
    raw = model.complete(situation, system=_route_system()).strip().lower()
    for name in TOOL_CATALOG:
        if name in raw:
            return name
    return raw.split()[0] if raw else ""


@dataclass
class Specialists:
    interpreter: Callable[[str], object] | None = None
    remediation: Callable[[str], object] | None = None
    validation: Callable[[str], object] | None = None
    finops: Callable[[str], object] | None = None


class Orchestrator:
    """Decides what step comes next and with what authority. No direct AWS APIs: only store,
    dispatcher, HITL and the specialists (agent-as-tool). Every decision is written to the
    decision_log."""

    def __init__(
        self,
        model: ModelLike,
        contract: DecisionContract,
        *,
        store: StateStore,
        dispatcher: Dispatcher,
        hitl: InMemoryHitlQueue,
        specialists: Specialists,
        clock: Callable[[], str],
        runbook_kb: InMemoryRunbookKB | None = None,
    ) -> None:
        self.model = model
        self.contract = contract
        self.store = store
        self.dispatcher = dispatcher
        self.hitl = hitl
        self.specialists = specialists
        self.clock = clock
        self.runbook_kb = runbook_kb

    def _log(self, wave_id: str, kind: str, summary: str, detail: dict | None = None) -> None:
        self.store.append_decision(
            DecisionLogEntry(
                wave_id=wave_id or "-",
                ts=self.clock(),
                actor="orchestrator",
                kind=kind,
                summary=summary,
                detail=detail or {},
            )
        )

    def decide(self, situation: str, wave_id: str = "") -> str:
        tool = route(self.model, situation)
        self._log(wave_id, "decision", f"situation -> {tool}", {"situation": situation, "tool": tool})
        return tool

    def check(self, action: Action, wave_id: str = "") -> Decision:
        decision = evaluate_policy(action, self.contract)
        if decision.allowed:
            return decision
        self._log(wave_id, "policy_denial", "; ".join(decision.reasons), {"action": asdict(action)})
        if decision.escalate:
            self.escalate(
                wave_id,
                context="; ".join(decision.reasons),
                hypothesis="the tool does not cover this case; report it as a scope limit",
                attempts=0,
            )
        return decision

    def transition(self, wave_id: str, target: WaveStatus) -> WaveState:
        wave = self.store.get_wave(wave_id)
        if wave is None:
            raise KeyError(wave_id)
        if wave.status != target:
            assert_legal(wave.status, target)
            wave.status = target
        self.store.put_wave(wave)
        return wave

    def advance(self, wave_id: str, step: str) -> WaveState:
        target = _STEP_STATUS.get(step)
        if target is not None:
            self.transition(wave_id, target)
        wave = self.store.get_wave(wave_id)
        wave.current_step = step
        self.store.put_wave(wave)
        return wave

    def dispatch(
        self,
        wave_id: str,
        step_id: str,
        name: str,
        *,
        guard: Action | None = None,
        params: dict | None = None,
    ) -> dict:
        ctx = StepContext(wave_id=wave_id, step_id=step_id, contract=self.contract, params=params or {})
        result = self.dispatcher.dispatch(name, ctx, guard=guard)
        self._log(wave_id, "decision", f"dispatched {name}", {"step_id": step_id, "result": result})
        return result

    def delegate(self, which: str, objective: str, wave_id: str = "") -> object:
        fn: Callable[[str], object] | None = getattr(self.specialists, which, None)
        if fn is None:
            raise NotImplementedError(f"specialist {which!r} not wired yet (Sprint 2)")
        out = fn(objective)
        self._log(wave_id, "decision", f"delegated to {which}", {"objective": objective})
        return out

    def request_approval(self, wave_id: str, payload: dict):
        return self.hitl.submit(wave_id, APPROVE_DERIVED_CONTRACT, payload)

    def escalate(self, wave_id: str, context: str, hypothesis: str, attempts: int) -> DecisionLogEntry:
        return escalate(
            self.store, self.clock(), Escalation(wave_id or "-", context, hypothesis, attempts)
        )

    def wave_digest(self, wave_id: str) -> dict:
        wave = self.store.get_wave(wave_id)
        if wave is None:
            return {"wave_id": wave_id, "status": "UNKNOWN", "completed": [], "remaining": []}
        remaining = [s for s in wave.steps if s not in wave.completed_steps]
        return {
            "wave_id": wave_id,
            "status": wave.status.value,
            "completed": list(wave.completed_steps),
            "remaining": remaining,
        }

    # --- wave loop: resumable, never blocks on long waits -------------------

    def run_wave(
        self,
        wave_id: str,
        inputs: WaveInputs | None = None,
        *,
        replication_ready: Callable[[str], bool] | None = None,
        require_contract_approval: bool = False,
    ) -> WaveOutcome:
        inputs = inputs or WaveInputs()
        ready = replication_ready or (lambda _wid: True)
        wave = self.store.get_wave(wave_id)
        if wave is None:
            wave = WaveState(wave_id=wave_id, steps=list(self.contract.steps))
            self.store.put_wave(wave)

        if require_contract_approval:
            gate = self._contract_gate(wave_id)
            if gate is not None:
                return gate

        for step in wave.steps:
            if step in wave.completed_steps:
                continue
            try:
                signal = self._run_step(wave_id, step, inputs, ready)
            except StepRejected as e:
                self._escalate_step(wave_id, f"step {step} rejected by server-side policy", str(e))
                return WaveOutcome.ESCALATED
            if signal is _Signal.WAIT:
                return WaveOutcome.WAITING
            if signal is _Signal.ROLLBACK:
                return WaveOutcome.ROLLED_BACK
            if signal is _Signal.ESCALATE:
                return WaveOutcome.ESCALATED
            wave = self.store.get_wave(wave_id)
            wave.completed_steps.append(step)
            self.store.put_wave(wave)

        self.transition(wave_id, WaveStatus.DONE)
        self._log(wave_id, "decision", "wave completed")
        return WaveOutcome.DONE

    def _contract_gate(self, wave_id: str) -> WaveOutcome | None:
        task = self.hitl.latest(wave_id, APPROVE_DERIVED_CONTRACT)
        if task is None:
            self.request_approval(wave_id, {"case_id": self.contract.case_id})
            return WaveOutcome.WAITING
        if task.status == "PENDING":
            return WaveOutcome.WAITING
        if task.status == "REJECTED":
            self.transition(wave_id, WaveStatus.ESCALATED)
            self.escalate(wave_id, "derived contract rejected by the human", "review the extraction", 0)
            return WaveOutcome.ESCALATED
        return None

    def _run_step(self, wave_id: str, step: str, inputs: WaveInputs, ready) -> _Signal:
        self.advance(wave_id, step)
        handler = getattr(self, f"_step_{step}", None)
        if handler is None:
            self._log(wave_id, "decision", f"step {step} has no handler; skipping")
            return _Signal.CONTINUE
        return handler(wave_id, inputs, ready)

    def _escalate_step(self, wave_id: str, context: str, hypothesis: str) -> _Signal:
        self.transition(wave_id, WaveStatus.ESCALATED)
        self.escalate(wave_id, context, hypothesis, 0)
        return _Signal.ESCALATE

    def _verdict_step(self, wave_id: str, step: str, spec: str, diffs: dict, criticality: str) -> _Signal:
        rolled = {"v": False}

        def request_rollback(reason: str) -> None:
            rolled["v"] = True
            self._log(wave_id, "rollback", f"{step}: {reason}")
            self.transition(wave_id, WaveStatus.ROLLING_BACK)
            self.dispatch(wave_id, f"{step}-rollback", "rollback")
            self.transition(wave_id, WaveStatus.ROLLED_BACK)

        verdict = issue_verdict(
            self.model, spec=spec, diffs=diffs, criticality=criticality,
            request_rollback=request_rollback,
        )
        self._log(
            wave_id, "decision", f"{step}: {'green' if verdict.ok else 'red'} verdict",
            {"reasons": verdict.reasons},
        )
        return _Signal.ROLLBACK if rolled["v"] else _Signal.CONTINUE

    def _step_precheck(self, wave_id, inputs, ready) -> _Signal:
        if not self.contract.steps:
            return self._escalate_step(wave_id, "contract has no steps", "incomplete extraction")
        return _Signal.CONTINUE

    def _step_interpret(self, wave_id, inputs, ready) -> _Signal:
        result = self.delegate("interpreter", inputs.interpret_objective, wave_id=wave_id)
        ok = getattr(result, "ok", False)
        iterations = getattr(result, "iterations", None)
        self._log(
            wave_id, "interpreter_convergence",
            f"interpreter {'converged' if ok else 'did not converge'} in {iterations} iteration(s)",
            {"ok": ok, "iterations": iterations},
        )
        if not ok:
            return self._escalate_step(
                wave_id, "the Interpreter did not converge", "invalid config after the iteration budget"
            )
        return _Signal.CONTINUE

    def _step_replicate(self, wave_id, inputs, ready) -> _Signal:
        result = self.dispatch(
            wave_id, "replicate", "start_replication",
            guard=Action("dispatch_step", step="replicate"),
        )
        if result.get("error"):
            rem = self.delegate("remediation", json.dumps({"failure": result["error"]}), wave_id=wave_id)
            resolved = getattr(rem, "resolved", False)
            self._log(
                wave_id, "remediation_outcome",
                f"remediation {'resolved' if resolved else 'did not resolve'} the failure",
                {
                    "resolved": resolved,
                    "from_runbook": getattr(rem, "from_runbook", False),
                    "attempts": getattr(rem, "attempts", None),
                    "failure": str(result["error"]),
                },
            )
            if not resolved:
                return self._escalate_step(wave_id, "replication failed and was not remediated", str(result["error"]))
        if ready(wave_id):
            return _Signal.CONTINUE
        self._log(wave_id, "decision", "replicating; will re-invoke once it finishes")
        return _Signal.WAIT

    def _step_test(self, wave_id, inputs, ready) -> _Signal:
        return self._verdict_step(
            wave_id, "test", inputs.test_spec, inputs.test_diffs, inputs.test_criticality
        )

    def _step_cutover(self, wave_id, inputs, ready) -> _Signal:
        action = Action("dispatch_step", step="cutover")
        decision = self.check(action, wave_id=wave_id)
        if not decision.allowed:
            if decision.escalate:
                return _Signal.ESCALATE
            return self._escalate_step(
                wave_id, "cutover blocked by policy", "; ".join(decision.reasons)
            )
        self.dispatch(wave_id, "cutover", "cutover", guard=action)
        return _Signal.CONTINUE

    def _step_parity(self, wave_id, inputs, ready) -> _Signal:
        return self._verdict_step(wave_id, "parity", inputs.test_spec, inputs.parity_diffs, "high")

    def _step_finops(self, wave_id, inputs, ready) -> _Signal:
        finding = evaluate_deviation(
            self.model, contract=self.contract, launched_resources=inputs.finops_resources
        )
        self._log(
            wave_id, "finops_variance",
            f"run-rate ${finding.actual_usd} vs baseline ${finding.baseline_usd} "
            f"({finding.variance_pct:+.1f}%)",
            {
                "within_budget": finding.within_budget,
                "unattributable": finding.unattributable,
                "attribution": finding.attribution,
                "notes": finding.notes,
            },
        )
        return _Signal.CONTINUE


def build_orchestrator(
    model: ModelLike,
    contract: DecisionContract,
    *,
    store: StateStore,
    dispatcher: Dispatcher,
    hitl: InMemoryHitlQueue,
    clock: Callable[[], str],
    specialists: Specialists | None = None,
    runbook_kb: InMemoryRunbookKB | None = None,
) -> Orchestrator:
    kb = runbook_kb if runbook_kb is not None else InMemoryRunbookKB()
    return Orchestrator(
        model,
        contract,
        store=store,
        dispatcher=dispatcher,
        hitl=hitl,
        specialists=specialists
        or Specialists(
            interpreter=build_interpreter(model),
            remediation=build_remediation(model, contract, kb=kb),
            validation=build_validation(model),
            finops=build_finops(model, contract),
        ),
        clock=clock,
        runbook_kb=kb,
    )
