from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agents.model import StrandsModel
from agents.orchestrator import WaveInputs, build_orchestrator
from dispatcher.handler import build_lambda_dispatcher
from dispatcher.steps import DynamoDbLedger, LambdaInvokingDispatcher
from state.store import DynamoDbStateStore
from tools.hitl import InMemoryHitlQueue
from tools.spec import DecisionContract

# AgentCore Runtime contract: POST /invocations and GET /ping on 8080. Specialists are in-process
# @tools, not separate runtimes. Prefer `agentcore configure` / `agentcore launch` to generate the
# production wrapper; this is a working reference.

PORT = int(os.environ.get("PORT", "8080"))
# AgentCore Runtime routes POST /invocations to the container, so the app must listen on all
# interfaces inside it. Override with HOST for local runs.
HOST = os.environ.get("HOST", "0.0.0.0")  # nosec B104


def _clock() -> str:
    return datetime.now(UTC).isoformat()


def _build(contract: DecisionContract):
    store = DynamoDbStateStore(
        os.environ["WAVE_STATE_TABLE"], os.environ["DECISION_LOG_TABLE"]
    )
    model = StrandsModel(
        os.environ.get("ORCHESTRATOR_MODEL_ID", "us.amazon.nova-lite-v1:0"),
        region=os.environ.get("AWS_REGION", "us-east-1"),
        guardrail_id=os.environ.get("BEDROCK_GUARDRAIL_ID"),
    )
    # STEP_DISPATCHER_FUNCTION set -> invoke the separate step Lambda (name or ARN, both work).
    # Not set -> run the steps in-process (single-container mode), still policy-checked + idempotent.
    fn = os.environ.get("STEP_DISPATCHER_FUNCTION")
    if fn:
        dispatcher = LambdaInvokingDispatcher(fn)
    else:
        ledger_table = os.environ.get("STEP_LEDGER_TABLE")
        dispatcher = build_lambda_dispatcher(
            ledger=DynamoDbLedger(ledger_table) if ledger_table else None
        )
    hitl = InMemoryHitlQueue(store, _clock)
    return build_orchestrator(model, contract, store=store, dispatcher=dispatcher, hitl=hitl, clock=_clock)


def invoke(payload: dict) -> dict:
    """payload: {contract, wave_id, inputs?, require_contract_approval?}. Called for both the first
    run and every EventBridge re-invocation - `run_wave` is resumable, so they're the same call."""
    contract = DecisionContract.model_validate(payload["contract"])
    orq = _build(contract)
    inputs = WaveInputs(**payload["inputs"]) if payload.get("inputs") else None
    outcome = orq.run_wave(
        payload["wave_id"],
        inputs,
        require_contract_approval=payload.get("require_contract_approval", False),
    )
    return {"wave_id": payload["wave_id"], "outcome": str(outcome)}


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/ping":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/invocations":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            self._send(200, invoke(payload))
        except Exception as e:  # noqa: BLE001 - surface any failure as a 500 body, don't crash the server
            self._send(500, {"error": type(e).__name__, "detail": str(e)})

    def log_message(self, *_args) -> None:  # quiet the default stderr access log
        pass


def serve(port: int = PORT, host: str = HOST) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _Handler)


def main() -> None:
    serve().serve_forever()


if __name__ == "__main__":
    main()
