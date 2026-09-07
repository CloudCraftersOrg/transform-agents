from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Protocol

from tools.policy import Action, Decision, evaluate_policy
from tools.spec import DecisionContract

# Dispatcher registry. deploy_lza stays registered even when LZA is off: it just never dispatches.
DETERMINISTIC_STEPS = (
    "deploy_lza",
    "initialize_mgn",
    "resize_replication_server",
    "probe_apps",
    "mgn_status",
    "apply_remediation",
    "start_replication",
    "launch_test",
    "cutover",
    "rollback",
    "finalize",
)


@dataclass
class StepContext:
    wave_id: str
    step_id: str
    contract: DecisionContract
    params: dict = field(default_factory=dict)


class StepRejected(RuntimeError):
    pass


StepFn = Callable[[StepContext], dict]


class Ledger(Protocol):
    """Idempotency ledger for (wave_id, step_id) -> result. In-memory for local runs; the Lambda
    dispatcher swaps in a DynamoDB-backed one so replays survive across invocations. `put` returns
    False when the key already existed (a concurrent writer won the race)."""

    def get(self, key: tuple[str, str]) -> dict | None: ...
    def put(self, key: tuple[str, str], result: dict) -> bool: ...


class InMemoryLedger:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], dict] = {}

    def get(self, key: tuple[str, str]) -> dict | None:
        return self._entries.get(key)

    def put(self, key: tuple[str, str], result: dict) -> bool:
        if key in self._entries:
            return False
        self._entries[key] = result
        return True


class DynamoDbLedger:
    """Conditional-write ledger over a single table (PK `step_key`). A replay reads the stored
    result instead of re-executing the effect."""

    def __init__(self, table: str, client=None) -> None:
        self._table = table
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb")
        return self._client

    def get(self, key: tuple[str, str]) -> dict | None:
        resp = self.client.get_item(
            TableName=self._table, Key={"step_key": {"S": f"{key[0]}#{key[1]}"}}
        )
        item = resp.get("Item")
        return json.loads(item["result"]["S"]) if item else None

    def put(self, key: tuple[str, str], result: dict) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.put_item(
                TableName=self._table,
                Item={"step_key": {"S": f"{key[0]}#{key[1]}"}, "result": {"S": json.dumps(result)}},
                ConditionExpression="attribute_not_exists(step_key)",
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise


class Dispatcher:
    """Deterministic executor. Idempotent by (wave_id, step_id). Re-evaluates policy server-side
    without trusting that the Orchestrator already did."""

    def __init__(self, ledger: Ledger | None = None) -> None:
        self._registry: dict[str, StepFn] = {}
        self._ledger: Ledger = ledger or InMemoryLedger()

    def register(self, name: str, fn: StepFn) -> None:
        self._registry[name] = fn

    def dispatch(self, name: str, ctx: StepContext, guard: Action | None = None) -> dict:
        key = (ctx.wave_id, ctx.step_id)
        cached = self._ledger.get(key)
        if cached is not None:
            return cached
        if guard is not None:
            decision: Decision = evaluate_policy(guard, ctx.contract)
            if not decision.allowed:
                raise StepRejected("; ".join(decision.reasons))
        try:
            result = self._registry[name](ctx)
        except StepRejected:
            raise
        except Exception as e:  # noqa: BLE001 - a step failure is remediation's input, not a crash
            return {"error": f"{type(e).__name__}: {e}"}
        if not self._ledger.put(key, result):
            # a concurrent invocation already recorded this step; return the stored result
            return self._ledger.get(key) or result
        return result


class LambdaInvokingDispatcher:
    """Same `.dispatch(name, ctx, guard)` shape the Orchestrator expects, but the effect runs in
    the step-dispatcher Lambda: policy re-eval, idempotency and the AWS calls all happen there, in
    a separate process. `function` is a name or ARN - `lambda:Invoke` accepts either."""

    def __init__(self, function: str, client=None) -> None:
        self._function = function
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("lambda")
        return self._client

    def dispatch(self, name: str, ctx: StepContext, guard: Action | None = None) -> dict:
        event = {
            "wave_id": ctx.wave_id,
            "step_id": ctx.step_id,
            "step": name,
            "params": ctx.params,
            "contract": ctx.contract.model_dump(mode="json"),
            "guard": asdict(guard) if guard is not None else None,
        }
        resp = self.client.invoke(
            FunctionName=self._function, Payload=json.dumps(event).encode()
        )
        raw = resp["Payload"].read()
        if resp.get("FunctionError"):
            return {"error": raw.decode(errors="replace")}
        body = json.loads(raw)
        if body.get("rejected"):
            raise StepRejected(body["reason"])
        if not body.get("ok"):
            return {"error": body.get("reason") or str(body)}
        return body["result"]
