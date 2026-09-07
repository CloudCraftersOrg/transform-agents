from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum, StrEnum

from agents.finops import build_finops, evaluate_deviation
from agents.interpreter import build_interpreter, generate_modernization_iac
from agents.model import ModelLike
from agents.remediation import build_remediation
from agents.validation import build_validation, issue_verdict
from dispatcher.steps import Dispatcher, StepContext, StepRejected
from state.models import DecisionLogEntry, WaveState, WaveStatus
from state.store import StateStore
from state.transitions import IllegalTransition, assert_legal
from tools.escalation import Escalation, escalate
from tools.hitl import APPROVE_DERIVED_CONTRACT, InMemoryHitlQueue
from tools.policy import Action, Decision, evaluate_policy
from tools.runbooks import InMemoryRunbookKB
from tools.spec import DecisionContract
from tools.trace import trace
from tools.transform_mcp import TransformBusy, TransformError

# AWS Transform advances a job conversationally, so the Orchestrator has to talk to it. It drives
# the workflow autonomously: at each gate it either picks the option that moves forward or, if the
# gate is a genuine judgement call, escalates. The turn budget bounds one invocation; a WAIT just
# means "re-invoke", and run_wave resumes.
MAX_CHAT_TURNS = 6

# Substrings (case-insensitive) that mark an option as plain workflow progression - accepting a
# default, confirming a review, moving to the next phase. Picking one of these is not a decision.
ADVANCE_OPTIONS = (
    "proceed", "continue", "confirm", "keep these", "keep current", "use these",
    "use default", "use recommended", "looks good", "accept", "go ahead", "yes, ",
    "continue rehost", "start wave", "begin wave", "load inventory", "next", "approve settings",
)

# If any of these appear in an option or in the prompt that precedes it, the agent must not pick
# it on its own - a person has to weigh it. Irreversible or destructive actions, and skipping a
# safety step, are never autonomous.
STOP_SIGNALS = (
    "delete", "terminate", "permanently", "destroy", "wipe", "overwrite",
    "skip test", "skip validation", "skip the test", "without testing", "force cutover",
    "cannot be undone", "irreversible", "no rollback", "data loss",
)

# Statuses in which the job is alive and waiting on the chat rather than on itself.
DRIVABLE_STATUSES = ("EXECUTING", "AWAITING_HUMAN_INPUT")

# AWS Transform reports external prerequisites as prose it writes fresh each time, so a blocker is
# matched on the terms that have to co-occur rather than on one phrasing. Each entry maps a
# recognised blocker to the step that clears it and the phrase that resumes the job.
BLOCKERS = (
    (("mgn", "not initialized"), "initialize_mgn", "continue rehost"),
    (("mgn", "needs to be initialized"), "initialize_mgn", "continue rehost"),
)

# Prerequisites only a person with console access can satisfy. The agent recognises them so it
# escalates with a useful record instead of blindly replying "continue" on work that is not done.
MANUAL_BLOCKERS = (
    (("tagged subnet",), "tag a VPC and a subnet for the MGN staging area in the AWS console"),
    (("tagged vpc",), "tag a VPC and a subnet for the MGN staging area in the AWS console"),
    (("no tagged",), "tag the network resources AWS Transform needs in the AWS console"),
)
# Deliberately not here: "install agents, then type Re-check status". Every option that gate offers
# is a pure status re-read, which asserts nothing - refusing it would also refuse the re-check that
# clears the gate once the agents really are there. What made that gate harmful was answering it
# with a claim, and that is caught on the way out by claims_unverifiable_work.

# Work the agent has no tool to perform. Recognising the prerequisite in AWS Transform's wording is
# whack-a-mole - it phrases them differently every time - so the durable rule is about the message
# going out: never assert that physical or console work happened. The agent once replied "Agents
# are being installed as per the instruction" to a gate nobody was working on, which is a lie the
# service then acts on.
UNVERIFIABLE_CLAIMS = (
    "install", "installed", "installing", "configured", "configuring", "provisioned",
    "tagged", "deployed the agent", "set up",
)
ASSERTION_MARKERS = ("is ", "are ", "was ", "were ", "has ", "have ", "been ", "we ", "i ")

def carries_stop_signal(text: str) -> str | None:
    """The phrase that makes a piece of text unsafe to answer autonomously, or None. One
    definition, two callers with different inputs: the wave checks the options AWS Transform is
    offering, the agent tool checks the message it is about to send."""
    low = (text or "").lower()
    return next((s for s in STOP_SIGNALS if s in low), None)


def manual_prerequisite(said: str) -> str | None:
    """What a person has to do before this can be answered honestly, if anything."""
    low = (said or "").lower()
    for terms, need in MANUAL_BLOCKERS:
        if all(t in low for t in terms):
            return need
    return None


# A gate that asks whether a manual step is finished, and an answer that says it is. Together they
# are a claim, even though neither is one alone: "Completed" is an innocuous word until it is the
# reply to "install the agent on your source servers". The agent answered one of these correctly by
# guessing, which is the same as answering the next one wrongly by guessing.
COMPLETION_GATES = (("install", "agent"), ("installation",))
COMPLETION_ANSWERS = ("completed", "complete", "done", "finished", "installed")


def affirms_a_manual_step(pending_text: str, message: str) -> bool:
    low_gate = (pending_text or "").lower()
    if not any(all(t in low_gate for t in terms) for terms in COMPLETION_GATES):
        return False
    low = (message or "").lower().strip(" .!")
    return any(low == a or low.endswith(a) or low.startswith(a) for a in COMPLETION_ANSWERS)


def installation_is_done(mgn: dict, expected: list[str] | None = None) -> tuple[bool, str]:
    """Whether MGN itself shows the agent installed. `expected` is the wave's own hostnames, so a
    stale record for a machine outside the wave neither satisfies the check nor blocks it."""
    servers = (mgn or {}).get("servers") or []
    if not servers:
        return False, "MGN reports no source servers at all"
    wanted = {h.lower() for h in (expected or [])}
    scoped = [s for s in servers
              if not wanted or (s.get("host") or "").lower() in wanted] if wanted else servers
    if wanted and not scoped:
        return False, f"MGN knows none of the {len(wanted)} servers this wave names"
    pending = [s.get("host") for s in scoped if s.get("lifecycle") == "PENDING_INSTALLATION"]
    if pending:
        return False, (f"{len(pending)} of {len(scoped)} still show PENDING_INSTALLATION in MGN: "
                       f"{pending[:6]}")
    return True, f"all {len(scoped)} show an agent registered and past installation in MGN"


