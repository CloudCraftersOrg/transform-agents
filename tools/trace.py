from __future__ import annotations

import sys

# Every decision the agent records goes to DynamoDB, which is durable but invisible while a wave is
# running. These lines put the same narrative on stdout so the runtime log group carries it too.
# The prefix exists so a reader can allow-list the agent's own output: the MCP server and botocore
# log at INFO on every call, and filtering them out one pattern at a time never converges.

PREFIX = "AGENT"

# What a reader scans for first. Everything else is ordinary progress.
MARKS = {
    "escalation": "!!",
    "policy_denial": "!!",
    "rollback": "!!",
    "finops_variance": " !",
    "hitl": " ?",
    "remediation_outcome": " ~",
}

# Keys worth putting on the line itself. The full detail stays in the decision log.
HIGHLIGHTS = ("step", "gate", "turn", "error", "artifact", "resolved", "status", "job_id")


def _tail(detail: dict | None) -> str:
    if not detail:
        return ""
    parts = []
    for key in HIGHLIGHTS:
        if key not in detail:
            continue
        value = detail[key]
        if key == "job_id":
            value = str(value)[:8]
        text = str(value).replace("\n", " ")
        parts.append(f"{key}={text[:120]}")
    return ("  " + " ".join(parts)) if parts else ""


def trace(wave_id: str, kind: str, summary: str, detail: dict | None = None) -> None:
    """One line per decision, fixed columns so a wave reads as a narrative rather than a dump."""
    mark = MARKS.get(kind, "  ")
    line = (
        f"{PREFIX} {mark} {kind:<24} {(wave_id or '-'):<18} "
        f"{summary.replace(chr(10), ' ')[:400]}{_tail(detail)}"
    )
    print(line, flush=True)


def note(message: str) -> None:
    """Runtime-level milestones that are not decisions: what it resolved, what it reused."""
    print(f"{PREFIX}    {'runtime':<24} {'-':<18} {message}", flush=True)


def fail(message: str) -> None:
    """Failures reach stderr as well, so they survive a reader that only tails errors."""
    print(f"{PREFIX} XX {'failure':<24} {'-':<18} {message}", flush=True)
    print(message, file=sys.stderr, flush=True)
