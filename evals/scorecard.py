from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from agents.model import ModelLike
from state.models import WaveStatus
from state.store import StateStore
from tools.policy import Action, evaluate_policy
from tools.spec import REPO_ROOT, Constraint, DecisionContract

# Section 3 of the plan. Computed from the decision_log a real run already produces - no separate
# telemetry pipeline. Anything that needs labeled ground truth (false green/red, FinOps precision,
# diagnosis precision against injected failures) returns None with a note when that data isn't
# supplied, instead of silently pretending it was measured.


def unintervened_success_rate(store: StateStore, wave_ids: list[str]) -> float:
    if not wave_ids:
        return 0.0
    clean = 0
    for wid in wave_ids:
        wave = store.get_wave(wid)
        escalated = any(e.kind == "escalation" for e in store.decisions(wid))
        if wave is not None and wave.status == WaveStatus.DONE and not escalated:
            clean += 1
    return clean / len(wave_ids)


def escalations_per_run(store: StateStore, wave_ids: list[str]) -> float:
    if not wave_ids:
        return 0.0
    total = sum(sum(e.kind == "escalation" for e in store.decisions(wid)) for wid in wave_ids)
    return total / len(wave_ids)


@dataclass
class ConvergenceStats:
    samples: int
    converged: int
    avg_iterations: float | None

    @property
    def rate(self) -> float:
        return self.converged / self.samples if self.samples else 0.0


def interpreter_convergence(store: StateStore, wave_ids: list[str]) -> ConvergenceStats:
    entries = [
        e for wid in wave_ids for e in store.decisions(wid) if e.kind == "interpreter_convergence"
    ]
    converged = [e for e in entries if e.detail.get("ok")]
    iterations = [e.detail.get("iterations") for e in converged if e.detail.get("iterations") is not None]
    avg = sum(iterations) / len(iterations) if iterations else None
    return ConvergenceStats(len(entries), len(converged), avg)


@dataclass
class RemediationStats:
    total: int
    resolved: int
    escalated: int
    resolved_from_runbook: int

    @property
    def resolution_rate(self) -> float:
        return self.resolved / self.total if self.total else 0.0

    @property
    def learning_reuse_rate(self) -> float:
        """Share of resolved remediations that reused a known runbook instead of reasoning from
        scratch - the operational proxy for 'the system gets better between runs'."""
        return self.resolved_from_runbook / self.resolved if self.resolved else 0.0


def remediation_outcomes(store: StateStore, wave_ids: list[str]) -> RemediationStats:
    entries = [
        e for wid in wave_ids for e in store.decisions(wid) if e.kind == "remediation_outcome"
    ]
    resolved = [e for e in entries if e.detail.get("resolved")]
    from_runbook = [e for e in resolved if e.detail.get("from_runbook")]
    return RemediationStats(len(entries), len(resolved), len(entries) - len(resolved), len(from_runbook))


@dataclass
class FalseVerdicts:
    false_green: int
    false_red: int
    labeled: int


def false_verdicts(
    store: StateStore, wave_ids: list[str], known_healthy: dict[str, bool]
) -> FalseVerdicts:
    """`known_healthy[wave_id] = True` means the migration was actually sound. False green: the
    wave finished DONE on an unsound migration - the single most dangerous metric, target zero.
    False red: rolled back a sound migration - costly, not dangerous."""
    false_green = false_red = labeled = 0
    for wid in wave_ids:
        if wid not in known_healthy:
            continue
        labeled += 1
        wave = store.get_wave(wid)
        if wave is None:
            continue
        if wave.status == WaveStatus.DONE and not known_healthy[wid]:
            false_green += 1
        elif wave.status == WaveStatus.ROLLED_BACK and known_healthy[wid]:
            false_red += 1
    return FalseVerdicts(false_green, false_red, labeled)


