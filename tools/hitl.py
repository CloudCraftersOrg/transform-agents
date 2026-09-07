from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from state.models import DecisionLogEntry
from state.store import StateStore

APPROVE_DERIVED_CONTRACT = "approve_derived_contract"


@dataclass
class HitlTask:
    wave_id: str
    kind: str
    payload: dict
    status: str = "PENDING"  # PENDING | APPROVED | REJECTED
    created_at: str = ""  # set when the task is persisted, so resolve() updates the same row


# Asking is idempotent. The scheduler re-runs a wave every five minutes and the wave re-asks the
# same question each time, so without this one unanswered question becomes one row per tick - the
# probe-spec question reached sixty-odd identical rows in a night and buried the panel it was meant
# to surface. Same question, still open -> the open one is returned. A genuinely different question,
# or one already answered, still opens a new task.
def _already_open(tasks, wave_id: str, kind: str, payload: dict):
    return next((t for t in tasks
                 if t.status == "PENDING" and t.wave_id == wave_id
                 and t.kind == kind and t.payload == payload), None)


class InMemoryHitlQueue:
    """Local stand-in for the Transform MCP's `submit_hitl_task`, for decoupled runs.
    The only programmed human gate is approving the derived contract."""

    def __init__(self, store: StateStore, clock: Callable[[], str]) -> None:
        self.store = store
        self._clock = clock
        self._tasks: list[HitlTask] = []

    def submit(self, wave_id: str, kind: str, payload: dict) -> HitlTask:
        open_already = _already_open(self._tasks, wave_id, kind, payload)
        if open_already is not None:
            return open_already
        task = HitlTask(wave_id=wave_id, kind=kind, payload=payload)
        self._tasks.append(task)
        self.store.append_decision(
            DecisionLogEntry(
                wave_id=wave_id,
                ts=self._clock(),
                actor="system",
                kind="hitl",
                summary=f"pending human task: {kind}",
                detail=payload,
            )
        )
        return task

    def pending(self) -> list[HitlTask]:
        return [t for t in self._tasks if t.status == "PENDING"]

    def latest(self, wave_id: str, kind: str) -> HitlTask | None:
        matches = [t for t in self._tasks if t.wave_id == wave_id and t.kind == kind]
        return matches[-1] if matches else None

    def resolve(self, task: HitlTask, approved: bool) -> HitlTask:
        task.status = "APPROVED" if approved else "REJECTED"
        self.store.append_decision(
            DecisionLogEntry(
                wave_id=task.wave_id,
                ts=self._clock(),
                actor="human",
                kind="hitl",
                summary=f"task {task.kind}: {task.status}",
                detail=task.payload,
            )
        )
        return task


class DynamoDbHitlQueue:
    """The same queue, durable. In-memory tasks die with the invocation, so nothing could answer
    "what is the agent waiting on right now?" without replaying the decision log. One row per task,
    PK `task_key` = `<wave_id>#<kind>#<ts>`, with a `status` attribute the console can filter."""

    def __init__(self, table: str, store: StateStore, clock: Callable[[], str], client=None) -> None:
        self._table = table
        self.store = store
        self._clock = clock
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb")
        return self._client

    def _key(self, task: HitlTask, ts: str) -> str:
        return f"{task.wave_id}#{task.kind}#{ts}"

    def _put(self, task: HitlTask, ts: str) -> None:
        import json

        self.client.put_item(
            TableName=self._table,
            Item={
                "task_key": {"S": self._key(task, ts)},
                "wave_id": {"S": task.wave_id},
                "kind": {"S": task.kind},
                "status": {"S": task.status},
                "ts": {"S": ts},
                "payload": {"S": json.dumps(task.payload, default=str)},
            },
        )

    def submit(self, wave_id: str, kind: str, payload: dict) -> HitlTask:
        open_already = _already_open(self._scan(), wave_id, kind, payload)
        if open_already is not None:
            return open_already
        ts = self._clock()
        task = HitlTask(wave_id=wave_id, kind=kind, payload=payload)
        task.created_at = ts
        self._put(task, ts)
        self.store.append_decision(
            DecisionLogEntry(
                wave_id=wave_id, ts=ts, actor="system", kind="hitl",
                summary=f"pending human task: {kind}", detail=payload,
            )
        )
        return task

    def _scan(self) -> list[HitlTask]:
        import json

        out: list[HitlTask] = []
        kwargs: dict = {"TableName": self._table}
        while True:
            resp = self.client.scan(**kwargs)
            for i in resp.get("Items", []):
                t = HitlTask(
                    wave_id=i["wave_id"]["S"], kind=i["kind"]["S"],
                    payload=json.loads(i["payload"]["S"]), status=i["status"]["S"],
                )
                t.created_at = i.get("ts", {}).get("S", "")
                out.append(t)
            if "LastEvaluatedKey" not in resp:
                return sorted(out, key=lambda t: t.created_at or "")
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def pending(self) -> list[HitlTask]:
        return [t for t in self._scan() if t.status == "PENDING"]

    def latest(self, wave_id: str, kind: str) -> HitlTask | None:
        matches = [t for t in self._scan() if t.wave_id == wave_id and t.kind == kind]
        return matches[-1] if matches else None

    def resolve(self, task: HitlTask, approved: bool) -> HitlTask:
        task.status = "APPROVED" if approved else "REJECTED"
        ts = getattr(task, "created_at", "") or self._clock()
        self._put(task, ts)
        self.store.append_decision(
            DecisionLogEntry(
                wave_id=task.wave_id, ts=self._clock(), actor="human", kind="hitl",
                summary=f"task {task.kind}: {task.status}", detail=task.payload,
            )
        )
        return task
