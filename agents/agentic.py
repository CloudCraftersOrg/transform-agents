from __future__ import annotations

import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

from agents.binding import Binding
from agents.prompt import SYSTEM_PROMPT
from agents.runtime import _clock
from agents.tools import build_tools
from tools.sessions import DynamoDbSessions
from tools.trace import fail, note

# The agentic entrypoint. `BedrockAgentCoreApp` owns the /invocations + /ping contract, so the
# hand-rolled HTTP server is gone; Strands owns the reasoning loop. The deterministic layer is not
# gone either - it is a tool: `run_migration_wave` still carries the state machine and the resume
# point the scheduler depends on. The agent decides *when* to run a wave, not how one is sequenced.

app = BedrockAgentCoreApp()

MODEL_ID = os.environ.get("ORCHESTRATOR_MODEL_ID", "amazon.nova-lite-v1:0")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def _sessions() -> DynamoDbSessions | None:
    table = os.environ.get("SESSION_TABLE")
    return DynamoDbSessions(table) if table else None


def _agent(orq, workspace, plan_job, messages: list | None = None) -> Agent:
    return Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
        system_prompt=SYSTEM_PROMPT,
        tools=build_tools(orq, workspace=workspace, plan_job=plan_job),
        messages=messages or [],
    )


# Payloads the deterministic entrypoint already owns: the scheduler's resume, a direct wave run,
# and the console's read/ask/say. Kept working so this deployment is additive - the agentic surface
# has to earn its place before anything that works today is taken away.
BINDING = Binding()

DETERMINISTIC_KEYS = ("action", "wave_id", "contract", "ask", "say", "read_chat")


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    """One conversational surface. `prompt` is what you want done, in words; the agent chooses the
    tools. Reusing a session id continues the conversation instead of starting cold. Every payload
    the deterministic entrypoint understands still routes there unchanged."""
    if any(k in payload for k in DETERMINISTIC_KEYS):
        from agents.runtime import invoke as deterministic

        return deterministic(payload)

    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "send a prompt, or one of "
                         f"{list(DETERMINISTIC_KEYS)} for the deterministic paths"}

    session_id = getattr(context, "session_id", "") or payload.get("session_id", "")
    store = _sessions()
    try:
        with BINDING.session() as (orq, workspace, plan_job):
            history = store.load(session_id) if store else []
            note(f"agentic invoke (session {session_id[:12] or '-'}, "
                 f"{len(history)} prior turn(s)): {prompt[:140]}")
            agent = _agent(orq, workspace, plan_job, messages=history)
            result = agent(prompt)
            turns = store.save(session_id, agent.messages, _clock()) if store else 0
            return {"prompt": prompt, "answer": str(result),
                    "session_id": session_id, "turns_remembered": turns}
    except Exception as e:  # noqa: BLE001 - surface the failure, do not drop the connection
        fail(f"{type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"[:600]}


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
