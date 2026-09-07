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
`initialize_mgn`, `resize_replication_server`, `start_replication`, `launch_test`,
`cutover`, `rollback`, `finalize`).
Idempotent by `(wave_id, step_id)`; a step that raises is returned as `{"error": ...}` and
is **not** recorded, so a retry re-executes it. Re-evaluates policy server-side via the `guard: Action`. `deploy_lza` stays
registered even when LZA is off: it simply never gets dispatched.

## Orchestrator

`agents/orchestrator.py`. The **deterministic layer**: the state machine, the policy checks and
the wave sequence. The agentic surface lives in `agents/tools.py` + `agents/agentic.py`; this class
is what `run_migration_wave` calls. It **only holds** `store`, `dispatcher`, `hitl` and the
specialists (agent-as-tool) — **no AWS API**:

- `check(action, wave_id)` — `evaluate_policy`; on deny it writes `policy_denial`, and if the action
  falls under `modernization_unsupported` it **auto-escalates** (intent deliverable 5: declare the
  limit).
- `advance(wave_id, step)` — moves the wave's state through the transition table (`assert_legal`).
- `dispatch(wave_id, step_id, name, guard=...)` — delegates to the `Dispatcher` (idempotent +
  server-side re-eval).
- `delegate(which, objective)` — invokes the specialist; a clear `NotImplementedError` if it isn't
  wired yet.
- `request_approval` / `escalate` / `wave_digest` (digest from the store, not from MGN).
- `_clear_blocker(...)` — AWS Transform stops a wave on prerequisites it cannot satisfy
  itself and states them in prose it rewrites each turn, so `BLOCKERS` matches on terms
  that must co-occur rather than on a phrasing. A match dispatches the step that clears it
  (policy-checked, idempotent) and replies with the exact phrase Transform asked for. A
  refusal is published as a job artifact and escalated — never talked past.
- `_classify_gate(...)` — AWS Transform advances a job through dozens of conversational gates
  (accept these replication settings? Static or Dynamic IP? which staging disk type?). The agent
  decides each one autonomously: a plain progression option (`ADVANCE_OPTIONS`) is taken as-is;
  an either/or configuration choice goes to the model bounded to the offered strings, told to pick
  the AWS-recommended, reversible, low-cost default. It escalates only on a genuine judgement call
  — a `STOP_SIGNAL` (destructive / irreversible / safety-skipping), a `MANUAL_BLOCKER` (needs
  console access), the cutover when the plan failed the contract, or a gate the model itself
  returns `ESCALATE` on. `_chat_is_stalled(...)` catches the other failure mode: the same option
  chosen three times with no change means a step outside the agent (an unreachable source estate,
  agents that never register) has to complete first. Before handing that to a person it
  dispatches `resize_replication_server` — one shared replication server carries the whole wave,
  and on a burstable type its CPU credits drain mid-sync until agents start failing to connect.
  The step only acts on that exact signature (`FAILED_TO_CONNECT_AGENT_TO_REPLICATION_SERVER`
  present *and* the template still burstable), so a stall with any other cause still reaches a
  human with a `workflow stalled` record.

`build_orchestrator(model, contract, *, store, dispatcher, hitl, clock, specialists=...)`.

