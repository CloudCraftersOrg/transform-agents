from __future__ import annotations

from typing import Any


def api_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        "added": {k: after[k] for k in after.keys() - before.keys()},
        "removed": {k: before[k] for k in before.keys() - after.keys()},
        "changed": {
            k: [before[k], after[k]] for k in before.keys() & after.keys() if before[k] != after[k]
        },
    }


def schema_diff(before: dict[str, str], after: dict[str, str]) -> dict[str, Any]:
    return api_diff(before, after)


def playwright_run(suite: str) -> dict:
    raise NotImplementedError("Sprint 2: run Playwright against the target environment")
