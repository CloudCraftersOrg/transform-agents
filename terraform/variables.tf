variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name_prefix" {
  type    = string
  default = "transform-agents"
}

variable "business_case_bucket" {
  type        = string
  description = "S3 bucket holding business cases + the Knowledge Base source docs"
}

variable "runtime_arn" {
  type        = string
  description = "AgentCore Runtime ARN, for the EventBridge re-invocation schedule"
  default     = ""
}

variable "lambda_bucket" {
  type        = string
  description = "S3 bucket holding the step-dispatcher deployment package (built in CI)"
}

variable "lambda_key" {
  type        = string
  description = "S3 key of the step-dispatcher zip"
  default     = "step-dispatcher.zip"
}

variable "lambda_runtime" {
  type    = string
  default = "python3.12"
}

variable "budget_alert_email" {
  type        = string
  description = "Email for the monthly budget alert; empty disables the notification"
  default     = ""
}

variable "scheduler_state" {
  description = "ENABLED or DISABLED for the wave-resume schedule"
  type        = string
  default     = "ENABLED"
}

variable "escalation_email" {
  description = "Address to subscribe to the escalation topic. Empty means no subscription is created."
  type        = string
  default     = ""
}

variable "escalation_topic" {
  description = "Create the SNS escalation topic. Needs sns:CreateTopic; leave false without it - the agent then only writes escalations to the decision_log."
  type        = bool
  default     = false
}

variable "harness_arn" {
  description = "ARN of the AgentCore Harness, once created (agents/harness.py). Empty leaves the scheduler on the deterministic runtime."
  type        = string
  default     = ""
}

variable "mcp_runtime_arn" {
  description = "ARN of the AgentCore Runtime serving the MCP tool surface (SERVE_PROTOCOL=MCP). The gateway signs onward to it."
  type        = string
  default     = ""
}

variable "harness_name" {
  description = "Harness name. AgentCore derives its managed memory and workload identity from this, so the execution role is scoped to it."
  type        = string
  default     = "transform_agents_orchestrator"
}