**Wave loop** — `run_wave(wave_id, inputs, *, replication_ready=..., require_contract_approval=False)`.
Walks `contract.steps` (`precheck → interpret → replicate → test → cutover → parity → finops`),
advances state through the transition table, and persists `completed_steps` step by step.
**Resumable, never blocks:** at `replicate` it dispatches `start_replication` and returns `WAITING`
(EventBridge re-invokes it; re-entering picks up where it left off). A red verdict at `test`/`parity`
triggers a rollback and returns `ROLLED_BACK`. Interpreter failing to converge, cutover blocked by
policy, a Transform gate that needs human judgement, or a workflow that stalls on something outside
the agent → `ESCALATED`. `require_contract_approval=True` = **mode 1** (HITL gate on the derived
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
- `evals/bakeoff/` — `cases.jsonl` (24 cases, covering all 8 tools the deployed agent has;
  the catalog is derived from `agents/tools.py`, so the eval cannot drift away from it),
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

## Application sanity check — what the cutover is judged against

`issue_verdict` on an empty diff returns **green**. Nothing populated `test_diffs` in the deployed
path, so the QA gate approved every cutover having tested nothing. That is now closed at the point
where the vacuous verdict would have been issued:

- **`precheck`** asks the engineer, through a `app_probe_spec` HITL task, for what "working" looks
  like: a URL per application, the expected response, and — for anything needing a login — a
  **Secrets Manager ARN**. Never the credential: the wave inputs and the `decision_log` are both
  persisted and rendered in the console. The step (`dispatcher/handler.py::_probe_apps`) resolves
  the reference at the moment of use and returns only signals that are safe to keep — status code,
  latency, a SHA-256 fingerprint of the body, never the body.
- The pre-migration observation is written to the `decision_log` as `app_baseline`, so a resumed
  wave keeps comparing against the original rather than re-measuring an already-migrated system.
- **`test`** probes again and diffs the two. If there is **no baseline and no explicit
  `test_diffs`, the wave escalates instead of issuing a verdict** — refusing to judge is the whole
  point of the gate.

The probe runs from the step-dispatcher Lambda, which is not attached to a VPC: it reaches
applications that are publicly routable. Private-only estates need the Lambda in the VPC first.

**The agent works out what to check, and asks only when it cannot.** The chain is already in the
estate: an MGN application groups source servers, a source server carries the EC2 instance its
agent registered from, and EC2 knows that instance's public address. `discover_probe_targets`
walks it, probes each address, and keeps only the hosts that answer - discovery by observation, so
a machine that is not serving the application never becomes something the cutover is judged on. On
this estate it derives three applications out of thirteen servers in 17 seconds and correctly
leaves the message queue, cache, NFS and CI hosts out.

A derived spec is recorded in the decision log marked `source: derived`, because a person reading
the record has to be able to see that nobody supplied it. An engineer's answer always wins over
what the agent derived. Asking is the fallback, for an estate with nothing publicly reachable.

**Answering the agent, from the console.** Every question in the *Needs you* panel has a reply box
under it. You answer in a sentence, not a form: the agent already works out *where* its
applications answer, and what it cannot work out is what a person means by healthy - "the
leaderboard lists teams", "the catalog shows products". The reply is recorded against the wave and
reaches the QA verdict as the description the before/after diff is judged against. A form was the
wrong instrument and it is gone.

**The login is a reference, never a credential.** The `secret_arn` field takes a Secrets Manager
ARN whose `SecretString` is JSON with exactly `username` and `password`; the probe resolves it at
the moment of use and sends HTTP Basic. The name must start with `${name_prefix}/` - the runtime's
`GetSecretValue` grant is scoped to that prefix. A secret that cannot be read, or that uses other
key names, is reported as `login_error` on the probe result: it used to fall back to an
unauthenticated request, which comes back 401 and reads as the application being broken, on the
gate that decides the cutover.

**One row per application, not per server.** The verdict compares an application before and after,
so thirteen servers behind three applications are three things to check. The answer is written to
the `decision_log`, not to the task row: a task is answered once, the log is what every later run
reads back. Both surfaces read it through the same `_answered_probe_spec` - they used not to, and
a wave resumed by the scheduler probed nothing while the agentic path saw the answer.

## Agentic entrypoint — AgentCore Runtime + Strands

`agents/agentic.py`. `BedrockAgentCoreApp` owns the `/invocations` + `/ping` contract (the
hand-rolled HTTP server is gone) and a Strands `Agent` owns the reasoning loop. You talk to it:

```bash
aws bedrock-agentcore invoke-agent-runtime --agent-runtime-arn <arn>   --payload "$(printf '{"prompt":"why is wave-0 not progressing?"}' | base64 -w0)"   --content-type application/json --accept application/json out.json
```

**Nine tools** (`agents/tools.py`), which is the whole surface the model reasons over:

| Tool | What it is for |
|---|---|
| `waves_in_flight` | what is still running — the scheduler wakes the agent with no wave in mind |
| `migration_status` | wave state + what AWS Transform is asking + an MGN summary, in one read |
| `mgn_replication_health` | MGN's own per-server view — **the ground truth when Transform's narrative disagrees** |
| `run_migration_wave` | the deterministic wave, resumable, carrying the state machine |
| `sanity_check_apps` | probe the applications; baseline before, judgement after |
| `answer_transform` | reply to a Transform gate |
| `diagnose_and_remediate` | hand a failure to the Remediation specialist (agent-as-tool) |
| `ask_engineer` | get what only a person knows — URLs, expected responses, a secret *reference* |
| `escalate` | hand over with a diagnosis |

**`run_migration_wave` stays a tool rather than dissolving into the loop.** It carries the state
machine and the resume point the scheduler depends on. The agent decides *when* to run a wave; it
does not re-derive how a wave is sequenced internally.

**The switch is additive.** Every payload the deterministic entrypoint understands — the
scheduler's `{"action":"resume"}`, a direct `{"wave_id":...}`, the console's `ask` / `say` /
`read_chat` — still routes to `agents/runtime.py` unchanged. The agentic surface has to earn its
place before anything that works today is removed; `tools/console.py` becomes optional, not
obsolete-by-decree.

## AgentCore Harness — AWS runs the loop, the tools arrive over MCP

Two ways to run the same agent, from one image. `SERVE_PROTOCOL` picks the role:

| | `SERVE_PROTOCOL=HTTP` (default) | `SERVE_PROTOCOL=MCP` |
|---|---|---|
| Serves | `agents/agentic.py` on `:8080` | `tools/mcp_server.py` on `:8000/mcp` |
| Runs the loop | Strands, in our container | AWS, in the Harness |
| Conversation memory | `agent_sessions` table | Harness managed memory |
| Who calls the tools | the Strands `Agent` | the Harness, over MCP |

**The tools are not redefined for MCP.** `tools/mcp_server.py` re-publishes
`agents.tools.build_tools` over a different transport, so there is one definition and every guard
inside those tools is a guard the Harness inherits. `tests/test_mcp_server.py` fails if the two
surfaces ever differ by a name, a description or a schema.

**`allowedTools` is a safety control, not a token optimisation.** A Harness ships `shell` and
`file_operations` in every session. A shell on a live migration reaches MGN and Route 53 without
passing one dispatcher guard, so the harness allows `@transform-agents` and nothing else.

### The credential chain

`remoteMcp` takes a URL and static headers — it cannot SigV4-sign — and an AgentCore Runtime
serving MCP requires SigV4 or a JWT. A bearer token would expire under an unattended agent, so the
chain goes through a Gateway instead and every hop is IAM:

```
EventBridge → resume Lambda → InvokeHarness → Harness ──awsIam──▶ Gateway
                                                                    │ GATEWAY_IAM_ROLE (SigV4)
                                                                    ▼
                                                       AgentCore Runtime (MCP) → tools
```

`HARNESS_MCP_URL` + `HARNESS_MCP_HEADERS` remain as a fallback for an MCP server that
authenticates on a header.

### Deploy

```bash
# one image, pushed once; the MCP runtime differs from the HTTP one only by environment
aws bedrock-agentcore-control create-agent-runtime \
  --agent-runtime-name transform_agents_mcp \
  --agent-runtime-artifact "containerConfiguration={containerUri=<image>}" \
  --role-arn "$(terraform output -raw runtime_role_arn)" \
  --network-configuration '{"networkMode":"PUBLIC"}' \
  --protocol-configuration '{"serverProtocol":"MCP"}' \
  --environment-variables SERVE_PROTOCOL=MCP,WAVE_STATE_TABLE=...,DECISION_LOG_TABLE=...

terraform apply -var mcp_runtime_arn=<arn>          # creates the gateway role
python -m agents.gateway "$(terraform output -raw gateway_role_arn)" <mcp-runtime-arn>
HARNESS_GATEWAY_ARN=<arn> python -m agents.harness "$(terraform output -raw harness_role_arn)"
terraform apply -var harness_arn=<arn>              # the scheduler switches to InvokeHarness
```

`agents/gateway.py` and `agents/harness.py` are both create-or-reuse, so re-running one is how
you redeploy it, not something to avoid.

Deploying needs paired permissions the console does not hint at: `CreateHarness` also requires
`bedrock-agentcore:CreateAgentRuntime` **and** `CreateMemory`; `UpdateHarness` requires
`UpdateAgentRuntime` and `UpdateMemory`; and **`InvokeHarness` is checked against both
`bedrock-agentcore:InvokeHarness` and `bedrock-agentcore:InvokeAgentRuntime` on the same ARN** —
granting only the first is denied.

Three more that cost an attempt each:

- **`mcp` is a reserved gateway-target name.** `CreateGatewayTarget` rejects it outright; the
  target here is called `tools`.
- **Managed memory is not named what the docs say.** The execution-role sample scopes memory to
  `memory/harness_<abbrev>_*`; the service actually creates `memory/<harnessName>-<suffix>`, so a
  role written from the docs is denied `ListEvents` on the first invocation.
- **Gateway tools arrive namespaced.** A tool published as `waves_in_flight` reaches the model as
  `tools___waves_in_flight` — the target name, three underscores, the tool. `allowedTools` still
  matches on the harness tool name (`@transform-agents`), not on this.

### What the Harness does not do

It does not wake anything up. A Harness sitting still does nothing, exactly like a Runtime sitting
still; it changes *who runs the loop*, not *who starts it*. The EventBridge schedule is still the
only reason the migration advances unattended — it now calls `InvokeHarness` instead of
`InvokeAgentRuntime`, and `dispatcher/resume.py` keeps both paths so the switch is reversible by
unsetting one environment variable.

Ticks share one `runtimeSessionId` per day, so the agent remembers what it tried five minutes ago
instead of rediscovering it. `RESUME_BATCH` no longer applies on this path: the agent chooses how
many waves to drive, having asked `waves_in_flight` first.

Each new MCP runtime session is a cold microVM, so the first tool call in it pays a full Transform
workspace walk. The derived contract is read back from `contract#<jobId>` rather than re-derived,
which is what keeps that cost to a walk instead of a walk plus an extraction loop. `stopReason` is reported on every turn: `max_iterations_exceeded`
looks exactly like success until you read it, and that is how a stalled migration goes unnoticed.

## Guards live in the tool, never in the call order

Several guarantees used to hold only because `run_wave` called things in a fixed sequence. That is
safe for a deterministic pipeline and unsafe the moment an agent picks its own order — it could
reach `cutover` by simply never mentioning `test`. The rule now: **every guard lives inside the tool
that performs the effect.**

| Guarantee | Enforced in | Survives an agent choosing its own order |
|---|---|---|
| Contract policy (sizing, budget, scope) | `Dispatcher.dispatch` re-evaluates `evaluate_policy` server-side | yes, already |
| Legal state transitions | `put_wave` → `assert_legal` | yes, already |
| **The migration was actually tested** | `dispatcher/handler.py::_cutover` queries the `decision_log` for a green `test` verdict **judged on a non-empty diff**, and raises `StepRejected` otherwise | yes, now |
| Nothing destructive answered autonomously | `_classify_gate` `STOP_SIGNALS` / `MANUAL_BLOCKERS` | still order-independent, but lives in the orchestrator — move it into the tool when the loop lands |

The cutover check **fails closed**: with no `DECISION_LOG_TABLE` configured it refuses. That is the
right default for an irreversible action, and it means an in-process run has to wire the log too.

## Autonomy scorecard

`evals/scorecard.py`. Section 3 of the plan, computed straight from the `decision_log` a run
already produces — no separate telemetry pipeline. `build_scorecard(store, wave_ids, *,
contract=None, known_healthy=None, cost_by_wave=None) -> Scorecard`:

| Metric | Function | Needs |
|---|---|---|
| Unintervened success rate | `unintervened_success_rate` | just the store |
| Escalations per run | `escalations_per_run` | just the store |
| Gate autonomy rate | `gate_autonomy` | just the store — share of AWS Transform's conversational gates the agent answered itself, split into plain progression vs. model-chosen configuration, with the reason for every gate it *did* hand to a human. A stall (the workflow blocked on something outside the agent) is counted separately: it is not a decision the agent declined |
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

**Running on AWS.** The Orchestrator is deployed as an AgentCore Runtime container against the
live `EPAM-PoC-Business-Case` workspace and the `VmwareMigration-2026-09-02-2050` job. In one
invocation it walks the job's artifacts over the Transform MCP, derives the decision contract
from the wave plan, checks every instance the plan would launch against it, and drives the job
through its chat. Nothing about the estate is configured.

What the agent has actually decided against the real workspace:

- **Withheld the Wave 0 cutover.** The plan's EC2 recommendations were generated with *Average*
  sizing while the enriched inventory records peak CPU of 94-100%; 9 of 12 servers failed the
  contract. The denial went to a human gate instead of proceeding.
- **Chose to fix the plan rather than override the denial.** With `resize_to_peak` approved it
  asked Transform to regenerate the recommendations on peak utilisation. Transform re-issued all
  12 servers as `c5a.large`, and the re-derived plan then passed the same contract unchanged.
- **Detected and tried to clear an external prerequisite.** Transform blocks the wave until AWS
  MGN is initialized in the target account. The agent recognised the blocker, dispatched
  `initialize_mgn`, and — when MGN refused — published the evidence as a job artifact and
  escalated. It never answered `continue rehost` on work it had not done.

**Open, and not fixable from this repo:** MGN will not activate in `us-east-1` for this account.
`InitializeService` returns 200 and no-ops, and every MGN write is refused with an
`AccessDeniedException` carrying an empty message. The same call in `us-west-2` initializes the
account and creates the default replication template, so this is neither IAM nor the code path.
AWS Transform hits the identical wall and asks for an administrator, so the rehost stops here
until the service is turned on from the console (or in another region).

**Still offline-only:** the S3 Vectors Knowledge Base, `playwright_run`, and wiring
Remediation's `apply_action` to `ssm_run_command`.
## Running on AgentCore

The Orchestrator runs as a single AgentCore Runtime container. `POST /invocations` with just
`{"wave_id": "..."}`: it discovers the Transform workspace, ingests the wave plan from the job's
artifacts, derives the decision contract from that prose, and runs the wave. Nothing about the
estate is configured.

```bash
aws bedrock-agentcore invoke-agent-runtime --agent-runtime-arn <arn>   --payload "$(printf '{"wave_id":"w1"}' | base64 -w0)"   --content-type application/json --accept application/json out.json
```

**Unattended operation.** An EventBridge schedule fires every 5 minutes at a small
`{name_prefix}-resume` Lambda, which calls `InvokeAgentRuntime` with `{"action": "resume"}`.
A **native** Lambda target on purpose: the `aws-sdk:bedrockagentcore:invokeAgentRuntime`
universal target is accepted at apply time and then never delivers — the schedule fires, the
role is correct, and no invocation reaches the runtime. That Lambda ships in the same zip as
the step dispatcher with a different handler; the separation that matters is the IAM role, not
the artifact — it can wake the Orchestrator and nothing else, while the step dispatcher, which
*can* mutate MGN and Route 53, deliberately cannot invoke the runtime.
That payload carries no wave id: the runtime reads `waves_in_flight()` and drives them, skipping
anything DONE / FAILED / ROLLED_BACK / ESCALATED — escalated means a person owes an answer, so
auto-resuming it would defeat the gate.

**That gate is only as good as the escalation that sets it.** `Orchestrator.escalate` wrote the
decision log and left the wave's status alone, so five of the six ways a wave escalates never took
it out of the in-flight set. One wave rediscovered the same Transform stall, re-published a stall
record and re-escalated on every five-minute tick — **102 rounds** before anyone read the log.
Marking the wave is now part of escalating rather than a step each call site has to remember, for
the same reason the guards live inside the tools: a rule enforced by convention is not enforced. The batch is bounded (`RESUME_BATCH`, default 2) because
one invocation has a request timeout and each wave costs a full MCP walk, and waves are ordered
**least-recently-serviced first**: resuming stamps `updated_at`, so ordering by newest would
hand every tick back to the same waves and starve the rest. Create it by passing `runtime_arn`
to `terraform apply`; without it the schedule is not created at all and nothing runs unattended.

`{"read_chat": true}` returns the agent's own chat thread, the job status and the artifact
listing without sending anything — a conversation is single-flight, so any probe that writes is
what makes the next real message fail with *"a message is already being processed"*.

The derived contract is cached in the `decision_log` under `contract#<jobId>`, not under the
wave. Waves come and go; the plan they are derived from does not, so a re-run reuses the same
contract instead of asking the model to invent it again — extraction does not produce a
byte-identical answer twice, and a thinner contract silently weakens every gate downstream.

Redeploying is image push + `update-agent-runtime`. **Pass `--environment-variables` every
time**: the update replaces the runtime configuration, and omitting them silently clears the
whole environment.

Four things about the Transform MCP are not obvious and cost a day if you rediscover them:

**`load_instructions` is a hard gate.** Every job-scoped call returns `INSTRUCTIONS_REQUIRED` until
it is called for that job. `TransformWorkspace` does it automatically. Those instructions are
service-supplied text that reaches the model, which is why the Bedrock Guardrail exists.

**Workspace access is per assumed-role *session*, not per role.** Transform keys collaborators on
`<role-id>:<session-name>` and refuses role ARNs, bare role ids and wildcards. AgentCore mints a
fresh session name (`BedrockAgentCore-<uuid>`) on every invocation, so a collaborator entry can
never match. The runtime therefore re-assumes its own role under a fixed `TRANSFORM_SESSION_NAME`
(`stable_session_env()` in `tools/transform_mcp.py`) and hands those credentials to the MCP
subprocess. Grant that identity once:

```
manage_collaborator(workspaceId, action="put",
                    userId="<runtime-role-id>:transform-agent", role="CONTRIBUTOR")
```

The identity must have called Transform at least once before it can be added ("has not logged in to
tenant"), so the order is: deploy, invoke once (it 403s), add the collaborator, invoke again.

**Chat threads are per-identity; artifacts are not.** Nothing the agent says in chat is visible
in the console, because the human and the runtime hold different Transform identities and see
different threads of the same job. Decision records are therefore *published as job artifacts*
(`upload_artifact`), which every collaborator shares. File names must be unique, so each record
carries a timestamp.

**A migration job cannot be restarted.** `control_job("start")` answers HTTP 400 *"job of this
type cannot be restarted"*, and `AWAITING_HUMAN_INPUT` is not a stalled job — it is the job
asking a question in chat. Both statuses are driven by answering, not by starting.

AgentCore Gateway is **not** an option for this MCP: its `mcpServer` target requires an
`https://` endpoint and the AWS Transform MCP ships as a stdio package. The Dockerfile installs it
into the image instead, so the runtime never reaches PyPI.

## Operator console

`tools/console.py` — a local page over the deployed agent, because a wave is a narrative and a
terminal is a bad place to read one. Zero new dependencies: stdlib HTTP server, boto3, one HTML
file. It reads the same DynamoDB tables the agent writes, so there is no second source of truth.

```bash
AWS_PROFILE=<profile> AGENT_RUNTIME_ARN=<runtime arn> uv run python -m tools.console
```

- **Wave pipeline** — where the wave sits in the state machine, which steps are behind it, and
  whether it left the pipeline through an off-ramp (`ESCALATED`, `ROLLING_BACK`, `FAILED`).
- **Decision log** — every entry oldest-first, coloured by kind, each with its raw `detail`. This is
  the audit trail the scorecard is computed from, not a rendering of it.
- **Run the agent** — invokes the runtime with `{wave_id, approve}`, the same call the scheduler
  makes, so the console cannot drive a wave down a path an unattended run could not also take. The
  gate checkboxes are the HITL approvals (`proceed_wave_cutover`, `resize_to_peak`).
- **AWS Transform job** — job status, the question Transform is currently blocked on, and the
  decision records the agent has published to the job's artifacts.
- **Agent output** — the runtime log group with the MCP server and botocore chatter filtered out.
  Tailing it raw looks empty because roughly nine in ten lines are theirs, not the agent's.

A fresh boto3 session is built per call: the console outlives an SSO token, and a cached session
would keep serving errors after a re-login.

## Usage

```bash
uv sync --extra dev          # includes moto; the whole suite runs offline
uv run pytest
uv run python -m evals.bakeoff.run
uv run python -m evals.wave_demo
```

- **Live models** — `uv sync --extra agents`, then `evals/bakeoff/run.py --model bedrock:<id>` and
  `diagnosis_precision(StrandsModel(...), contract)`.
- **Infra** — `cd terraform && terraform init` (Terraform >= 1.10; S3 backend
  `transform-agents-tfstate`, key `poc/terraform.tfstate`, S3-native lock), then
  `terraform apply -var business_case_bucket=<bucket> -var lambda_bucket=<bucket>`, then
  `agentcore configure` / `agentcore launch` against `Dockerfile` + `agents/runtime.py`,
  passing the `terraform output` values as env.
