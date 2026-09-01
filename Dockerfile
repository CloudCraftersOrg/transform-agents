# AgentCore Runtime: a single container, specialists as in-process @tools.
# Regenerate with the starter toolkit (agentcore configure / agentcore launch) before hardening.
FROM --platform=linux/arm64 python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
COPY . .
RUN uv pip install --system --no-cache ".[agents]"

# Runtime config (from `terraform output`): WAVE_STATE_TABLE, DECISION_LOG_TABLE, STEP_LEDGER_TABLE,
# BEDROCK_GUARDRAIL_ID, ORCHESTRATOR_MODEL_ID, AGENTCORE_GATEWAY_URL.
EXPOSE 8080
CMD ["python", "-m", "agents.runtime"]
