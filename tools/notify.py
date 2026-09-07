from __future__ import annotations

import json
import os

from state.models import DecisionLogEntry

# An escalation that only writes a row is an escalation nobody hears. The `Notifier` hook has been
# in tools/escalation.py from the start with nothing ever passed to it, so every "the agent
# escalated" in this system meant "someone has to go and look". This closes that.


def _subject(entry: DecisionLogEntry) -> str:
    wave = entry.wave_id or "-"
    return f"[transform-agents] {wave} escalated"[:99]  # SNS caps the subject at 100


def _body(entry: DecisionLogEntry) -> str:
    detail = entry.detail or {}
    return "\n".join([
        f"Wave:       {entry.wave_id}",
        f"When:       {entry.ts}",
        f"Summary:    {entry.summary}",
        "",
        f"Hypothesis: {detail.get('hypothesis', '-')}",
        f"Attempts:   {detail.get('attempts', '-')}",
        "",
        "Context:",
        str(detail.get("context", ""))[:2000],
        "",
        "The wave is not lost: it resumes on the next invocation once the blocker clears.",
    ])


def sns_notifier(topic_arn: str = "", client=None):
    """Publish escalations to SNS. Returns None when no topic is configured, which keeps the
    orchestrator's `notify` optional rather than making the topic a hard dependency."""
    arn = topic_arn or os.environ.get("ESCALATION_TOPIC_ARN", "")
    if not arn:
        return None

    def notify(entry: DecisionLogEntry) -> None:
        import boto3

        sns = client if client is not None else boto3.client("sns")
        try:
            sns.publish(TopicArn=arn, Subject=_subject(entry), Message=_body(entry))
        except Exception as e:  # noqa: BLE001 - a failed notification must not fail the wave
            print(f"AGENT XX notify                  -  could not publish escalation: "
                  f"{type(e).__name__}: {e}", flush=True)

    return notify


def json_notifier(sink: list):
    """For tests and local runs: collect what would have been sent."""
    def notify(entry: DecisionLogEntry) -> None:
        sink.append(json.loads(entry.model_dump_json()))

    return notify
