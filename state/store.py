from __future__ import annotations

import uuid
from typing import Protocol

from state.models import DecisionLogEntry, WaveState
from state.transitions import assert_legal


class StateStore(Protocol):
    def get_wave(self, wave_id: str) -> WaveState | None: ...
    def put_wave(self, wave: WaveState) -> None: ...
    def append_decision(self, entry: DecisionLogEntry) -> None: ...
    def decisions(self, wave_id: str) -> list[DecisionLogEntry]: ...


class InMemoryStateStore:
    def __init__(self) -> None:
        self._waves: dict[str, WaveState] = {}
        self._log: list[DecisionLogEntry] = []

    def get_wave(self, wave_id: str) -> WaveState | None:
        w = self._waves.get(wave_id)
        return w.model_copy(deep=True) if w else None

    def put_wave(self, wave: WaveState) -> None:
        prev = self._waves.get(wave.wave_id)
        if prev is not None and prev.status != wave.status:
            assert_legal(prev.status, wave.status)
        self._waves[wave.wave_id] = wave.model_copy(deep=True)

    def append_decision(self, entry: DecisionLogEntry) -> None:
        self._log.append(entry)

    def decisions(self, wave_id: str) -> list[DecisionLogEntry]:
        return [e for e in self._log if e.wave_id == wave_id]


class DynamoDbStateStore:
    """StateStore backed by boto3. Each row stores the model as a JSON blob (`doc`) plus a flat
    `status` attribute for future GSIs and optimistic locking.

    Tables:
    - wave_state:   PK `wave_id` (S)
    - decision_log: PK `wave_id` (S), SK `ts` (S) - the SK is `<ts>#<uuid8>` so same-timestamp
                    entries don't collide while still sorting chronologically.

    `put_wave` does a read-before-write to raise a friendly IllegalTransition; that check is not
    atomic - EventBridge re-invokes serially per wave, and a production version would fold the
    legality check into a ConditionExpression on `status`.
    """

    def __init__(self, wave_table: str, log_table: str, client=None) -> None:
        self._wave_table = wave_table
        self._log_table = log_table
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb")
        return self._client

    def get_wave(self, wave_id: str) -> WaveState | None:
        resp = self.client.get_item(
            TableName=self._wave_table, Key={"wave_id": {"S": wave_id}}
        )
        item = resp.get("Item")
        return WaveState.model_validate_json(item["doc"]["S"]) if item else None

    def put_wave(self, wave: WaveState) -> None:
        prev = self.get_wave(wave.wave_id)
        if prev is not None and prev.status != wave.status:
            assert_legal(prev.status, wave.status)
        self.client.put_item(
            TableName=self._wave_table,
            Item={
                "wave_id": {"S": wave.wave_id},
                "status": {"S": wave.status.value},
                "doc": {"S": wave.model_dump_json()},
            },
        )

    def append_decision(self, entry: DecisionLogEntry) -> None:
        sk = f"{entry.ts}#{uuid.uuid4().hex[:8]}"
        self.client.put_item(
            TableName=self._log_table,
            Item={
                "wave_id": {"S": entry.wave_id},
                "ts": {"S": sk},
                "doc": {"S": entry.model_dump_json()},
            },
        )

    def decisions(self, wave_id: str) -> list[DecisionLogEntry]:
        out: list[DecisionLogEntry] = []
        kwargs: dict = {
            "TableName": self._log_table,
            "KeyConditionExpression": "wave_id = :w",
            "ExpressionAttributeValues": {":w": {"S": wave_id}},
        }
        while True:
            resp = self.client.query(**kwargs)
            out.extend(DecisionLogEntry.model_validate_json(i["doc"]["S"]) for i in resp["Items"])
            if "LastEvaluatedKey" not in resp:
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
