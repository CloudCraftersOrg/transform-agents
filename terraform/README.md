# infra/

Terraform for the connective infrastructure. Nothing here has been applied - it needs credentials.

## What it provisions

- **DynamoDB**: `wave_state` (PK `wave_id`), `decision_log` (PK `wave_id`, SK `ts`),
  `step_ledger` (PK `step_key`, the dispatcher's cross-invocation idempotency). The legal
  transition table stays in code (`state/transitions.py`).
- **Bedrock Guardrail**: `PROMPT_ATTACK` filter at HIGH on input - prompt-injection screening for
  Transform artifacts and logs. Its id goes into the runtime as `BEDROCK_GUARDRAIL_ID` and
  `StrandsModel(guardrail_id=...)` applies it.
- **Step-dispatcher Lambda** + its own role: the only component that calls *mutating* AWS APIs -
  `mgn:Start*` / `mgn:FinalizeCutover`, `route53:ChangeResourceRecordSets`, and its idempotency
  ledger (`step_ledger`). Package is built in CI and uploaded to S3 (`lambda_bucket` / `lambda_key`).
  Handler: `dispatcher.handler.handler`.
- **Runtime IAM role**: least-privilege for the single AgentCore Runtime - DynamoDB on `wave_state`
  and `decision_log`, `bedrock:InvokeModel`/`ApplyGuardrail`, MGN **read only**, `lambda:InvokeFunction`
  on the step dispatcher, SSM `SendCommand` (narrow to allow-listed docs via tag condition),
  CloudWatch Logs read, S3 read on the business-case bucket. No mutating MGN/Route53, no account
  deletion, no management SCPs, no source-environment writes (the deny-list is the absence of those
  grants).
- **EventBridge Scheduler**: `rate(5 minutes)`, created **DISABLED**. Enable it while a wave is in
  flight; it re-invokes the runtime with `{"action":"resume"}` and `run_wave` picks up where it
  left off. Never blocks a session during replication.
- **AWS Budget**: alert at 50% of $100/month - the tripwire against accidental OpenSearch
  Serverless (use S3 Vectors for the Knowledge Base, never *Quick create*).

## Not here (by design)

- **AgentCore Runtime / Gateway**: use the starter toolkit - `agentcore configure` then
  `agentcore launch` - against `Dockerfile` + `agents/runtime.py`. Pass `--env` for the table
  names and guardrail id from `terraform output`.
- **Knowledge Base (S3 Vectors)**: create after the runtime, point it at `business_case_bucket`.
- **LZA**: only if `feature_flags.lza_enabled` is turned on (Sprint 3). Then the Organizations-only
  prerequisites from the plan apply: three mandatory accounts by email, CodeBuild Linux/Large
  quota >= 3, no Control Tower, security services + config recorder off.

## Apply

```bash
cd infra
terraform init
terraform apply \
  -var business_case_bucket=<bucket> \
  -var lambda_bucket=<bucket-for-the-lambda-zip> \
  -var region=<region>
# after `agentcore launch`, re-apply with -var runtime_arn=<arn> to wire the schedule
```

The AgentCore Runtime gets `STEP_DISPATCHER_FUNCTION` = the `step_dispatcher_function` output (ARN;
a bare name also works, same account/region). Unset it to run the wave steps in-process instead of
in a separate Lambda - useful for a single-container dev deployment.
