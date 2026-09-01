from __future__ import annotations

import sys
from pathlib import Path

from agents.model import FakeModel
from agents.orchestrator import WaveInputs, build_orchestrator
from agents.remediation import remediate
from dispatcher.steps import Dispatcher
from evals.scorecard import build_scorecard
from state.store import InMemoryStateStore
from tools.hitl import InMemoryHitlQueue
from tools.runbooks import InMemoryRunbookKB
from tools.spec import load_contract

_VALID_LZA = (
    Path(__file__).resolve().parents[1] / "fixtures" / "lza" / "valid_config.yaml"
).read_text(encoding="utf-8")


def _fake_model() -> FakeModel:
    def respond(prompt: str) -> str:
        if '"verdict"' in prompt:
            return '{"verdict":"green","reasons":[]}'
        if '"attribution"' in prompt:
            return '{"attribution":"EBS oversized in billing"}'
        return _VALID_LZA

    return FakeModel(respond)


def main() -> int:
    contract = load_contract()
    store = InMemoryStateStore()
    disp = Dispatcher()
    for name in ("start_replication", "cutover", "rollback"):
        disp.register(name, lambda ctx, n=name: {n: "ok"})
    hitl = InMemoryHitlQueue(store, lambda: "t0")
    orq = build_orchestrator(
        _fake_model(), contract, store=store, dispatcher=disp, hitl=hitl, clock=lambda: "t0"
    )

    outcome = orq.run_wave(
        "w1",
        WaveInputs(
            finops_resources=[{"instance_type": "m5.large", "ebs_gib": 2500, "tags": {"app": "billing"}}]
        ),
    )
    print(f"outcome: {outcome}\n")
    for e in store.decisions("w1"):
        extra = f"  {e.detail}" if e.detail else ""
        print(f"[{e.kind}] {e.summary}{extra}")

    print("\n--- Remediation learning loop ---")
    _demo_learning_loop()

    print("\n--- Autonomy scorecard (this run only - one sample, illustrative) ---")
    sc = build_scorecard(store, ["w1"], contract=contract, known_healthy={"w1": True})
    print(f"unintervened success rate: {sc.unintervened_success_rate:.0%}")
    print(f"escalations per run: {sc.escalations_per_run:.2f}")
    print(f"interpreter convergence: {sc.interpreter.rate:.0%} (avg {sc.interpreter.avg_iterations} iterations)")
    print(f"constraint adherence: {sc.constraint_adherence.rate:.0%} ({sc.constraint_adherence.avoided}/{sc.constraint_adherence.checked})")
    print(f"false green / false red: {sc.false_verdicts.false_green} / {sc.false_verdicts.false_red}")
    for note in sc.notes:
        print(f"  note: {note}")
    return 0


def _demo_learning_loop() -> None:
    contract = load_contract()
    kb = InMemoryRunbookKB()

    def apply_action(action: str) -> dict:
        return {"resolved": action == "resync_volume"}

    first_model = FakeModel(['{"action":"resync_volume","rationale":"checksum mismatch"}'])
    first = remediate(
        first_model, failure="checksum mismatch on vol-1",
        allowed_actions=contract.allowed_actions, apply_action=apply_action, kb=kb,
    )
    print(f"1st occurrence: resolved={first.resolved} attempts={first.attempts} "
          f"from_runbook={first.from_runbook}")

    second_model = FakeModel(["THE MODEL SHOULD NOT BE CALLED"])
    second = remediate(
        second_model, failure="checksum mismatch on vol-9",
        allowed_actions=contract.allowed_actions, apply_action=apply_action, kb=kb,
    )
    print(f"2nd occurrence: resolved={second.resolved} attempts={second.attempts} "
          f"from_runbook={second.from_runbook} (model called: {len(second_model.prompts)}x)")


if __name__ == "__main__":
    sys.exit(main())
