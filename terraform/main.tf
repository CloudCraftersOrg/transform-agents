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

resource "aws_dynamodb_table" "hitl_tasks" {
  name         = "${var.name_prefix}-hitl_tasks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "task_key"

  attribute {
    name = "task_key"
    type = "S"
  }
}

# One row per conversation. Without it every invocation builds a cold agent and you cannot ask a
# follow-up question - AgentCore hands us a session id per request and this is what it keys.
resource "aws_dynamodb_table" "agent_sessions" {
  name         = "${var.name_prefix}-agent_sessions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"

  attribute {
    name = "session_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# --- Escalations reach a person ---
# The Notifier hook has existed since the first sprint with nothing ever passed to it, so every
# escalation meant "someone has to go and look at DynamoDB". Subscribe an email or a chat webhook.

resource "aws_sns_topic" "escalations" {
  count = var.escalation_topic ? 1 : 0
  name  = "${var.name_prefix}-escalations"
}

resource "aws_sns_topic_subscription" "escalations_email" {
  count     = var.escalation_topic && var.escalation_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.escalations[0].arn
  protocol  = "email"
  endpoint  = var.escalation_email
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
  # AWS Transform keys workspace collaborators on <role-id>:<session-name>, and AgentCore mints a
  # fresh session name per invocation. The runtime re-assumes itself under a fixed session name so
  # the workspace can grant the agent access once instead of chasing a new UUID every call.
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::${local.account_id}:role/${var.name_prefix}-runtime"]
    }
  }
}

resource "aws_iam_role" "runtime" {
  name               = "${var.name_prefix}-runtime"
  assume_role_policy = data.aws_iam_policy_document.runtime_assume.json
}

