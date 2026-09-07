# AgentCore Runtime: a single container, specialists as in-process @tools.
# Regenerate with the starter toolkit (agentcore configure / agentcore launch) before hardening.
FROM --platform=linux/arm64 python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
COPY . .
RUN uv pip install --system --no-cache ".[agents]" awslabs.aws-transform-mcp-server

# Runtime config (from `terraform output`): WAVE_STATE_TABLE, DECISION_LOG_TABLE, STEP_LEDGER_TABLE,
# BEDROCK_GUARDRAIL_ID, ORCHESTRATOR_MODEL_ID, TRANSFORM_PLAN_JOB, TRANSFORM_CONTAINER_JOB.
# The Transform MCP server is installed above, so the runtime never reaches PyPI.
#
# SERVE_PROTOCOL picks the role: HTTP (default) serves the agent loop on 8080; MCP serves the
# tool surface on 8000/mcp for an AgentCore Harness. Same image, same code, different port.
EXPOSE 8080 8000
CMD ["python", "-m", "agents.serve"]
