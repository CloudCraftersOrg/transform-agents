from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Runbook:
    failure: str
    action: str


class InMemoryRunbookKB:
    """Runbook KB. Back this with S3 Vectors in Sprint 2/3 (never OpenSearch Serverless
    *Quick create*: a 2-OCU floor running 24/7). Matching here is word overlap; real semantic
    matching arrives with the vector store."""

    def __init__(self) -> None:
        self._entries: list[Runbook] = []

    def query(self, failure: str, limit: int = 3) -> list[Runbook]:
        tokens = set(failure.lower().split())
        scored = [
            (len(tokens & set(rb.failure.lower().split())), rb) for rb in self._entries
        ]
        scored = [(score, rb) for score, rb in scored if score > 0]
        scored.sort(key=lambda x: -x[0])
        return [rb for _, rb in scored[:limit]]

    def write(self, runbook: Runbook) -> None:
        self._entries.append(runbook)

    def __len__(self) -> int:
        return len(self._entries)
