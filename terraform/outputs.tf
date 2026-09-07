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

output "hitl_task_table" {
  value = aws_dynamodb_table.hitl_tasks.name
}

output "session_table" {
  value = aws_dynamodb_table.agent_sessions.name
}

output "escalation_topic_arn" {
  value = try(aws_sns_topic.escalations[0].arn, "")
}

output "harness_role_arn" {
  value = aws_iam_role.harness.arn
}

output "gateway_role_arn" {
  value = try(aws_iam_role.gateway[0].arn, "")
}