data "aws_iam_policy_document" "runtime" {
  statement {
    sid = "State"
    # Scan is for waves_in_flight and the HITL queue: the scheduler fires with no wave id, and the
    # console asks "what is blocked" without knowing which wave.
    actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [
      aws_dynamodb_table.wave_state.arn,
      aws_dynamodb_table.decision_log.arn,
      aws_dynamodb_table.step_ledger.arn,
      aws_dynamodb_table.hitl_tasks.arn,
      aws_dynamodb_table.agent_sessions.arn,
    ]
  }
  dynamic "statement" {
    for_each = aws_sns_topic.escalations
    content {
      sid       = "EscalationNotice"
      actions   = ["sns:Publish"]
      resources = [statement.value.arn]
    }
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
    resources = ["arn:aws:lambda:${var.region}:${local.account_id}:function:${var.name_prefix}-step-dispatcher"]
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
  # The Orchestrator reads the workspace, its artifacts and its HITL tasks through the Transform
  # MCP, which signs with this role. Without it the MCP authenticates as nobody inside the runtime.
  statement {
    sid       = "StableTransformSession"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/${var.name_prefix}-runtime"]
  }

  statement {
    sid = "TransformWorkspace"
    actions = [
      "transform:Get*", "transform:List*", "transform:AccessTransformProfile",
      "transform:StartJob", "transform:StopJob", "transform:UpdateJob",
      "transform:CreateArtifact", "transform:CompleteTask",
    ]
    resources = ["*"]
  }

  # AgentCore pulls the runtime container image with the execution role.
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "PullRuntimeImage"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = ["arn:aws:ecr:${var.region}:${local.account_id}:repository/${var.name_prefix}-runtime"]
  }
  statement {
    sid       = "RuntimeLogs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/*"]
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
    # Read-only, and only the log: the cutover refuses unless this wave has a green test verdict
    # judged on a real diff. Enforced here for the same reason policy is - a caller that skipped
    # the test must not reach the cutover by simply not mentioning it.
    sid       = "TestVerdictCheck"
    actions   = ["dynamodb:Query"]
    resources = [aws_dynamodb_table.decision_log.arn]
  }
  statement {
    sid = "MgnWaveActions"
    # ListApplications is how the sanity check learns which servers belong to which application -
    # the grouping the wave is defined by, and the name the before/after diff is reported under.
    actions   = ["mgn:StartReplication", "mgn:StartTest", "mgn:StartCutover", "mgn:FinalizeCutover", "mgn:DescribeSourceServers", "mgn:DescribeJobs", "mgn:ListApplications", "mgn:TerminateTargetInstances"]
    resources = ["*"]
  }
  statement {
    # AWS Transform stops the wave and asks for a human when MGN is uninitialized. Initializing it
    # is a prerequisite, not a migration decision, so the dispatcher clears it for the agent.
    sid       = "MgnInitialization"
    actions   = ["mgn:InitializeService", "mgn:CreateReplicationConfigurationTemplate", "mgn:UpdateReplicationConfigurationTemplate", "mgn:DescribeReplicationConfigurationTemplates", "mgn:TagResource"]
    resources = ["*"]
  }
  statement {
    # InitializeService creates MGN's own service-linked roles, and refuses outright without this.
    sid       = "MgnServiceLinkedRoles"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/aws-service-role/*"]
    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["mgn.amazonaws.com", "drs.amazonaws.com"]
    }
  }
  statement {
    # Initialization also reconciles MGN's own replication roles and their instance profiles. The
    # names are fixed by the service, so the grant is scoped to them rather than to iam:*.
    sid = "MgnServiceRoles"
    actions = [
      "iam:GetRole", "iam:CreateRole", "iam:AttachRolePolicy", "iam:PutRolePolicy",
      "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole", "iam:PassRole",
      "iam:GetInstanceProfile", "iam:CreateInstanceProfile", "iam:AddRoleToInstanceProfile",
    ]
    resources = [
      "arn:aws:iam::${local.account_id}:role/service-role/AWSApplicationMigration*",
      "arn:aws:iam::${local.account_id}:instance-profile/AWSApplicationMigration*",
    ]
  }
  statement {
    sid = "StagingAreaLookup"
    # Two jobs, both read-only. DescribeInstances is how the sanity check finds what to check: an
    # MGN application groups source servers, a source server carries the instance it registered
    # from, and EC2 knows its public address. The rest is what MGN reads back through the *caller*
    # during a launch - a test launch failed on ec2:DescribeSnapshots denied to this role, not to
    # MGN's own service role, so the caller needs its own view of the snapshots, volumes and images
    # a launch is assembled from. Mutating the instances remains MGN's, not ours.
    actions = [
      "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeInstances",
      "ec2:DescribeSnapshots", "ec2:DescribeVolumes", "ec2:DescribeImages",
      "ec2:DescribeInstanceTypes", "ec2:DescribeInstanceAttribute", "ec2:DescribeInstanceStatus",
      "ec2:DescribeLaunchTemplates", "ec2:DescribeLaunchTemplateVersions",
      "ec2:DescribeNetworkInterfaces", "ec2:DescribeAvailabilityZones", "ec2:DescribeTags",
      "ec2:DescribeAccountAttributes", "ec2:DescribeKeyPairs", "ec2:DescribePlacementGroups",
    ]
    resources = ["*"]
  }
  statement {
    # The sanity check resolves a login at the moment of use. The engineer supplies the reference,
    # never the credential, so nothing secret reaches the wave inputs or the decision_log.
    sid       = "SanityCheckLogins"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${var.name_prefix}/*"]
  }
  statement {
    # Remediation's hands. RetryDataReplication restarts a stalled source server; SSM restarts the
    # replication agent on a host. Both are in the contract's allowed_actions and both go through
    # the dispatcher, so the specialist chooses what, never whether it is permitted.
    sid = "WaveInventory"
    # A VMware import leaves the wave tracking placeholder source servers while the machines that
    # actually replicated sit outside every application. Moving them between applications is the
    # only way to reconcile that, and it is membership only - it starts nothing and launches
    # nothing. It lives on the dispatcher role because that is where every mutating MGN call lives.
    # MGN authorizes membership against both sides of the move - the application and every source
    # server named in the call - so scoping to the application alone is denied.
    actions = ["mgn:AssociateSourceServers", "mgn:DisassociateSourceServers"]
    resources = [
      "arn:aws:mgn:${var.region}:${local.account_id}:application/*",
      "arn:aws:mgn:${var.region}:${local.account_id}:source-server/*",
    ]
  }
  # Launching is not something MGN does entirely on its own behalf: StartTest and StartCutover run
  # parts of the launch under the *caller's* identity, and without these the job reports
  # CONVERSION_FAIL with no reason and never starts a conversion server. This list is AWS's own
  # `AWSApplicationMigrationEC2Access`, not a guess assembled from failures - the same actions, so
  # the grant is auditable against a published policy. It is broad because launching an instance
  # is broad; it lives here because the dispatcher is the one component allowed to change
  # infrastructure, and it is exactly the work it exists to do.
  statement {
    sid = "MgnLaunchesInstances"
    actions = [
      "ec2:RunInstances", "ec2:StartInstances", "ec2:StopInstances", "ec2:TerminateInstances",
      "ec2:ModifyInstanceAttribute", "ec2:CreateTags",
      "ec2:CreateVolume", "ec2:DeleteVolume", "ec2:AttachVolume", "ec2:DetachVolume",
      "ec2:ModifyVolume", "ec2:CreateSnapshot", "ec2:DeleteSnapshot",
      "ec2:CreateLaunchTemplate", "ec2:CreateLaunchTemplateVersion", "ec2:ModifyLaunchTemplate",
      "ec2:DeleteLaunchTemplate", "ec2:DeleteLaunchTemplateVersions",
      "ec2:CreateSecurityGroup", "ec2:AuthorizeSecurityGroupIngress",
      "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress",
      "ec2:GetConsoleOutput", "ec2:GetConsoleScreenshot",
    ]
    resources = ["*"]
  }
  statement {
    # Scoped to what EC2 itself is handed. Without the condition this would let the dispatcher pass
    # any role in the account to any service.
    sid       = "MgnPassesTheInstanceRole"
    actions   = ["iam:PassRole"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }
  statement {
    sid       = "RemediationActions"
    actions   = ["mgn:RetryDataReplication"]
    resources = ["*"]
  }
  statement {
    sid       = "RemediationSsm"
    actions   = ["ssm:SendCommand", "ssm:GetCommandInvocation"]
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
      # The cutover step re-checks the wave's test verdict server-side, so it reads the log.
      DECISION_LOG_TABLE = aws_dynamodb_table.decision_log.name
    }
  }
}

# --- EventBridge Scheduler: re-invoke the Orchestrator every 5 min during long waits ---
# The schedule targets a Lambda, not the `aws-sdk:bedrockagentcore:invokeAgentRuntime` universal
# target. That universal target is accepted at apply time and then never delivers: the schedule
# fires, the role is right, and no invocation reaches the runtime. A native Lambda target does.

resource "aws_lambda_function" "resume" {
  count = var.runtime_arn == "" ? 0 : 1

  function_name = "${var.name_prefix}-resume"
  role          = aws_iam_role.resume[0].arn
  handler       = "dispatcher.resume.handler"
  runtime       = var.lambda_runtime
  architectures = ["arm64"]
  # The runtime drives up to RESUME_BATCH waves per call; this has to outlast that, not just the
  # network round trip.
  timeout   = 600
  s3_bucket = var.lambda_bucket
  s3_key    = var.lambda_key

  environment {
    variables = {
      AGENT_RUNTIME_ARN = var.runtime_arn
      # Set -> the tick goes to the harness and the agent decides what to do with it. Unset -> the
      # deterministic runtime walks the in-flight waves itself. Both paths ship; this picks one.
      HARNESS_ARN = var.harness_arn
    }
  }
}

data "aws_iam_policy_document" "resume_assume" {
  count = var.runtime_arn == "" ? 0 : 1
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "resume" {
  count              = var.runtime_arn == "" ? 0 : 1
  name               = "${var.name_prefix}-resume"
  assume_role_policy = data.aws_iam_policy_document.resume_assume[0].json
}

# This role can wake the Orchestrator and do nothing else. The step dispatcher, which can mutate
# MGN and Route 53, deliberately cannot invoke the runtime - that separation is the whole point of
# them being two roles over one artifact.
resource "aws_iam_role_policy" "resume" {
  count = var.runtime_arn == "" ? 0 : 1
  name  = "wake-the-agent"
  role  = aws_iam_role.resume[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeAgentRuntime"]
        Resource = [var.runtime_arn, "${var.runtime_arn}/*"]
      },
      # InvokeHarness is checked against two actions on the same ARN, not one - without
      # InvokeAgentRuntime alongside it the call is denied.
      {
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeHarness", "bedrock-agentcore:InvokeAgentRuntime"]
        Resource = var.harness_arn == "" ? ["arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:harness/*"] : [var.harness_arn, "${var.harness_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-resume:*"
      },
    ]
  })
}

resource "aws_scheduler_schedule" "resume_waves" {
  count = var.runtime_arn == "" ? 0 : 1

  name                = "${var.name_prefix}-resume-waves"
  schedule_expression = "rate(5 minutes)"
  # Safe to leave on: with nothing in flight the invocation is a no-op.
  state = var.scheduler_state

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.resume[0].arn
    role_arn = aws_iam_role.scheduler[0].arn
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
  name  = "invoke-resume-lambda"
  role  = aws_iam_role.scheduler[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.resume[0].arn
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

  dynamic "notification" {
    for_each = var.budget_alert_email == "" ? [] : [1]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = 50
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }
}

# --- AgentCore Harness: AWS runs the agent loop, the tools arrive over MCP ---
# The harness never holds a credential for the tool surface. It signs to the gateway with this role
# and the gateway signs onward to the MCP runtime with its own, so the whole chain is IAM and
# nothing expires. That is the reason for a gateway here rather than a URL and a bearer token.

data "aws_iam_policy_document" "harness_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "harness" {
  name               = "${var.name_prefix}-harness"
  assume_role_policy = data.aws_iam_policy_document.harness_assume.json
}

data "aws_iam_policy_document" "harness" {
  statement {
    sid       = "Model"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:${var.region}:${local.account_id}:*"]
  }
  # The harness pulls its own managed image from ECR Public.
  statement {
    sid       = "ManagedImagePull"
    actions   = ["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"]
    resources = ["*"]
  }
  statement {
    sid       = "Telemetry"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets", "logs:PutResourcePolicy", "logs:DescribeLogGroups"]
    resources = ["*"]
  }
  statement {
    sid       = "HarnessLogs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }
  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }
  statement {
    sid     = "WorkloadIdentity"
    actions = ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT"]
    resources = [
      "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default/workload-identity/harness_${var.harness_name}-*",
    ]
  }
  # Managed memory is what replaces the hand-rolled session table for harness conversations: the
  # scheduler reuses one session id per day, so the agent remembers what it already tried.
  statement {
    sid     = "Memory"
    actions = ["bedrock-agentcore:CreateEvent", "bedrock-agentcore:DeleteEvent", "bedrock-agentcore:GetEvent", "bedrock-agentcore:ListEvents", "bedrock-agentcore:RetrieveMemoryRecords"]
    # The docs give the managed-memory name as `harness_<abbrev>_*`; the service actually creates
    # `<harnessName>-<suffix>`. Both are listed because only the second one is real.
    resources = [
      "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:memory/${var.harness_name}-*",
      "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:memory/harness_*",
    ]
  }
  statement {
    sid       = "ToolGateway"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = ["arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:gateway/*"]
  }
}

resource "aws_iam_role_policy" "harness" {
  name   = "harness"
  role   = aws_iam_role.harness.id
  policy = data.aws_iam_policy_document.harness.json
}

# --- Gateway: the only thing allowed to call the MCP runtime ---
# Its role is deliberately tiny. Everything the tools are permitted to do is on the runtime role;
# this one only proves the caller is the gateway.

resource "aws_iam_role" "gateway" {
  count              = var.mcp_runtime_arn == "" ? 0 : 1
  name               = "${var.name_prefix}-gateway"
  assume_role_policy = data.aws_iam_policy_document.harness_assume.json
}

resource "aws_iam_role_policy" "gateway" {
  count = var.mcp_runtime_arn == "" ? 0 : 1
  name  = "invoke-mcp-runtime"
  role  = aws_iam_role.gateway[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeAgentRuntime"]
        Resource = [var.mcp_runtime_arn, "${var.mcp_runtime_arn}/*"]
      },
    ]
  })
}
