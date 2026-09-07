from __future__ import annotations

import json
import os

# Conversation continuity. Without it every invocation builds a cold agent: you cannot ask "and the
# other server?" because there is no other server in its memory. AgentCore hands us a session_id per
# request; this keeps that session's messages so the next call continues instead of restarting.

# Bounded on purpose. A migration conversation can run for days, and an unbounded transcript grows
# past the model's context and the item size limit. Older turns are dropped, not summarised - the
# durable record of what happened is the decision_log, not the chat.
MAX_TURNS = int(os.environ.get("SESSION_MAX_TURNS", "40"))
MAX_ITEM_BYTES = 350_000  # DynamoDB's limit is 400 KB; leave room for the rest of the item


class DynamoDbSessions:
    """One row per session: PK `session_id`, the message list as JSON."""

    def __init__(self, table: str, client=None) -> None:
        self._table = table
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb")
        return self._client

    def load(self, session_id: str) -> list[dict]:
        if not session_id:
            return []
        item = self.client.get_item(
            TableName=self._table, Key={"session_id": {"S": session_id}}
        ).get("Item")
        if not item:
            return []
        try:
            return json.loads(item["messages"]["S"])
        except (KeyError, ValueError):
            return []

    def save(self, session_id: str, messages: list[dict], updated_at: str = "") -> int:
        """Keeps the most recent turns that fit. Returns how many were stored."""
        if not session_id:
            return 0
        kept = list(messages)[-MAX_TURNS:]
        blob = json.dumps(kept, default=str)
        while len(blob.encode()) > MAX_ITEM_BYTES and len(kept) > 2:
            kept = kept[2:]  # drop the oldest exchange, not half a turn
            blob = json.dumps(kept, default=str)
        self.client.put_item(
            TableName=self._table,
            Item={
                "session_id": {"S": session_id},
                "messages": {"S": blob},
                "turns": {"N": str(len(kept))},
                "updated_at": {"S": updated_at},
            },
        )
        return len(kept)
