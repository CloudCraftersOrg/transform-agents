# transform-agents

Multi-agent system that exercises **agentic autonomy over an AWS migration** (Strands → AgentCore)
and **measures how far it gets**. Double deliverable: the system working + an autonomy scorecard
backed by data.

Out of scope: the demo base app, the discovery CSV, and running AWS Transform itself (another team).

**New here?** Read [docs/how-it-works.md](docs/how-it-works.md) first (plain language, no AWS
background needed), then [docs/architecture.md](docs/architecture.md) for the deployment diagram.

---

## Design principle

> Match the tool to the task. An agent only earns its keep on unpredictable work.

| Surface | Unpredictable | Implementation |
|---|---|---|
| Trigger MGN / LZA / replication, advance the state machine, evaluate budget | No | Deterministic code (`dispatcher/`, `state/`, `tools/policy.py`) |
| Decide what step comes next and with what authority | Yes | Orchestrator agent |
| Generate valid config from an ambiguous source | Yes | Interpreter agent |
| Diagnose a failure never seen before | Yes | Remediation agent |
| Decide what to test and whether a diff matters | Yes | Validation QA agent |
| Attribute root cause to a cost deviation | Yes | FinOps agent |

The four specialists are Strands `Agent`s wrapped in `@tool` (the *agent-as-tool* pattern); the
Orchestrator invokes them like any other tool. No agent has more than 8 tools. The Orchestrator has
no direct AWS APIs.

## Roster

| Agent | Agentic use case | Writes | Model (plan) |
|---|---|---|---|
| Orchestrator | Dynamic tool selection; decision under policy | no | bake-off |
| Interpreter | Generation with verification (LZA config, IaC) | no | bake-off |
| Remediation | Exception-handling loop; the only one that writes (`policy.allowed_actions` allow-list) | yes | bake-off |
| Validation QA | Decides what to test, interprets diffs, **triggers the rollback** without waiting for a human | no | Nova Lite |
| FinOps | Attributes root cause to the run-rate deviation at cutover | no | Nova Lite |

## Three pillars

- **Memory** — wave state does NOT live in the loop: DynamoDB (`wave_state`, `decision_log`) + a
  legal transition table in code. Long term: runbook KB (S3 Vectors, never OpenSearch Serverless).
  Every failure Remediation resolves is written as a runbook: the system improves between runs, and
  that's measurable.
- **Guardrails** — input filters (Bedrock Guardrails), output verification (deterministic
  validators), permissions (policy engine + per-agent allow-lists). The policy engine is
  **code, never a prompt**. The dispatcher **re-evaluates policy server-side**: if the agent
  hallucinates an authorization, execution is rejected anyway. Absolute deny-list: deleting
  accounts, touching management SCPs, touching the source environment.