def claims_unverifiable_work(message: str) -> str | None:
    """The phrase by which a message asserts work the agent cannot do, or None.

    This is the guard that does not depend on recognising the question. Whatever AWS Transform is
    asking, the agent must never say that an agent was installed, a resource tagged or a machine
    configured - it has no tool that does any of it, so the claim can only be false."""
    low = (message or "").lower()
    for claim in UNVERIFIABLE_CLAIMS:
        at = low.find(claim)
        if at < 0:
            continue
        # A bare imperative ("install the agent") is a request, not a claim. What makes it a claim
        # is a subject and a tense in front of it.
        before = low[max(0, at - 40):at]
        if any(m in before for m in ASSERTION_MARKERS):
            return claim
    return None


RESIZE_OPTIONS = ("recommend", "sizing", "peak", "regenerate", "re-run", "rerun")

# The engineer knows what 'working' looks like for these applications; the agent does not.
# It asks once, before anything moves, and the answer is what the cutover is judged against.
APP_PROBE_SPEC = "app_probe_spec"

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
    # What the deterministic steps need (source_server_ids, hosted_zone_id, record_name, ...).
    # The wave carries them as data; the Orchestrator never builds them.
    step_params: dict = field(default_factory=dict)
    # modernization scenario (deliverable 1): produced as a verified IaC artifact, never applied.
    # empty target -> only the lift-and-shift path runs.
    modernization_target: str = ""
    modernization_subject: str = "*"
    modernization_objective: str = "containerize the workload on ECS/Fargate"
    # AWS Transform runs the containerization itself: name its job and the Orchestrator drives
    # it. Empty (or no workspace wired) -> the Interpreter proposes an artifact instead.
    modernization_job: str = "SourceCodeContainerization"
    # The Transform job that performs the rehost. Set -> the wave watches and gates that
    # job instead of dispatching MGN itself.
    migration_job: str = ""
    # [{name, instance_type, vcpu, ram_gib}] from the plan, so an interaction that would
    # advance the migration can be checked against the contract before it is answered.
    sizing: list[dict] = field(default_factory=list)
    # Gates a human approved on this invocation. The approval is also written to the
    # decision_log, so the Scheduler's later re-invocations see it without the payload.
    approved_gates: list[str] = field(default_factory=list)
    # What to sanity-check, from the engineer: [{name, url, expect_status, expect_contains,
    # secret_arn}]. `secret_arn` is a Secrets Manager reference - a credential itself must never
    # reach the wave inputs or the decision_log, both of which are persisted and rendered.
    app_probes: list[dict] = field(default_factory=list)



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
        workspace=None,
        # Called with every escalation. None means the decision_log is the only record, which is
        # the same as nobody being told.
        notify: Callable[[DecisionLogEntry], None] | None = None,
    ) -> None:
        self.model = model
        self.contract = contract
        self.store = store
        self.dispatcher = dispatcher
        self.hitl = hitl
        self.specialists = specialists
        self.clock = clock
        self.runbook_kb = runbook_kb
        self.workspace = workspace
        self.notify = notify

    def _save(self, wave: WaveState) -> WaveState:
        """Every wave write goes through here so `updated_at` is always current - the
        scheduler orders by it to decide which waves to resume first."""
        wave.updated_at = self.clock()
        self.store.put_wave(wave)
        return wave

    def _log(self, wave_id: str, kind: str, summary: str, detail: dict | None = None) -> None:
        trace(wave_id, kind, summary, detail)
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
        return self._save(wave)

    def advance(self, wave_id: str, step: str) -> WaveState:
        target = _STEP_STATUS.get(step)
        if target is not None:
            self.transition(wave_id, target)
        wave = self.store.get_wave(wave_id)
        wave.current_step = step
        return self._save(wave)

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
        """Hand the wave to a person. Marking the wave is part of escalating, not a step a caller
        can forget: the scheduler skips ESCALATED waves, so an escalation that leaves the wave
        running is not an escalation. Without this, one stalled wave rediscovered the same stall,
        re-published a record and re-escalated on every five-minute tick - 102 rounds deep before
        anyone read the log."""
        self._mark_escalated(wave_id)
        return escalate(
            self.store, self.clock(), Escalation(wave_id or "-", context, hypothesis, attempts),
            notify=self.notify,
        )

    def _mark_escalated(self, wave_id: str) -> None:
        """Best effort by design. An escalation carries a diagnosis a person needs; losing it
        because there is no wave row, or because the wave already finished, would be the worse
        failure."""
        try:
            self.transition(wave_id, WaveStatus.ESCALATED)
        except (KeyError, IllegalTransition) as e:
            self._log(wave_id, "decision",
                      f"escalated with no wave to mark ({type(e).__name__}); the decision log "
                      f"still carries it")

    def _mgn_view(self) -> dict:
        """MGN's own per-server report, or nothing. The ground truth an affirmation is checked
        against, never the model's recollection of it."""
        try:
            return self.dispatch(
                "-", f"mgn-view-{self.clock()[:16]}", "mgn_status",
                guard=Action("dispatch_step", step="mgn_status"),
            )
        except Exception:  # noqa: BLE001 - no evidence is not evidence of completion
            return {}

    def _estate_note(self) -> str:
        """One sentence of ground truth from MGN, or nothing. Deliberately a read-out: it says what
        the service reports, never that somebody installed or configured anything, so it passes the
        same honesty check as any message the agent sends through its tools."""
        try:
            result = self.dispatch(
                "-", f"estate-{self.clock()[:16]}", "mgn_status",
                guard=Action("dispatch_step", step="mgn_status"),
            )
        except Exception:  # noqa: BLE001 - context is a bonus, never a reason to not start
            return ""
        by_state = result.get("by_state") or {}
        ready = by_state.get("CONTINUOUS", 0)
        if not ready:
            return ""
        note = (f" AWS Application Migration Service reports {result.get('total', 0)} source "
                f"servers in this account, {ready} of them replicating continuously and ready for "
                f"a test launch.")
        stalled = result.get("stalled") or []
        if stalled:
            note += f" {len(stalled)} report a replication error."
        return note

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
            self._save(wave)

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
            self._save(wave)

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

    def _verdict_step(
        self, wave_id: str, step: str, spec: str, diffs: dict, criticality: str,
        params: dict | None = None,
    ) -> _Signal:
        rolled = {"v": False}

        def request_rollback(reason: str) -> None:
            rolled["v"] = True
            self._log(wave_id, "rollback", f"{step}: {reason}")
            self.transition(wave_id, WaveStatus.ROLLING_BACK)
            self.dispatch(wave_id, f"{step}-rollback", "rollback", params=params)
            self.transition(wave_id, WaveStatus.ROLLED_BACK)

        verdict = issue_verdict(
            self.model, spec=spec, diffs=diffs, criticality=criticality,
            request_rollback=request_rollback,
        )
        # Structured, because the cutover step re-checks this server-side. `judged` is how many
        # applications the verdict actually looked at: a green verdict on nothing is not a pass.
        self._log(
            wave_id, "decision", f"{step}: {'green' if verdict.ok else 'red'} verdict",
            {"reasons": verdict.reasons,
             "verdict": {"step": step, "ok": verdict.ok, "judged": len(diffs or {})}},
        )
        return _Signal.ROLLBACK if rolled["v"] else _Signal.CONTINUE

    def _step_precheck(self, wave_id, inputs, ready) -> _Signal:
        if not self.contract.steps:
            return self._escalate_step(wave_id, "contract has no steps", "incomplete extraction")
        return self._baseline_apps(wave_id, inputs)

    def _baseline(self, wave_id: str) -> dict | None:
        """The pre-migration observation, read back from the decision_log so a resumed wave keeps
        comparing against the original baseline rather than re-measuring a migrated system."""
        for e in self.store.decisions(wave_id):
            blob = (e.detail or {}).get("app_baseline")
            if blob:
                return blob
        return None

    def _probe_targets(self, wave_id: str, expectations: list[dict] | None = None,
                       phase: str = "baseline") -> list[dict]:
        """Where each application answers *right now*, with what the engineer said healthy means.

        Resolved on every probe, never pinned. An application is a name; the machine behind it is
        the source before a launch and the migrated instance after one, and the whole point of the
        diff is that those are different machines. Reusing the baseline's addresses compares a
        machine with itself, comes back identical, and hands the cutover a verdict it never earned.

        The engineer contributes what only they know - the text a healthy response contains, a
        Secrets Manager reference for a login - and never an address."""
        found = self._discovered_probes(wave_id, phase)
        if not found:
            # Nothing in the estate answers on a public address. The engineer's own entries are
            # then the only thing to go on, addresses included - see _step_test, which refuses to
            # call that a verdict, because comparing one fixed address twice proves nothing.
            return list(expectations or [])
        knowledge = {e.get("name"): e for e in (expectations or [])}
        merged = []
        for target in found:
            extra = knowledge.get(target["name"]) or {}
            merged.append({**{k: v for k, v in extra.items() if k != "url" and k != "name"},
                           **target})
        return merged

    def _discovered_probes(self, wave_id: str, phase: str = "baseline") -> list[dict]:
        """Ask the estate where its applications are. Recorded in the decision log and marked as
        derived: a person reading the record has to be able to see that nobody supplied these."""
        try:
            # Keyed by which probe it serves, not by the clock. The dispatcher is idempotent by
            # (wave, step id): under one shared id the after-probe was handed the baseline's cached
            # addresses, which is the very comparison-with-itself this resolution exists to avoid.
            result = self.dispatch(
                wave_id, f"discover-probes-{phase}", "discover_probe_targets",
                guard=Action("dispatch_step", step="discover_probe_targets"),
            )
        except Exception as e:  # noqa: BLE001 - falling back to asking is the designed outcome
            self._log(wave_id, "decision",
                      f"could not derive what to sanity-check ({type(e).__name__}); "
                      f"the engineer will be asked instead")
            return []
        # `side` travels with the target: _step_test refuses to judge a comparison in which every
        # application still answers on its source machine, and cannot know that without it.
        probes = [{k: v for k, v in c.items() if k in ("name", "url", "expect_status", "side")}
                  for c in result.get("candidates") or []]
        if not probes:
            return []
        sides = result.get("sides") or []
        self._log(
            wave_id, "decision",
            f"resolved where to sanity-check {', '.join(p['name'] for p in probes)} - "
            f"{' and '.join(sides) or 'source'} instance(s)",
            {"app_probes": probes, "source": "derived", "sides": sides,
             "not_serving": result.get("not_serving")},
        )
        return probes

    def _probes_recorded_as(self, wave_id: str, source: str) -> list[dict]:
        for e in self.store.decisions(wave_id):
            detail = e.detail or {}
            if detail.get("app_probes") and detail.get("source") == source:
                return detail["app_probes"]
        return []

    def _baseline_apps(self, wave_id: str, inputs) -> _Signal:
        """Sanity-check the applications before anything moves. Without this the cutover gate has
        nothing to compare against and passes on an empty diff, which is worse than no gate."""
        if self._baseline(wave_id) is not None:
            return _Signal.CONTINUE
        probes = self._probe_targets(wave_id, inputs.app_probes)
        if not probes:
            # Nothing publicly reachable to check. Only now is a person the answer.
            self.hitl.submit(wave_id, APP_PROBE_SPEC, {
                "needed": "url + expected response per application, and a Secrets Manager ARN "
                          "for any that need a login (never the credential itself)",
                "shape": {"apps": [{"name": "", "url": "", "expect_status": 200,
                                    "expect_contains": "", "secret_arn": ""}]},
            })
            self._log(
                wave_id, "hitl",
                "asked the engineer for what to sanity-check: no application in this wave answered "
                "on a public address, so the targets could not be derived from the estate. The "
                "wave continues, but the cutover cannot be judged until this arrives",
                {"gate": APP_PROBE_SPEC},
            )
            return _Signal.CONTINUE
        inputs.app_probes = probes

        result = self.dispatch(
            wave_id, "baseline-probe", "probe_apps",
            guard=Action("dispatch_step", step="probe_apps"),
            params={"apps": probes},
        )
        if result.get("error"):
            return self._escalate_step(wave_id, "the pre-migration sanity check failed",
                                       str(result["error"])[:300])
        self._log(
            wave_id, "decision",
            f"pre-migration sanity check: {result.get('healthy')} of {result.get('probed')} "
            f"application(s) healthy - this is the baseline the cutover is judged against",
            {"app_baseline": result},
        )
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
        if inputs.modernization_target:
            self._modernization_scenario(wave_id, inputs)
        return _Signal.CONTINUE

    def _modernization_scenario(self, wave_id: str, inputs: WaveInputs) -> None:
        """Deliverable 1's modernization half. If policy says the target can't be modernized
        (e.g. Transform doesn't cover it), `check` already logged the escalation - that IS the
        output (deliverable 5). Otherwise the Interpreter generates a verified, un-applied IaC
        artifact. Either way the lift-and-shift path keeps going."""
        action = Action(
            "modernize", subject=inputs.modernization_subject,
            modernization_target=inputs.modernization_target,
        )
        decision = self.check(action, wave_id=wave_id)
        if not decision.allowed:
            self._log(
                wave_id, "decision",
                f"modernization of {inputs.modernization_target!r} not pursued - rehost only",
                {"reasons": decision.reasons},
            )
            return
        if self.workspace is not None and inputs.modernization_job:
            self._drive_transform_job(wave_id, inputs.modernization_job, "modernization_artifact")
            return
        result = generate_modernization_iac(inputs.modernization_objective, self.model)
        self._log(
            wave_id, "modernization_artifact",
            f"modernization IaC {'verified' if result.ok else 'NOT verified'} "
            f"in {result.iterations} iteration(s)",
            {"verified": result.ok, "iterations": result.iterations, "iac": result.value or ""},
        )

    def _drive_transform_job(
        self, wave_id: str, job_needle: str, kind: str, sizing: list[dict] | None = None,
        approved_gates: list[str] | None = None,
    ) -> _Signal:
        """AWS Transform executes; the Orchestrator only decides. A blocking CRITICAL task is a
        human gate, never something to answer autonomously. Returns the wave signal to follow."""
        job = self.workspace.job_by_name(job_needle)
        if job is None:
            self._log(wave_id, "escalation", f"no Transform job matching {job_needle!r}")
            return _Signal.ESCALATE
        jid, jname = job["jobId"], job.get("jobName", job_needle)
        status = (job.get("statusDetails") or {}).get("status", "")

        blocking = self.workspace.blocking_tasks(jid)
        if blocking:
            titles = "; ".join(f"{t['title']} [{t['taskId']}]" for t in blocking)
            self._log(
                wave_id, "hitl", f"{jname} is blocked on {len(blocking)} human task(s): {titles}",
                {"job_id": jid, "status": status, "tasks": blocking},
            )
            self.escalate(
                wave_id,
                context=f"{jname} status={status}, blocking tasks: {titles}",
                hypothesis="a CRITICAL Transform task needs a human; the agent must not answer it",
                attempts=0,
            )
            return _Signal.ESCALATE

        if status not in ("EXECUTING", "COMPLETED"):
            try:
                self.workspace.control_job(jid, "start")
                status = "STARTING"
            except TransformError as e:
                # Some job types refuse a restart outright. Those are already under way, so the
                # status stands and the chat keeps driving them.
                self._log(wave_id, "decision", f"{jname} cannot be restarted, driving it as-is",
                          {"job_id": jid, "status": status, "detail": str(e)[:200]})

        # A Transform job can sit in EXECUTING doing nothing, waiting on a chat interaction, and
        # AWAITING_HUMAN_INPUT is the same gate stated out loud. Answering it is what advances the
        # migration - neither status means the job is progressing on its own.
        if kind == "decision" and status in DRIVABLE_STATUSES:
            return self._advance_migration(wave_id, jid, jname, sizing, approved_gates)

        artifacts = self.workspace.walk_artifacts(jid)
        self._log(
            wave_id, kind,
            f"{jname} driven through AWS Transform (status {status}, {len(artifacts)} artifact(s))",
            {"job_id": jid, "status": status, "artifact_count": len(artifacts), "verified": True},
        )
        return _Signal.CONTINUE if status == "COMPLETED" else _Signal.WAIT

    PROCEED_GATE = "proceed_wave_cutover"
    RESIZE_GATE = "resize_to_peak"

    def _gate_approved(self, wave_id: str, gate: str, approved_gates: list[str]) -> bool:
        """Approved on this invocation, or on any earlier one - the decision_log is the record."""
        if gate in (approved_gates or []):
            return True
        return any(
            e.kind == "hitl" and e.actor == "human" and (e.detail or {}).get("gate") == gate
            and "APPROVED" in e.summary
            for e in self.store.decisions(wave_id)
        )

    @staticmethod
    def _pick_option(options: list[dict], allow: tuple = ADVANCE_OPTIONS) -> str | None:
        """Only options that plainly advance this wave. Anything else is a human's call."""
        for o in options:
            value = str(o.get("value") or o.get("label") or "")
            if any(k in value.lower() for k in allow):
                return value
        return None

    GATE_SYSTEM = (
        "You are an autonomous AWS migration agent driving an AWS Transform rehost to completion. "
        "AWS Transform is asking you to choose a configuration value or confirm a step. Choose the "
        "AWS-recommended default - the most reversible, lowest-risk, lowest-cost option - unless "
        "the decision contract constrains it. Reply with EXACTLY one of the offered option "
        "strings and nothing else, or the single word ESCALATE if the choice has security, data-"
        "loss, or budget consequences you cannot settle from the contract."
    )

    def _decide_option(self, pending: dict, labels: list[str]) -> tuple[str | None, str | None]:
        """No plain advance option - let the model pick a sensible default among what is offered,
        bounded to the offered strings and to ESCALATE."""
        c = self.contract
        prompt = (
            f"Decision contract {c.case_id}: budget ceiling "
            f"{c.budget.ceiling_monthly_usd} USD/month; constraints: "
            + ("; ".join(f"{k.subject} {k.predicate} {k.value}" for k in c.constraints[:8])
               or "none")
            + f"\n\nAWS Transform asks:\n{(pending.get('text') or '').strip()[:1200]}\n\n"
            f"Options: {labels}\n\nYour choice (one option string exactly, or ESCALATE):"
        )
        raw = self.model.complete(prompt, system=self.GATE_SYSTEM).strip().strip("\"'`").strip()
        for label in labels:
            if raw.lower() == label.lower() or label.lower() in raw.lower():
                return label, "model chose the safe default for a configuration gate"
        return None, (f"the offered options {labels} are a configuration choice the agent could "
                      f"not resolve from the contract (model said {raw[:80]!r})")

    def _classify_gate(
        self, pending: dict, cutover_denied: bool
    ) -> tuple[str | None, str | None]:
        """At a Transform gate the agent decides: pick an option, or hand it to a person. Returns
        (option_to_pick, None) to advance, or (None, reason) to escalate. Cost is not judged here -
        the contract ceiling is enforced on structured sizing estimates, not on chat prose."""
        text = (pending.get("text") or "").lower()
        options = pending.get("options") or []
        labels = [str(o.get("value") or o.get("label") or "") for o in options]
        hay = text + " " + " ".join(labels).lower()

        need = manual_prerequisite(text)
        if need:
            return None, f"AWS Transform needs a manual step first: {need}"
        if carries_stop_signal(hay):
            return None, "an option carries an irreversible, destructive or safety-skipping action"
        if cutover_denied and any("cut" in label.lower() and "over" in label.lower()
                                  for label in labels):
            return None, "the cutover is withheld pending re-size or human approval"

        for label in labels:
            if any(k in label.lower() for k in ADVANCE_OPTIONS):
                return label, None
        # Transform often offers a single "continue rehost" and nothing else - taking it is safe
        # once the stop-signal and manual-blocker checks above have passed.
        if len(labels) == 1 and labels[0]:
            return labels[0], None
        # An either/or configuration choice (Static/Dynamic IP, GP2/GP3, ...): the model picks the
        # safe default, bounded to the offered options.
        if labels:
            return self._decide_option(pending, labels)
        return None, "AWS Transform offered no options to choose from"

    STALL_MARK = "did not advance the workflow this invocation"
    STALL_ESCALATE_AFTER = 4

    def _prior_stalls(self, wave_id: str, pending_text: str) -> int:
        """How many past invocations bounced on this exact question. A question that keeps changing
        (a sync counting up, a phase moving on) is progress, not a stall, so it does not count."""
        key = (pending_text or "").strip()[:200]
        return sum(
            1 for e in self.store.decisions(wave_id)
            if self.STALL_MARK in e.summary and (e.detail or {}).get("pending_key") == key
        )

    @staticmethod
    def _stall_record(jname: str, pending: dict, option: str) -> str:
        return chr(10).join([
            f"# {jname}: the rehost workflow has stalled",
            "",
            (f"The agent has selected **{option!r}** repeatedly and AWS Transform keeps returning "
             "to the same question. The workflow is not advancing."),
            "",
            "## Where it stopped",
            "",
            f"> {(pending.get('text') or '').strip()[:1500]}",
            "",
            "## Most likely cause",
            "",
            ("One of two things. Either a step depends on something outside the agent - source "
             "servers that are not reachable, replication agents that never register with MGN, a "
             "connector not deployed in the source environment - and a retry will not fix it. Or "
             "the replication/sync phase is being driven from the AWS Transform console by a "
             "person: Transform's chat threads are per-identity, so this agent's thread cannot "
             "see that progress and keeps re-asking its own last question."),
            "",
            "## To resume",
            "",
            ("Finish the underlying step (in the source environment or the console). Once the "
             "shared job reaches its next decision point, re-invoke the wave and the agent picks "
             "up from there. Everything up to here was decided and recorded autonomously."),
            "",
        ])

    @staticmethod
    def _gate_record(jname: str, pending: dict, reason: str) -> str:
        """The record a human reads when the agent stops at a Transform gate it will not decide."""
        opts = [o.get("value") or o.get("label") for o in pending.get("options") or []]
        return chr(10).join([
            f"# {jname}: a human decision is needed",
            "",
            ("The agent drives AWS Transform's workflow autonomously. It stopped here because "
             f"**{reason}**."),
            "",
            "## What AWS Transform asked",
            "",
            f"> {(pending.get('text') or '').strip()[:1500]}",
            "",
            f"Options offered: {opts}",
            "",
            "## To resume",
            "",
            ("Answer in the AWS Transform console, or approve the gate "
             "(`proceed_wave_cutover`) and re-invoke the wave. No state was lost."),
            "",
        ])

    def _publish_record(self, wave_id: str, jid: str, title: str, body: str) -> None:
        """Chat threads are per-identity, so nothing the agent says reaches the console. Artifacts
        are shared: publishing the decision record is what makes the reasoning reviewable."""
        try:
            stamp = self.clock().replace(':', '-')[:19]
            name = f"agent-{title.lower().replace(' ', '-')}-{wave_id}-{stamp}.md"
            self.workspace.upload_artifact(jid, body, name)
            self._log(wave_id, "decision", f"published {name} to the job's artifacts",
                      {"job_id": jid, "artifact": name})
        except Exception as e:  # noqa: BLE001 - publishing is a courtesy, never the blocker
            self._log(wave_id, "decision", f"could not publish the decision record: {e}",
                      {"job_id": jid})

    def _denial_record(self, jname: str, denied: list[str], sizing: list[dict] | None) -> str:
        """The record a human reads in the console: what was refused, and on what evidence."""
        rows = [
            f'| {d.split(chr(40))[0].strip()} | {d.split(chr(40))[1].split(chr(41))[0] if chr(40) in d else ""} | {d.split(chr(58) + chr(32), 1)[1] if chr(58) + chr(32) in d else d} |'
            for d in denied
        ]
        return (
            f"# Wave 0 cutover withheld - {jname}\n\n"
            f"The agent derived a decision contract from this job's own wave plan and "
            f"enriched inventory, then checked every instance the plan would launch "
            f"against it. **{len(denied)} of {len(sizing or [])} servers fail.**\n\n"
            f"| Server | Planned instance | Why it fails |\n|---|---|---|\n"
            + chr(10).join(rows)
            + "\n\nThe recommendations were generated with **Average** sizing while "
            "the inventory records peak CPU of 94-100% on most of these hosts. The "
            "cutover is held until the sizing is corrected or a human approves "
            "proceeding anyway; either decision is recorded in the wave's decision log.\n"
        )

    def _request_resize(
        self, wave_id: str, jid: str, jname: str, denied: list[str], sizing: list[dict] | None
    ) -> _Signal:
        """Ask AWS Transform to redo the EC2 recommendations on peak utilisation. This changes the
        plan, not the estate: nothing migrates until the sizing satisfies the contract."""
        worst = "; ".join(denied[:6])
        self._publish_record(
            wave_id, jid, "resize requested", self._denial_record(jname, denied, sizing)
        )
        text = (
            f"The Wave 0 EC2 recommendations were generated with Average sizing, but the enriched "
            f"inventory shows {len(denied)} of {len(sizing or [])} servers are under-provisioned "
            f"by it: {worst}. Please regenerate the EC2 recommendations for Wave 0 using peak "
            f"utilisation with headroom, then show me the updated wave plan."
        )
        for turn in range(MAX_CHAT_TURNS):
            try:
                reply = self.workspace.send_message(jid, text, skip_polling=False)
            except TransformBusy as e:
                self._log(wave_id, "decision",
                          f"{jname}: assistant still working on the previous turn; "
                          f"will resume on the next invocation",
                          {"job_id": jid, "detail": str(e)[:200]})
                return _Signal.WAIT
            pending = self.workspace.pending_interaction(jid)
            self._log(
                wave_id, "remediation_outcome",
                f"{jname}: asked AWS Transform to re-size Wave 0 on peak utilisation "
                f"(turn {turn + 1})",
                {"job_id": jid, "turn": turn + 1, "denied": len(denied),
                 "reply": str(reply)[:600],
                 "offered": [o.get("value") for o in (pending or {}).get("options", [])]},
            )
            if pending is None:
                return _Signal.WAIT
            option = self._pick_option(pending["options"], RESIZE_OPTIONS)
            if option is None:
                self._log(
                    wave_id, "hitl",
                    f"{jname} offered options the agent may not choose while re-sizing: "
                    f"{[o.get('value') for o in pending['options']]}",
                    {"job_id": jid, "options": pending["options"]},
                )
                return _Signal.ESCALATE
            text = option
        return _Signal.WAIT

    def _clear_blocker(self, wave_id: str, jid: str, jname: str, said: str) -> str | None:
        """A prerequisite AWS Transform cannot satisfy itself is still a real decision: the agent
        clears it through a policy-gated, idempotent step, never by talking its way past the gate.
        Returns the phrase that resumes the job, or None when nothing was recognised."""
        low = (said or "").lower()
        for terms, step, resume in BLOCKERS:
            if not all(t in low for t in terms):
                continue
            result = self.dispatch(
                wave_id, f"blocker-{step}", step, guard=Action("dispatch_step", step=step),
            )
            if result.get("error"):
                self._log(
                    wave_id, "escalation", f"{jname}: could not clear the {step} prerequisite",
                    {"job_id": jid, "step": step, "error": str(result["error"])[:400]},
                )
                # The chat thread is private to this identity, so the record is how a human sees it
                self._publish_record(
                    wave_id, jid, f"{step} blocked",
                    self._blocker_record(jname, step, said, str(result["error"])),
                )
                self.escalate(
                    wave_id, context=f"{jname} is blocked on {step}: {result['error']}",
                    hypothesis="the prerequisite needs an account administrator", attempts=1,
                )
                return None
            self._log(
                wave_id, "remediation_outcome",
                f"{jname}: cleared the {step} prerequisite the wave was blocked on",
                {"job_id": jid, "step": step, "result": result},
            )
            return resume
        return None

    @staticmethod
    def _blocker_record(jname: str, step: str, said: str, error: str) -> str:
        """What a human needs to unblock the wave: what stopped it, what the agent already
        tried, and what is left that only an account administrator can do."""
        return chr(10).join([
            f"# {jname} is blocked on `{step}`",
            "",
            ("AWS Transform stopped the wave and reported a prerequisite it cannot satisfy "
             "itself. The agent attempted the remediation automatically and it was refused."),
            "",
            "## What AWS Transform reported",
            "",
            f"> {said.strip()[:1200]}",
            "",
            "## What the agent tried",
            "",
            (f"Dispatched the `{step}` step through the step dispatcher, under the same "
             "policy check and idempotency ledger as every other wave action."),
            "",
            "## Why it failed",
            "",
            "```",
            error[:800],
            "```",
            "",
            "## What is left for a human",
            "",
            ("This needs an administrator of the target account. The wave resumes on the "
             "next invocation once the prerequisite is met - no state was lost, and nothing "
             "was forced past the gate."),
            "",
        ])

    def _check_replication_health(self, wave_id: str, jid: str, jname: str) -> None:
        """AWS Transform reports its own progress, not MGN's. Servers can sit in
        FAILED_TO_CONNECT_AGENT_TO_REPLICATION_SERVER while the chat keeps counting up, so this
        looks at the infrastructure directly rather than waiting for the conversation to stall.
        The step is a no-op unless the burstable-server signature is actually present."""
        # Once per invocation: the ledger key moves with the clock so a later run re-checks, while
        # repeated calls inside one run reuse the stored result.
        fix = self.dispatch(
            wave_id, f"replication-health-{self.clock()[:16]}", "resize_replication_server",
            guard=Action("dispatch_step", step="resize_replication_server"),
        )
        if fix.get("resized"):
            self._log(
                wave_id, "remediation_outcome",
                f"{jname}: {len(fix.get('stalled') or [])} server(s) could not reach the shared "
                f"replication server, which is burstable and out of CPU credits; resized "
                f"{fix.get('from')} -> {fix.get('to')}",
                {"job_id": jid, "result": fix},
            )

    def _advance_migration(
        self, wave_id: str, jid: str, jname: str,
        sizing: list[dict] | None, approved_gates: list[str] | None = None,
    ) -> _Signal:
        """Instruct AWS Transform to run the cutover, but only after every server the plan would
        launch has been checked against the contract. A denial is the finding, not an obstacle to
        work around: it goes to a human, and only their approval lets the cutover through."""
        self._check_replication_health(wave_id, jid, jname)
        denied = []
        for s in sizing or []:
            decision = evaluate_policy(
                Action(
                    "recommend_instance", subject=s.get("name", "*"),
                    vcpu=s.get("vcpu"), ram_gib=s.get("ram_gib"),
                ),
                self.contract,
            )
            if not decision.allowed:
                denied.append(f"{s.get('name')} ({s.get('instance_type')}): {decision.reasons[0]}")

        payload = {"gate": self.PROCEED_GATE, "job_id": jid, "denied": denied}
        if denied:
            self._log(
                wave_id, "policy_denial",
                f"{jname} cutover withheld: the plan violates the contract for "
                f"{len(denied)} of {len(sizing or [])} servers",
                payload,
            )
            if not self._gate_approved(wave_id, self.PROCEED_GATE, approved_gates or []):
                # Fixing the plan is the other way out of the denial, and it is the safe one.
                if self._gate_approved(wave_id, self.RESIZE_GATE, approved_gates or []):
                    return self._request_resize(wave_id, jid, jname, denied, sizing)
                self.hitl.submit(wave_id, self.PROCEED_GATE, payload)
                self._publish_record(
                    wave_id, jid, "cutover withheld",
                    self._denial_record(jname, denied, sizing),
                )
                self.escalate(
                    wave_id,
                    context=f"{jname}: " + "; ".join(denied[:5]),
                    hypothesis="the wave plan under-sizes servers against its own inventory; a "
                               "human must re-size or approve the cutover before it proceeds",
                    attempts=0,
                )
                return _Signal.ESCALATE
            self.hitl.resolve(self.hitl.latest(wave_id, self.PROCEED_GATE)
                              or self.hitl.submit(wave_id, self.PROCEED_GATE, payload), True)

        # Open by asking Transform to move the rehost forward, then walk each gate it raises.
        # Jumping straight to "cutover" only makes it back the workflow up a phase at a time.
        #
        # The estate line matters more than it looks. An AWS Transform chat thread is per-identity,
        # so the agent's conversation starts empty however much context a person gave the job in
        # theirs - and an assistant told nothing about the estate replies "the workflow is at the
        # very beginning". This is a read-out of what MGN reports, not a claim about work done.
        text = f"Continue the {jname} Wave 0 rehost through to cutover.{self._estate_note()}"
        picked_here: list[str] = []
        for turn in range(MAX_CHAT_TURNS):
            try:
                reply = self.workspace.send_message(jid, text, skip_polling=False)
            except TransformBusy as e:
                self._log(wave_id, "decision",
                          f"{jname}: assistant still working on the previous turn; "
                          f"will resume on the next invocation",
                          {"job_id": jid, "detail": str(e)[:200]})
                return _Signal.WAIT
            pending = self.workspace.pending_interaction(jid)
            self._log(
                wave_id, "decision", f"{jname}: instructed {text!r} (turn {turn + 1})",
                {"job_id": jid, "turn": turn + 1, "reply": str(reply)[:600],
                 "offered": [o.get("value") for o in (pending or {}).get("options", [])],
                 "overridden_denials": len(denied)},
            )
            # A prerequisite can land either as loose prose or as the preamble to an option, and
            # which one it is varies per turn, so read everything the assistant just said.
            said = " ".join(filter(None, [
                (pending or {}).get("text", ""), self.workspace.latest_reply(jid),
            ]))
            resumed = self._clear_blocker(wave_id, jid, jname, said)
            if resumed:
                text = resumed
                continue
            if pending is None:
                return _Signal.WAIT

            option, why = self._classify_gate(pending, bool(denied))
            if option is None:
                self._log(
                    wave_id, "hitl", f"{jname}: a human decision is needed - {why}",
                    {"job_id": jid, "options": pending["options"], "reason": why},
                )
                self._publish_record(
                    wave_id, jid, "human decision needed",
                    self._gate_record(jname, pending, why or "?"),
                )
                self.escalate(
                    wave_id, context=f"{jname} gate: {why}",
                    hypothesis="a Transform gate needs human judgement; the agent advanced every "
                               "other step on its own",
                    attempts=0,
                )
                return _Signal.ESCALATE
            if option in picked_here:
                # Transform bounced the same question straight back within this invocation. Nudge
                # it once per invocation, no more - re-poll on the next call, and only escalate
                # for real once several invocations bounce on the *same* question. A long wait
                # whose text changes (replication syncing, a phase advancing) never gets here.
                key = (pending.get("text") or "").strip()[:200]
                rounds = self._prior_stalls(wave_id, key) + 1
                self._log(
                    wave_id, "decision",
                    f"{jname}: {option!r} {self.STALL_MARK} (round {rounds}); "
                    f"Transform still says: {(pending.get('text') or '')[:160]}",
                    {"job_id": jid, "option": option, "round": rounds, "pending_key": key,
                     "pending": (pending.get("text") or "")[:400]},
                )
                if rounds >= self.STALL_ESCALATE_AFTER:
                    # Before handing a stall to a person, try the infrastructure cause the agent
                    # can actually fix. The step is a no-op unless that exact signature is present.
                    fix = self.dispatch(
                        wave_id, f"stall-resize-{rounds}", "resize_replication_server",
                        guard=Action("dispatch_step", step="resize_replication_server"),
                    )
                    if fix.get("resized"):
                        self._log(
                            wave_id, "remediation_outcome",
                            f"{jname}: replication was stalled because the shared replication "
                            f"server is burstable and had run out of CPU credits; resized "
                            f"{fix.get('from')} -> {fix.get('to')} for "
                            f"{len(fix.get('stalled') or [])} server(s) failing to connect",
                            {"job_id": jid, "result": fix},
                        )
                        return _Signal.WAIT
                    self._log(
                        wave_id, "decision",
                        f"{jname}: the replication server is not the cause - "
                        f"{fix.get('reason') or fix.get('error')}",
                        {"job_id": jid, "result": fix},
                    )
                    self._log(
                        wave_id, "hitl",
                        f"{jname}: the workflow has stalled - {option!r} for {rounds} invocations "
                        f"with no change; a step outside the agent has to complete first",
                        {"job_id": jid, "option": option, "options": pending["options"]},
                    )
                    self._publish_record(
                        wave_id, jid, "workflow stalled",
                        self._stall_record(jname, pending, option),
                    )
                    self.escalate(
                        wave_id,
                        context=f"{jname}: {option!r} for {rounds} invocations, not advancing",
                        hypothesis="a step depends on the source estate or a service that never "
                                   "registers; retrying will not clear it",
                        attempts=rounds,
                    )
                    return _Signal.ESCALATE
                return _Signal.WAIT
            if affirms_a_manual_step(pending.get("text") or "", option):
                done, evidence = installation_is_done(
                    self._mgn_view(), [s.get("name") for s in (sizing or []) if s.get("name")])
                if not done:
                    self._log(wave_id, "decision",
                              f"{jname}: refused to answer {option!r} - {evidence}",
                              {"job_id": jid, "option": option, "evidence": evidence})
                    return self._escalate_step(
                        wave_id,
                        f"{jname} is waiting to be told a manual step is finished",
                        f"answering {option!r} would assert work nobody has shown was done: "
                        f"{evidence}")
                self._log(wave_id, "decision",
                          f"{jname}: {option!r} is answerable - {evidence}",
                          {"job_id": jid, "option": option, "evidence": evidence})
            self._log(
                wave_id, "decision",
                f"{jname}: autonomously chose {option!r} - {why or 'plain workflow advance'}",
                {"job_id": jid, "option": option, "turn": turn + 1, "basis": why},
            )
            picked_here.append(option)
            text = option
        self._log(wave_id, "decision", f"{jname}: chat turn budget spent, will resume",
                  {"job_id": jid})
        return _Signal.WAIT

    def _step_replicate(self, wave_id, inputs, ready) -> _Signal:
        # AWS Transform owns the rehost: it drives MGN through its own job steps. When the wave
        # names that job, the Orchestrator watches and gates it instead of calling MGN itself.
        if self.workspace is not None and inputs.migration_job:
            return self._drive_transform_job(
                wave_id, inputs.migration_job, "decision", inputs.sizing, inputs.approved_gates
            )
        result = self.dispatch(
            wave_id, "replicate", "start_replication",
            guard=Action("dispatch_step", step="replicate"), params=inputs.step_params,
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

    @staticmethod
    def _diff_probes(before: dict, after: dict) -> dict:
        """What changed per application between the baseline and now. The QA agent judges this;
        an empty diff means nothing was measured, not that nothing broke."""
        was = {r["name"]: r for r in (before or {}).get("results", [])}
        now = {r["name"]: r for r in (after or {}).get("results", [])}
        diffs: dict = {}
        for name in sorted(set(was) | set(now)):
            b, a = was.get(name), now.get(name)
            if b is None:
                diffs[name] = {"appeared": a}
            elif a is None:
                diffs[name] = {"disappeared": b}
            elif b.get("ok") != a.get("ok") or b.get("status") != a.get("status"):
                diffs[name] = {"was": {"ok": b.get("ok"), "status": b.get("status")},
                               "now": {"ok": a.get("ok"), "status": a.get("status"),
                                       "error": a.get("error")}}
            elif b.get("fingerprint") != a.get("fingerprint"):
                diffs[name] = {"content_changed": True,
                               "was_bytes": b.get("bytes"), "now_bytes": a.get("bytes")}
        return diffs

    def _step_test(self, wave_id, inputs, ready) -> _Signal:
        baseline = self._baseline(wave_id)
        diffs = inputs.test_diffs
        if baseline is None and not diffs:
            # A verdict on an empty diff comes back green, which would approve a cutover that was
            # never tested. Refusing here is the whole point of the gate.
            return self._escalate_step(
                wave_id, "the cutover cannot be judged",
                "there is no pre-migration baseline and no test diffs, so a verdict here would be "
                f"green by default. Supply the {APP_PROBE_SPEC} (application URLs, expected "
                "response, Secrets Manager ARN for any login) and re-run the wave",
            )
        if baseline is not None:
            # Resolved again, not reused: by now the application answers on the migrated
            # instance, and probing the baseline's address would compare the source with itself.
            targets = self._probe_targets(wave_id, inputs.app_probes, phase="after")
            # Only when the agent resolved the addresses itself does it know whose machine they
            # are. An engineer's own entry may well be a name that follows the migration - that is
            # the ordinary case - so it is taken at face value. An address the agent resolved to
            # the source is not: probing it again would compare the source with itself.
            resolved = [t for t in targets if t.get("side")]
            if resolved and not any(t["side"] == "migrated" for t in resolved):
                return self._escalate_step(
                    wave_id, "the cutover cannot be judged",
                    "every application still resolves to its source machine, so an after-probe "
                    "would compare the source with itself and come back green without the "
                    "migration having been shown to work. Launch the test instances first",
                )
            after = self.dispatch(
                wave_id, "post-migration-probe", "probe_apps",
                guard=Action("dispatch_step", step="probe_apps"),
                params={"apps": targets},
            )
            if after.get("error"):
                return self._escalate_step(wave_id, "the post-migration sanity check failed",
                                           str(after["error"])[:300])
            diffs = self._diff_probes(baseline, after)
            self._log(
                wave_id, "decision",
                f"post-migration sanity check: {after.get('healthy')} of {after.get('probed')} "
                f"healthy, {len(diffs)} application(s) differ from the baseline",
                {"app_after": after, "app_diffs": diffs},
            )
        return self._verdict_step(
            wave_id, "test", inputs.test_spec, diffs, inputs.test_criticality
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
        self.dispatch(wave_id, "cutover", "cutover", guard=action, params=inputs.step_params)
        return _Signal.CONTINUE

    def _step_parity(self, wave_id, inputs, ready) -> _Signal:
        return self._verdict_step(
            wave_id, "parity", inputs.test_spec, inputs.parity_diffs, "high", inputs.step_params
        )

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


def _remediation_executor(dispatcher, contract):
    """Remediation's hands. Every action goes through the dispatcher, so it is policy-checked and
    idempotent like any other effect - the specialist chooses *what*, never *whether it is allowed*."""
    def apply_action(action: str, target: dict) -> dict:
        ctx = StepContext(
            wave_id=target.get("wave_id", "-"),
            step_id=f"remediate-{action}-{target.get('source_server_id') or target.get('instance_id') or 'x'}",
            contract=contract,
            params={"action": action, **target},
        )
        try:
            return dispatcher.dispatch(
                "apply_remediation", ctx, guard=Action("dispatch_step", step="apply_remediation")
            )
        except StepRejected as e:
            return {"resolved": False, "detail": f"policy rejected {action}: {e}"}

    return apply_action


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
    workspace=None,
    notify: Callable[[DecisionLogEntry], None] | None = None,
) -> Orchestrator:
    kb = runbook_kb if runbook_kb is not None else InMemoryRunbookKB()
    _apply = _remediation_executor(dispatcher, contract)
    return Orchestrator(
        model,
        contract,
        store=store,
        dispatcher=dispatcher,
        hitl=hitl,
        specialists=specialists
        or Specialists(
            interpreter=build_interpreter(model),
            remediation=build_remediation(model, contract, kb=kb, apply_action=_apply),
            validation=build_validation(model),
            finops=build_finops(model, contract),
        ),
        clock=clock,
        runbook_kb=kb,
        workspace=workspace,
        notify=notify,
    )
