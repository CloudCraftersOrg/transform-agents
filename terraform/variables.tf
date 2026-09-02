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