- **Trust** — `agents/trust.py::converge(generate, validate, max_iter)`: the loop
  `generate → validate against a deterministic oracle → regenerate with the error as context`,
  never "generate and trust". It's what lets cheap models work: they don't need to be right on the
  first try, they need to **converge**. Used by extraction (oracle = the contract's schema) and the
  Interpreter (oracle = `validate_lza_config`). Once the budget runs out, the agent
  **escalates with a diagnosis**.

## Decision contract

The Orchestrator works with **any** Transform business case. The split:

| | Shape | Where |
|---|---|---|
| Business case | Free-form, untyped | S3 + Knowledge Base (qualitative queries) |
| Decision contract | Small, stable, typed, **derived** | DynamoDB (policy engine decisions) |

Deriving the contract from prose is judgment → an agentic task. `agents/extraction.py::extract_contract(
business_case, model)` runs the Trust loop with oracle = `DecisionContract.model_validate` +
a check that every constraint has a non-empty `source`; it returns `(ConvergeResult, contract)`.
**Every constraint carries its `source`** (a literal quote) so the extraction can be audited. The
only programmed human gate is approving that derived contract. Prose fixture with the 3 traps:
[`fixtures/fbctf/business_case.md`](fixtures/fbctf/business_case.md).

`tools/transform_ingest.py::load_business_case(zip_or_dir)` flattens whatever the estate produced
into the prose blob `extract_contract` takes, auto-detecting the format:
- a **Transform assessment** (PPTX + XLSX + PDF, no JSON) — XLSX + FSx sheets for the per-resource
  numbers, the PDF **Financial Summary** for the authoritative compute+network+storage cost basis
  (Business Support excluded), PDF prose for the narrative.
  [`fixtures/vmware-001/`](fixtures/vmware-001/) (2 Ubuntu 16.04 servers, `c7a.medium`, 3-Year RI,
  $43/month) and [`fixtures/mixed-estate-001/`](fixtures/mixed-estate-001/) (12 servers, 10 Linux +
  2 Windows, `c5a.large`, EBS + one FSx, 3-Year RI **$587/month**; every server flagged
  "vCPU spec missing" so the contract's `budget.basis` is `transform_3yr_ri_provisional`).
- a **discovery-tool export** (the CSV bundle you upload to Transform) — server inventory,
  performance, process/app detection, dependency edges, DB inventory. This carries the app and
  dependency detail a Transform assessment doesn't.
  [`fixtures/discovery-001/`](fixtures/discovery-001/) is the **same 12-server estate** as
  `mixed-estate-001`, from the discovery side (Java services, Redis, Oracle XE, SQL Server, the
  Java→Oracle and Apache→SQL Server dependency edges, and the unidentified 100%-CPU Windows box the
  contract puts `out_of_scope`).

Per the plan the discovery CSV is another team's deliverable, but a business case is "free-form", so
the ingest reads both.

**Closed predicate vocabulary, open constraints.** `tools/spec.py::PREDICATES`:
`min_vcpu`, `min_ram_gib`, `arch_not_allowed`, `modernization_unsupported`, `blackout_window`,
`out_of_scope`, `sizing_basis`. How many constraints there are and which resources they target comes
from the document. A new predicate gets added to the vocabulary; the Orchestrator is never rewritten.

Fixture: [`fixtures/fbctf/decision_contract.json`](fixtures/fbctf/decision_contract.json) (FBCTF, 2
servers, ~$190/month baseline). Constraints `c4` (`min_ram_gib`) and `c5` (`sizing_basis`) are
derived from the same two intent lines as `c2` and are needed so the sizing traps are actually
enforceable by the engine.

## Policy engine

`tools/policy.py::evaluate_policy(action: Action, contract) -> Decision(allowed, reasons[], escalate)`.
Pure, deterministic, no dependency on Strands or the network. Every `reason` cites the constraint and
its `source`. `escalate=True` when the action falls under `modernization_unsupported` (Transform
doesn't cover the case → the agent escalates, it doesn't invent). The dispatcher calls it again
before any effect.

The plan's three traps (section 6) are acceptance criteria:
[`tests/test_golden_traps.py`](tests/test_golden_traps.py).

## State machine

`state/models.py::WaveStatus` + `state/transitions.py` (the `_LEGAL` table, in code).
`PENDING → PRECHECK → INTERPRETING → REPLICATING → TESTING → CUTTING_OVER → PARITY_CHECK → FINOPS → DONE`,
plus a `REPLICATING` self-loop (long waits), `ROLLING_BACK/ROLLED_BACK` from any post-replication
stage, `ESCALATED` from/to any active stage, `FAILED` terminal. `InMemoryStateStore` validates every
transition in `put_wave`; `DynamoDbStateStore` is the same interface, for Sprint 2.

## Dispatcher

`dispatcher/steps.py::Dispatcher`. Registry of deterministic steps (`deploy_lza`,
`start_replication`, `launch_test`, `cutover`, `rollback`, `finalize`). Idempotent by
`(wave_id, step_id)`. Re-evaluates policy server-side via the `guard: Action`. `deploy_lza` stays
registered even when LZA is off: it simply never gets dispatched.

## Orchestrator

`agents/orchestrator.py`. Agentic surface = `route(model, situation) -> str`: given a situation,
pick a tool from `TOOL_CATALOG` (the same path the bake-off scores). Everything else is the
deterministic layer, wrapped in the `Orchestrator` class, which **only holds** `store`,
`dispatcher`, `hitl` and the specialists (agent-as-tool) — **no AWS API**:

- `decide(situation)` — routes and writes the choice to the `decision_log`.
- `check(action, wave_id)` — `evaluate_policy`; on deny it writes `policy_denial`, and if the action
  falls under `modernization_unsupported` it **auto-escalates** (intent deliverable 5: declare the
  limit).
- `advance(wave_id, step)` — moves the wave's state through the transition table (`assert_legal`).
- `dispatch(wave_id, step_id, name, guard=...)` — delegates to the `Dispatcher` (idempotent +
  server-side re-eval).
- `delegate(which, objective)` — invokes the specialist; a clear `NotImplementedError` if it isn't
  wired yet.
- `request_approval` / `escalate` / `wave_digest` (digest from the store, not from MGN).

`build_orchestrator(model, contract, *, store, dispatcher, hitl, clock, specialists=...)`.

**Wave loop** — `run_wave(wave_id, inputs, *, replication_ready=..., require_contract_approval=False)`.
Walks `contract.steps` (`precheck → interpret → replicate → test → cutover → parity → finops`),
advances state through the transition table, and persists `completed_steps` step by step.
**Resumable, never blocks:** at `replicate` it dispatches `start_replication` and returns `WAITING`
(EventBridge re-invokes it; re-entering picks up where it left off). A red verdict at `test`/`parity`
triggers a rollback and returns `ROLLED_BACK`. Interpreter failing to converge or cutover blocked by
policy → `ESCALATED`. `require_contract_approval=True` = **mode 1** (HITL gate on the derived
contract before dispatching); `False` = autonomous **mode 2**. `python -m evals.wave_demo` runs a
mode-2 wave with fakes and prints the `decision_log`.

**Two approaches in one wave (deliverable 1).** The lift-and-shift path is *executed* (MGN
replicate → cutover). If `WaveInputs.modernization_target` is set, `interpret` also produces the
**modernization scenario as a verified IaC artifact that is never applied** — the Interpreter
generates container Terraform, `validate_iac` checks it, and it's written to the `decision_log`
(`kind="modernization_artifact"`, `verified: bool`). If policy says the target can't be modernized
(`modernization_unsupported`, e.g. Transform doesn't cover PHP/Hack), the escalation `check` writes
*is* the output (**deliverable 5**: "here is what cannot be modernized") and the rehost still runs.

## Thin specialists

Judgment lives in the model, execution lives in deterministic tools. Each one exposes a typed
function and a `build_*` that returns the `delegate_*(objective: str)` callable, with `objective` as
JSON (decoupled PoC).

- **Interpreter** — `agents/interpreter.py::generate_lza_config` (rehost/LZA config) and
  `generate_modernization_iac` (the container scenario). Both are `converge` loops; the second's
  oracle is `validate_iac` (`terraform validate` if the binary is present, else an offline
  structural HCL check for a container compute target + image source).
- **Validation QA** — `agents/validation.py::issue_verdict(model, spec, diffs, criticality,
  request_rollback=...)`. The model decides whether a diff matters and issues a `Verdict(ok, reasons)`;
  on doubt or an illegible verdict → **red** (false green = zero target). On red it calls
  `request_rollback` without waiting for a human. Deterministic tools: `tools/qa.py::api_diff` /
  `schema_diff` (real added/removed/changed), `playwright_run` (stub).
- **FinOps** — `agents/finops.py::evaluate_deviation(model, contract, launched_resources)`. The
  pass/fail check against `budget.ceiling_with_variance` is arithmetic
  (`tools/finops.py::monthly_run_rate` — uses a per-resource `monthly_usd` from Transform when it's
  there, otherwise a small price table with a directional Reserved-Instance discount keyed on
  `budget.pricing_model`). The model only steps in to **attribute**
  a deviation; if resources are untagged or the attribution is illegible →
  `Finding(unattributable=True)`, it never fabricates a cause.
- **Remediation** — `agents/remediation.py::remediate(model, failure, allowed_actions, apply_action,
  kb, max_retries)`. Exception-handling loop with a **learning loop**: it first queries
  `tools/runbooks.py::InMemoryRunbookKB` (word-overlap matching) and applies the known precedent
  **without calling the model**; if there's no precedent or it doesn't resolve it, the model
  proposes an action — **the allow-list is checked in code** (`contract.allowed_actions`, not the
  prompt) — and on success a new runbook is written to the KB. `Remediation.from_runbook`
  distinguishes "resolved via precedent" from "resolved by reasoning from scratch": it's the
  scorecard's learning metric, already measurable
  (`tests/test_remediation.py::test_learning_loop_second_occurrence_skips_the_model`). Once retries
  are exhausted, `resolved=False` so the Orchestrator can escalate. `build_orchestrator` shares a
  single KB across runs (`orq.runbook_kb`); `apply_action` is a no-op until Sprint 2 wires up
  `ssm_run_command`.

## Verification oracle — `validate_lza_config`

`tools/validators.py::validate_lza_config(doc) -> ValidationOutcome(ok, error)`. Deterministic, runs
in milliseconds, is the oracle for the Interpreter's `converge` loop. `doc` is a mapping (or
YAML/JSON) with `global_config`, `accounts_config` and an optional `security_config`. **Minimal
structural check aligned with the plan's minimal stack (§4)**, not the full LZA schema:

- `global_config`: `homeRegion` present and inside `enabledRegions`; `controlTower.enable == false`.
- `accounts_config`: the 3 mandatory accounts (`Management`, `LogArchive`, `Audit`) present, each
  with a valid `email` — LZA doesn't create them in Organizations-only mode, it resolves them by
  email.
- `security_config` (if present): `guardduty` / `macie` / `securityHub` / `accessAnalyzer` with
  `enable == false`; `awsConfig.enableConfigurationRecorder == false`; `awsConfig.ruleSets` empty.

Valid fixture: [`fixtures/lza/valid_config.yaml`](fixtures/lza/valid_config.yaml). Expanding to the
full schema doesn't change the signature.

## Model, Interpreter and bake-off

- `agents/model.py::ModelLike` — minimal protocol `complete(prompt, *, system) -> str`. `FakeModel`
  (deterministic scripts + prompt capture) for tests; `StrandsModel` (Bedrock) as the adapter, not
  exercised without credentials. Agents depend on the protocol, not on Strands.
- `agents/interpreter.py::generate_lza_config(objective, model, spec=...)` — `converge` with
  `validate_lza_config` as the oracle, ≤5 iterations, never touching AWS. `build_interpreter(model)`
  yields the *agent-as-tool* callable `delegate_interpreter(objective, spec)`.
- `evals/bakeoff/` — `cases.jsonl` (24 cases, covering all 9 tools in `TOOL_CATALOG`),
  `run.py::score(model, cases) -> Report`. Without `--model`: validates the case file. With
  `--model bedrock:<id>`: scores tool correctness per case; exits with code 2 if `accuracy < 90%`
  (the plan's threshold for switching the Orchestrator to Haiku).

## Escalation and the human gate

- `tools/escalation.py::escalate(store, ts, Escalation(wave_id, context, hypothesis, attempts))`:
  once the iteration budget is exhausted, the agent escalates with a diagnosis — writes to the
  `decision_log` (`kind="escalation"`) and notifies. Every escalation is a scorecard data point, not
  an error.
- `tools/hitl.py::InMemoryHitlQueue`: local stand-in for the Transform MCP's `submit_hitl_task`. The
  only programmed human gate is `approve_derived_contract` — approving the derived contract (ten
  lines with their citations), not signing off on the business case.

## Autonomy scorecard

`evals/scorecard.py`. Section 3 of the plan, computed straight from the `decision_log` a run
already produces — no separate telemetry pipeline. `build_scorecard(store, wave_ids, *,
contract=None, known_healthy=None, cost_by_wave=None) -> Scorecard`:

| Metric | Function | Needs |
|---|---|---|
| Unintervened success rate | `unintervened_success_rate` | just the store |
| Escalations per run | `escalations_per_run` | just the store |
| Interpreter convergence rate + avg iterations | `interpreter_convergence` | `interpreter_convergence` log entries |
| Resolved vs. escalated, learning reuse rate | `remediation_outcomes` | `remediation_outcome` log entries |
| Constraint adherence | `constraint_adherence` | a `DecisionContract` — synthesizes the smallest action that violates each constraint and confirms `evaluate_policy` catches it (the generalized version of the plan's 3 FBCTF traps, works on any contract) |
| False green / false red | `false_verdicts` | a `known_healthy: dict[wave_id, bool]` ground-truth label |
| Cost per successful run | `cost_per_successful_run` | a `cost_by_wave` mapping |

**No silent caps:** whatever ground truth isn't supplied (labeled health, cost data, the injected
failure catalog for diagnosis precision) shows up as `None` plus an explicit note in
`Scorecard.notes` — never a fabricated number. `interpreter_convergence` and `remediation_outcome`
are logged by the Orchestrator itself (`_step_interpret` / `_step_replicate`), so any real run
already carries what the scorecard needs. `evals/wave_demo.py` prints one at the end of its run.

## LZA feature flag — data, not a branch

`feature_flags.lza_enabled` (off by default). The wave carries its step list from the specification;
with LZA off, the wave doesn't include `deploy_lza` and the Orchestrator never even learns the
option exists. There is no `if lza_enabled` anywhere in any prompt.

## Layout

```
agents/       trust (converge), model (ModelLike + Fake/Strands), extraction, interpreter,
              orchestrator (route + the Orchestrator class), remediation, validation, finops,
              runtime (AgentCore HTTP entry: POST /invocations, GET /ping)
tools/        spec, policy (pure engine), validators (LZA + IaC oracles), qa (diffs),
              finops (run-rate), runbooks (learning KB), escalation, hitl, aws (MGN/logs/SSM),
              transform_mcp (stdio + Gateway),
              transform_ingest (Transform assessment PPTX/XLSX/PDF + discovery CSVs -> prose)
state/        models, transitions (legal table), store (in-memory + DynamoDB)
dispatcher/   steps — deterministic executor, idempotent (swappable ledger), re-evaluates policy;
              handler — the Lambda entry + real wave step functions (MGN, Route53)
fixtures/     fbctf/ (synthetic), vmware-001/ + mixed-estate-001/ (real Transform assessments),
              discovery-001/ (real discovery export, same estate as mixed-estate-001),
              lza/valid_config.yaml, failures/catalog.json
evals/        bakeoff/ (cases.jsonl + run.py), scorecard.py (autonomy metrics), wave_demo.py
              (rehost + modernization + learning loop + scorecard); golden tests live under tests/
terraform/    main.tf / variables.tf / outputs.tf — DynamoDB, Guardrail, step-dispatcher Lambda +
              split IAM roles, EventBridge Scheduler, Budget. S3 backend: transform-agents-tfstate
tests/        181 tests, all offline (FakeModel for the LLM, moto for DynamoDB, fake clients for
              MGN/Route53/SSM/Lambda, synthetic workbooks/CSVs for the ingest)
```

## Current state

**Implemented and tested offline** (`FakeModel` for the LLM, `moto` for DynamoDB, fake boto3 clients
for MGN/Route53/SSM/logs): the entire decision path — typed contract + loader, closed vocabulary,
policy engine + traps, state machine, `converge` Trust loop, `validate_lza_config` +
`validate_iac` oracles, `escalate` + HITL queue, ingest of both a Transform assessment and a
discovery export, with the PDF Financial Summary as the authoritative cost basis (`transform_ingest`)
+ extraction `business_case → DecisionContract` (fixtures: synthetic FBCTF; real `vmware-001`,
`mixed-estate-001`, `discovery-001`), Interpreter (`generate_lza_config`
for rehost + `generate_modernization_iac` for the verified container artifact),
Orchestrator (`route` + resumable mode-1/2 `run_wave` producing both approaches in one wave, no AWS APIs), all
five roster agents, Remediation's **learning loop**, FinOps aligned to Transform's cost basis
(per-resource `monthly_usd` + RI-aware run-rate), the **autonomy scorecard** (incl.
`diagnosis_precision` over the injected-failure catalog), bake-off (24 cases + `score`).

**Connective layer — written and tested with mocks, needs credentials to run for real:**
`DynamoDbStateStore` + `DynamoDbLedger` (moto tests), `tools/aws.py` MGN read / jobs / logs / SSM
wrappers with allow-list enforcement (fake-client tests), `dispatcher/handler.py` — the step Lambda
with real step functions (`start_replication` → MGN, `cutover`/`rollback` → Route53) + cross-invocation
idempotency + structured policy-rejection, `dispatcher/steps.py::LambdaInvokingDispatcher` — the
Runtime-side adapter that invokes that Lambda (name or ARN), `agents/runtime.py` — the AgentCore HTTP
shell + resumable `invoke`, wired to either the Lambda (`STEP_DISPATCHER_FUNCTION` set) or in-process
steps, `StrandsModel` (Bedrock, agent cache + guardrail id), `tools/transform_mcp.py` (stdio +
AgentCore Gateway paths), `infra/*.tf`.

**Still open (shape only knowable against credentials):** exact AWS Transform *MCP* tool schemas
(the file ingest handles the console export; MCP may return the same data structured) and the real
MGN payloads — the wrappers are built against the documented APIs and adjust on first real response.
Also pending real infra: `terraform apply`, `agentcore configure/launch`, the S3 Vectors Knowledge
Base, `validate_iac` (needs the `terraform` binary), `playwright_run`, and wiring
Remediation's `apply_action` to `ssm_run_command`.

**Next:** the experiment itself — runs in the three modes (baseline / assisted / autonomous),
wall-clock bound.

## Usage

```bash
uv sync --extra dev          # includes moto; the whole suite runs offline
uv run pytest
uv run python -m evals.bakeoff.run
uv run python -m evals.wave_demo
```

- **Live models** — `uv sync --extra agents`, then `evals/bakeoff/run.py --model bedrock:<id>` and
  `diagnosis_precision(StrandsModel(...), contract)`.
- **Infra** — `cd terraform && terraform init` (S3 backend `transform-agents-tfstate`, key
  `poc/terraform.tfstate`, S3-native lock), then
  `terraform apply -var business_case_bucket=<bucket>`, then `agentcore configure` /
  `agentcore launch` against `Dockerfile` + `agents/runtime.py`, passing the `terraform output`
  values as env.
