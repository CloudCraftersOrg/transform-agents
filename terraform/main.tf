terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
  }

  backend "s3" {
    bucket       = "transform-agents-tfstate"
    key          = "poc/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

# --- State: two tables + the step idempotency ledger. Legal transitions live in code. ---

resource "aws_dynamodb_table" "wave_state" {
  name         = "${var.name_prefix}-wave_state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "wave_id"

  attribute {
    name = "wave_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "decision_log" {
  name         = "${var.name_prefix}-decision_log"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "wave_id"
  range_key    = "ts"

  attribute {
    name = "wave_id"
    type = "S"
  }
  attribute {
    name = "ts"
    type = "S"
  }
}

resource "aws_dynamodb_table" "step_ledger" {
  name         = "${var.name_prefix}-step_ledger"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "step_key"

  attribute {
    name = "step_key"
    type = "S"
  }
}

# --- Guardrail: prompt-injection screening on Transform artifacts and logs ---

resource "aws_bedrock_guardrail" "main" {
  name                      = "${var.name_prefix}-guardrail"
  blocked_input_messaging   = "This request was blocked by the transform-agents guardrail."
  blocked_outputs_messaging = "This response was blocked by the transform-agents guardrail."

  content_policy_config {
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }
  }
}

# --- Runtime role: least privilege for the single AgentCore Runtime ---

data "aws_iam_policy_document" "runtime_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "runtime" {
  name               = "${var.name_prefix}-runtime"
  assume_role_policy = data.aws_iam_policy_document.runtime_assume.json
}

data "aws_iam_policy_document" "runtime" {
  statement {
    sid       = "State"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.wave_state.arn, aws_dynamodb_table.decision_log.arn, aws_dynamodb_table.step_ledger.arn]
  }
  statement {
    sid       = "Model"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:ApplyGuardrail"]
    resources = ["*"]
  }
  statement {
    sid       = "MgnRead"
    actions   = ["mgn:DescribeSourceServers", "mgn:DescribeJobs", "mgn:DescribeReplicationConfigurationTemplates"]
    resources = ["*"]
  }
  statement {
    sid       = "InvokeStepLambda"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.step_dispatcher.arn]
  }
  # Mutating MGN / Route 53 actions are NOT here - they live on the step-dispatcher Lambda's role.
  statement {
    sid       = "RemediationSsm"
    actions   = ["ssm:SendCommand", "ssm:GetCommandInvocation"]
    resources = ["*"]
    # narrow to the allow-listed documents from contract.allowed_actions
    condition {
      test     = "StringLike"
      variable = "ssm:resourceTag/transform-agents"
      values   = ["allowed"]
    }
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:GetLogEvents", "logs:FilterLogEvents", "logs:DescribeLogStreams"]
    resources = ["*"]
  }
  statement {
    sid       = "BusinessCaseRead"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.business_case_bucket}", "arn:aws:s3:::${var.business_case_bucket}/*"]
  }
}

resource "aws_iam_role_policy" "runtime" {
  name   = "runtime"
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime.json
}

# --- Step dispatcher: the only component that calls mutating AWS APIs ---
# Package is built in CI (or `agentcore`-style) and uploaded to S3; this just points at it.

data "aws_iam_policy_document" "step_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "step_dispatcher" {
  name               = "${var.name_prefix}-step-dispatcher"
  assume_role_policy = data.aws_iam_policy_document.step_assume.json
}

data "aws_iam_policy_document" "step_dispatcher" {
  statement {
    sid       = "Ledger"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.step_ledger.arn]
  }
  statement {
    sid       = "MgnWaveActions"
    actions   = ["mgn:StartReplication", "mgn:StartTest", "mgn:StartCutover", "mgn:FinalizeCutover", "mgn:DescribeSourceServers", "mgn:DescribeJobs"]
    resources = ["*"]
  }
  statement {
    sid       = "DnsCutover"
    actions   = ["route53:ChangeResourceRecordSets", "route53:GetChange"]
    resources = ["arn:aws:route53:::hostedzone/*"]
  }
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-step-dispatcher:*"]
  }
}

resource "aws_iam_role_policy" "step_dispatcher" {
  name   = "step-dispatcher"
  role   = aws_iam_role.step_dispatcher.id
  policy = data.aws_iam_policy_document.step_dispatcher.json
}

resource "aws_lambda_function" "step_dispatcher" {
  function_name = "${var.name_prefix}-step-dispatcher"
  role          = aws_iam_role.step_dispatcher.arn
  handler       = "dispatcher.handler.handler"
  runtime       = var.lambda_runtime
  architectures = ["arm64"]
  timeout       = 60
  s3_bucket     = var.lambda_bucket
  s3_key        = var.lambda_key

  environment {
    variables = {
      STEP_LEDGER_TABLE = aws_dynamodb_table.step_ledger.name
    }
  }
}

# --- EventBridge Scheduler: re-invoke the Orchestrator every 5 min during long waits ---
# `run_wave` is resumable, so the schedule just re-calls invoke with the same wave_id.

resource "aws_scheduler_schedule" "resume_waves" {
  count = var.runtime_arn == "" ? 0 : 1

  name                = "${var.name_prefix}-resume-waves"
  schedule_expression = "rate(5 minutes)"
  state               = "DISABLED" # enable once a wave is in flight

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime"
    role_arn = aws_iam_role.scheduler[0].arn
    input    = jsonencode({ AgentRuntimeArn = var.runtime_arn, Payload = "{\"action\":\"resume\"}" })
  }
}

data "aws_iam_policy_document" "scheduler_assume" {
  count = var.runtime_arn == "" ? 0 : 1
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  count              = var.runtime_arn == "" ? 0 : 1
  name               = "${var.name_prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume[0].json
}

resource "aws_iam_role_policy" "scheduler" {
  count = var.runtime_arn == "" ? 0 : 1
  name  = "invoke-runtime"
  role  = aws_iam_role.scheduler[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["bedrock-agentcore:InvokeAgentRuntime"]
      Resource = var.runtime_arn
    }]
  })
}

# --- Budget guardrail against accidental OpenSearch Serverless spend ---

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name_prefix}-monthly"
  budget_type  = "COST"
  limit_amount = "100"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = []
  }
}