def _synthesize_violation(c: Constraint) -> Action | None:
    """Build the smallest action that would violate a single constraint, to check the engine
    actually catches it. Generic over any contract - it reads the constraint, not a fixed workload."""
    if c.predicate == "min_vcpu":
        return Action("recommend_instance", subject=c.subject, vcpu=int(c.value) - 1, ram_gib=1000.0)
    if c.predicate == "min_ram_gib":
        return Action("recommend_instance", subject=c.subject, ram_gib=float(c.value) - 0.5, vcpu=1000)
    if c.predicate == "arch_not_allowed":
        return Action("recommend_instance", subject=c.subject, arch=str(c.value), vcpu=1000, ram_gib=1000.0)
    if c.predicate == "modernization_unsupported":
        return Action("modernize", subject=c.subject, modernization_target=str(c.value))
    if c.predicate == "sizing_basis":
        other = "average" if c.value != "average" else "peak_with_headroom"
        return Action("set_sizing_basis", subject=c.subject, sizing_basis=other)
    if c.predicate == "blackout_window":
        return Action("dispatch_step", subject=c.subject, window=str(c.value))
    if c.predicate == "out_of_scope":
        return Action("dispatch_step", subject=c.subject)
    return None


@dataclass
class AdherenceResult:
    avoided: int
    checked: int
    misses: list[str] = field(default_factory=list)  # constraint ids the engine failed to catch

    @property
    def rate(self) -> float:
        return self.avoided / self.checked if self.checked else 0.0


def constraint_adherence(contract: DecisionContract) -> AdherenceResult:
    """For every constraint, synthesize the trap that violates it and confirm evaluate_policy
    denies it. This is the generalized version of the plan's three FBCTF traps."""
    avoided = 0
    misses: list[str] = []
    checked = 0
    for c in contract.constraints:
        action = _synthesize_violation(c)
        if action is None:
            continue
        checked += 1
        if evaluate_policy(action, contract).allowed:
            misses.append(c.id)
        else:
            avoided += 1
    return AdherenceResult(avoided, checked, misses)


_CATALOG = REPO_ROOT / "fixtures" / "failures" / "catalog.json"


@dataclass
class DiagnosisResult:
    total: int
    correct: int
    details: list[tuple[str, str, str | None, bool]]  # (failure_id, expected, got, correct)

    @property
    def precision(self) -> float:
        return self.correct / self.total if self.total else 0.0


def load_failure_catalog(path: str | Path | None = None) -> list[dict]:
    p = Path(path) if path else _CATALOG
    return json.loads(p.read_text(encoding="utf-8")).get("failures", [])


def diagnosis_precision(
    model: ModelLike, contract: DecisionContract, *, catalog_path: str | Path | None = None
) -> DiagnosisResult | None:
    """Run Remediation against each injected failure and compare its chosen action to the catalog's
    ground truth. `expected_action == "escalate"` means the correct outcome is *not resolving*
    (the Orchestrator then escalates). Returns None if the catalog is empty."""
    from agents.remediation import remediate

    failures = load_failure_catalog(catalog_path)
    if not failures:
        return None
    correct = 0
    details: list[tuple[str, str, str | None, bool]] = []
    for f in failures:
        expected = f["expected_action"]

        def apply_action(a: str, _target: dict, _exp: str = expected) -> dict:
            return {"resolved": a == _exp}

        r = remediate(
            model, failure=f["text"], allowed_actions=contract.allowed_actions,
            apply_action=apply_action,
        )
        if expected == "escalate":
            got, ok = (r.action if r.resolved else "escalate"), (not r.resolved)
        else:
            got, ok = r.action, (r.resolved and r.action == expected)
        correct += int(ok)
        details.append((f["id"], expected, got, ok))
    return DiagnosisResult(len(failures), correct, details)


def cost_per_successful_run(cost_by_wave: dict[str, float], store: StateStore, wave_ids: list[str]) -> float | None:
    successful = [wid for wid in wave_ids if (w := store.get_wave(wid)) and w.status == WaveStatus.DONE]
    known = [cost_by_wave[wid] for wid in successful if wid in cost_by_wave]
    if not known:
        return None
    return sum(known) / len(known)


