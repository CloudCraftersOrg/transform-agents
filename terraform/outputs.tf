output "wave_state_table" {
  value = aws_dynamodb_table.wave_state.name
}

output "decision_log_table" {
  value = aws_dynamodb_table.decision_log.name
}

output "step_ledger_table" {
  value = aws_dynamodb_table.step_ledger.name
}

output "guardrail_id" {
  value = aws_bedrock_guardrail.main.guardrail_id
}

output "runtime_role_arn" {
  value = aws_iam_role.runtime.arn
}

output "step_dispatcher_function" {
  value = aws_lambda_function.step_dispatcher.arn
}

# Feed these into the runtime as env: WAVE_STATE_TABLE, DECISION_LOG_TABLE, BEDROCK_GUARDRAIL_ID,
# STEP_DISPATCHER_FUNCTION (= step_dispatcher_function above; a name works too, ARN is unambiguous).
# Leave STEP_DISPATCHER_FUNCTION unset to run steps in-process instead (single-container mode).