@dataclass
class GateAutonomy:
    """AWS Transform advances a job through conversational gates. This is the share the agent
    answered itself. `stalled` and `prerequisites_cleared` are counted separately: a stall is the
    workflow blocked on something outside the agent, not a decision it declined to make."""

    advanced_plain: int
    advanced_by_model: int
    escalated: int
    stalled: int
    prerequisites_cleared: int
    escalation_reasons: list[tuple[str, int]] = field(default_factory=list)

    @property
    def decided(self) -> int:
        return self.advanced_plain + self.advanced_by_model

    @property
    def rate(self) -> float:
        total = self.decided + self.escalated
        return self.decided / total if total else 0.0


def gate_autonomy(store: StateStore, wave_ids: list[str]) -> GateAutonomy:
    """Read straight off the decision_log a real run already writes - the summaries are the record,
    so this measures what the agent did, not what it was configured to do."""
    plain = by_model = escalated = stalled = cleared = 0
    reasons: dict[str, int] = {}
    for wid in wave_ids:
        for e in store.decisions(wid):
            s = e.summary
            if "autonomously chose" in s:
                if "model chose the safe default" in s:
                    by_model += 1
                else:
                    plain += 1
            elif e.kind == "hitl" and "a human decision is needed" in s:
                escalated += 1
                why = (e.detail or {}).get("reason") or s
                reasons[why[:100]] = reasons.get(why[:100], 0) + 1
            elif e.kind == "hitl" and "workflow has stalled" in s:
                stalled += 1
            elif e.kind == "remediation_outcome" and "cleared the" in s:
                cleared += 1
    return GateAutonomy(
        advanced_plain=plain, advanced_by_model=by_model, escalated=escalated,
        stalled=stalled, prerequisites_cleared=cleared,
        escalation_reasons=sorted(reasons.items(), key=lambda kv: -kv[1]),
    )


@dataclass
class Scorecard:
    total_runs: int
    unintervened_success_rate: float
    escalations_per_run: float
    gate_autonomy: GateAutonomy
    interpreter: ConvergenceStats
    remediation: RemediationStats
    constraint_adherence: AdherenceResult | None
    false_verdicts: FalseVerdicts | None
    cost_per_successful_run: float | None
    notes: list[str] = field(default_factory=list)


def build_scorecard(
    store: StateStore,
    wave_ids: list[str],
    *,
    contract: DecisionContract | None = None,
    known_healthy: dict[str, bool] | None = None,
    cost_by_wave: dict[str, float] | None = None,
) -> Scorecard:
    notes: list[str] = []

    adherence = constraint_adherence(contract) if contract is not None else None
    if adherence is None:
        notes.append("constraint_adherence: no contract supplied")

    verdicts = false_verdicts(store, wave_ids, known_healthy) if known_healthy else None
    if verdicts is None:
        notes.append("false_verdicts: no known_healthy ground truth supplied")
    elif verdicts.labeled < len(wave_ids):
        notes.append(f"false_verdicts: only {verdicts.labeled}/{len(wave_ids)} runs are labeled")

    cost = cost_per_successful_run(cost_by_wave or {}, store, wave_ids)
    if cost is None:
        notes.append("cost_per_successful_run: no cost data supplied")

    notes.append(
        f"remediation diagnosis precision: call diagnosis_precision(model, contract) - "
        f"{len(load_failure_catalog())} injected failures in the catalog"
    )

    return Scorecard(
        total_runs=len(wave_ids),
        unintervened_success_rate=unintervened_success_rate(store, wave_ids),
        escalations_per_run=escalations_per_run(store, wave_ids),
        gate_autonomy=gate_autonomy(store, wave_ids),
        interpreter=interpreter_convergence(store, wave_ids),
        remediation=remediation_outcomes(store, wave_ids),
        constraint_adherence=adherence,
        false_verdicts=verdicts,
        cost_per_successful_run=cost,
        notes=notes,
    )
