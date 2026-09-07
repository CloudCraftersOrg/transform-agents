from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from state.models import WaveState
from tools.trace import PREFIX

# Operator console for the deployed agent. Read-only over the wave state and decision log, plus the
# one write that matters: invoking a wave. It runs on the operator's own credentials and talks to
# the same tables and runtime the agent uses - there is no second source of truth.

PORT = int(os.environ.get("CONSOLE_PORT", "8765"))
HOST = os.environ.get("CONSOLE_HOST", "127.0.0.1")

WAVE_TABLE = os.environ.get("WAVE_STATE_TABLE", "transform-agents-wave_state")
LOG_TABLE = os.environ.get("DECISION_LOG_TABLE", "transform-agents-decision_log")
HITL_TABLE = os.environ.get("HITL_TASK_TABLE", "transform-agents-hitl_tasks")
RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# The pipeline a wave walks, in order. Off-ramps are rendered separately - they are not steps.
PIPELINE = [
    "PENDING", "PRECHECK", "INTERPRETING", "REPLICATING",
    "TESTING", "CUTTING_OVER", "PARITY_CHECK", "FINOPS", "DONE",
]
OFF_RAMPS = ["ESCALATED", "ROLLING_BACK", "ROLLED_BACK", "FAILED"]

# The agent prefixes its own output (tools/trace.py). Everything else in this log group is the MCP
# server and botocore logging at INFO on every call - roughly nine lines in ten - which is why
# tailing it raw looks empty. Allow-listing converges; filtering their patterns out never did.
SIGNAL = re.compile(
    rf"^{PREFIX} "                       # the agent's own narrative
    r'|Traceback|Exception|^\s+File "'   # failures, with the frames that explain them
    r"|error response"                   # the MCP server's own refusals, which are findings
)


def _client(service: str):
    """A fresh session per call. The console outlives an SSO token, and the default session caches
    the resolved credentials, so reusing it would keep serving errors after a re-login."""
    import boto3

    return boto3.Session().client(service, region_name=REGION)


def waves() -> list[dict]:
    """Every wave the agent has run, newest first. A scan is right here: the table holds one row
    per wave of a PoC, and the console is the only thing that ever reads all of them."""
    out: list[dict] = []
    paginator = _client("dynamodb").get_paginator("scan")
    for page in paginator.paginate(TableName=WAVE_TABLE):
        for item in page["Items"]:
            state = WaveState.model_validate_json(item["doc"]["S"])
            out.append(state.model_dump(mode="json"))
    return sorted(out, key=lambda w: w.get("updated_at") or "", reverse=True)


def decisions(wave_id: str) -> list[dict]:
    """The decision log for one wave, oldest first - it reads as a narrative, not a tail."""
    out: list[dict] = []
    paginator = _client("dynamodb").get_paginator("query")
    pages = paginator.paginate(
        TableName=LOG_TABLE,
        KeyConditionExpression="wave_id = :w",
        ExpressionAttributeValues={":w": {"S": wave_id}},
    )
    for page in pages:
        out.extend(json.loads(i["doc"]["S"]) for i in page["Items"])
    return sorted(out, key=lambda e: e.get("ts") or "")


def invoke(payload: dict) -> dict:
    """Fire the deployed agent. Same call the scheduler makes, so the console cannot drive the
    wave down a path the unattended run could not also take."""
    if not RUNTIME_ARN:
        return {"error": "AGENT_RUNTIME_ARN is not set"}
    resp = _client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        payload=json.dumps(payload).encode(),
        contentType="application/json",
        accept="application/json",
    )
    body = resp["response"].read()
    try:
        return json.loads(body)
    except ValueError:
        return {"raw": body.decode(errors="replace")[:4000]}


def agent_logs(minutes: int = 30, limit: int = 300) -> list[dict]:
    """The runtime's log group carries far more MCP and botocore chatter than agent output, which
    is why tailing it looks empty. Everything the agent itself printed survives this filter."""
    group = os.environ.get(
        "AGENT_LOG_GROUP",
        "/aws/bedrock-agentcore/runtimes/transform_agents_runtime-fKJft75ihB-DEFAULT",
    )
    start = int((datetime.now(UTC).timestamp() - minutes * 60) * 1000)
    events: list[dict] = []
    try:
        paginator = _client("logs").get_paginator("filter_log_events")
        for page in paginator.paginate(logGroupName=group, startTime=start):
            for e in page["events"]:
                message = (e.get("message") or "").rstrip()
                if not message or not SIGNAL.search(message):
                    continue
                events.append({
                    "ts": datetime.fromtimestamp(e["timestamp"] / 1000, UTC).isoformat(),
                    "message": message[:2000],
                })
            if len(events) > limit * 4:
                break
    except Exception as e:  # noqa: BLE001 - a console panel never takes the console down
        return [{"ts": "", "message": f"could not read {group}: {e}"}]
    return events[-limit:]


# One query per escalated wave adds up: measured at ~13s, which is longer than the page's refresh
# interval, so uncached polling would pile requests on top of each other. Nothing here changes
# faster than the agent runs.
_ATTENTION_TTL = 20.0
_attention_cache: dict = {"at": 0.0, "value": None}


def _one_line_reason(raw) -> str:
    """A hypothesis is meant to be scannable. Some escalations carry a whole boto3 stack trace
    because the failing step's detail was passed straight through, so pull out the sentence that
    actually says what went wrong and drop the frames."""
    if not raw:
        return ""
    text = str(raw)
    if text.lstrip().startswith("{"):
        try:
            blob = json.loads(text)
            text = str(blob.get("errorMessage") or blob.get("detail") or blob.get("error") or text)
        except ValueError:
            pass
    text = text.split("stackTrace")[0].split('\n')[0]
    text = " ".join(text.replace('\\"', '"').split())
    return text[:220] + ("..." if len(text) > 220 else "")


def attention(force: bool = False) -> dict:
    """What is waiting on a person, right now. This is the notification channel: the agent writes
    when it is blocked and nobody is told, so the console has to be the thing that tells you.

    ESCALATED waves come from the one-row-per-wave table rather than a scan of every decision."""
    now = time.monotonic()
    fresh = now - _attention_cache["at"] < _ATTENTION_TTL
    if not force and _attention_cache["value"] is not None and fresh:
        return {**_attention_cache["value"], "cached": True}

    blocked = [w for w in waves() if w.get("status") == "ESCALATED"]
    items: list[dict] = []
    for w in blocked:
        last = next(
            (e for e in reversed(decisions(w["wave_id"])) if e.get("kind") == "escalation"), None
        )
        items.append({
            "kind": "escalation",
            "wave_id": w["wave_id"],
            "summary": (last or {}).get("summary", "escalated"),
            "hypothesis": _one_line_reason(((last or {}).get("detail") or {}).get("hypothesis")),
            "ts": (last or {}).get("ts", w.get("updated_at") or ""),
        })

    for task in _pending_hitl():
        items.append({
            "kind": "question",
            # what the panel offers a form for; anything else is answered in the agent's own words
            "gate": task.get("kind", ""),
            "wave_id": task.get("wave_id", ""),
            "summary": f"the agent is waiting on you: {task.get('kind', '')}",
            "hypothesis": _one_line_reason(task.get("payload", {}).get("question")
                                           or task.get("payload", {}).get("needed")),
            "ts": task.get("ts", ""),
        })

    items.sort(key=lambda i: i.get("ts") or "", reverse=True)
    value = {"count": len(items), "items": items}
    _attention_cache.update(at=time.monotonic(), value=value)
    return value


def _clock() -> str:
    return datetime.now(UTC).isoformat()


def reply_to_agent(wave_id: str, gate: str, text: str) -> dict:
    """Answer one of the agent's questions in your own words.

    A form was the wrong instrument. The agent works out *where* its applications answer by itself
    now; what it cannot work out is what a person means by healthy - "the leaderboard lists teams",
    "the catalog shows products" - and that is a sentence, not a set of fields. The reply is
    recorded against the wave and reaches the QA verdict as the description it judges the
    before/after diff against."""
    text = (text or "").strip()
    if not text:
        raise ValueError("write an answer")
    ts = _clock()
    entry = {
        "wave_id": wave_id, "ts": ts, "actor": "human", "kind": "hitl",
        "summary": f"the engineer answered {gate or 'the agent'}: {text[:160]}",
        "detail": {"gate": gate, "engineer_reply": text},
    }
    client = _client("dynamodb")
    client.put_item(TableName=LOG_TABLE, Item={
        "wave_id": {"S": wave_id}, "ts": {"S": ts}, "doc": {"S": json.dumps(entry)}})
    closed = _close_hitl(client, wave_id, gate) if gate else 0
    return {"answered": True, "wave_id": wave_id, "gate": gate, "questions_closed": closed}


def _close_hitl(client, wave_id: str, kind: str) -> int:
    """Mark the agent's open questions of this kind answered, so the panel stops asking."""
    closed = 0
    kwargs: dict = {"TableName": HITL_TABLE}
    while True:
        resp = client.scan(**kwargs)
        for i in resp.get("Items", []):
            if (i.get("status", {}).get("S") != "PENDING"
                    or i.get("wave_id", {}).get("S") != wave_id
                    or i.get("kind", {}).get("S") != kind):
                continue
            client.put_item(TableName=HITL_TABLE,
                            Item={**i, "status": {"S": "APPROVED"}})
            closed += 1
        if "LastEvaluatedKey" not in resp:
            return closed
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _pending_hitl() -> list[dict]:
    """Tasks the agent opened and nobody has answered. Durable since the queue moved to DynamoDB -
    before that they died with the invocation and could not be listed at all."""
    out: list[dict] = []
    kwargs: dict = {"TableName": HITL_TABLE}
    try:
        client = _client("dynamodb")
        while True:
            resp = client.scan(**kwargs)
            for i in resp.get("Items", []):
                if i.get("status", {}).get("S") != "PENDING":
                    continue
                try:
                    payload = json.loads(i.get("payload", {}).get("S", "{}"))
                except ValueError:
                    payload = {}
                out.append({
                    "wave_id": i.get("wave_id", {}).get("S", ""),
                    "kind": i.get("kind", {}).get("S", ""),
                    "ts": i.get("ts", {}).get("S", ""),
                    "payload": payload,
                })
            if "LastEvaluatedKey" not in resp:
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception:  # noqa: BLE001 - the panel degrades, the console stays up
        return out


def wave_view(wave_id: str) -> dict:
    """One wave, shaped for the page: where it is in the pipeline and how it got there."""
    log = decisions(wave_id)
    state = next((w for w in waves() if w["wave_id"] == wave_id), None)
    status = (state or {}).get("status", "PENDING")
    return {
        "wave": state or {"wave_id": wave_id, "status": status, "completed_steps": []},
        "decisions": log,
        "pipeline": PIPELINE,
        "off_ramps": OFF_RAMPS,
        "position": PIPELINE.index(status) if status in PIPELINE else -1,
        "counts": _counts(log),
    }


def _counts(log: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in log:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    return counts


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, content_type="application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            # the browser navigated away or the fetch was cancelled - not a server error
            pass

    def do_GET(self) -> None:
        route = urlparse(self.path)
        query = parse_qs(route.query)
        try:
            if route.path in ("/", "/index.html"):
                page = (Path(__file__).parent / "console.html").read_bytes()
                self._send(200, page, "text/html; charset=utf-8")
            elif route.path == "/api/waves":
                self._send(200, {"waves": waves(), "runtime": bool(RUNTIME_ARN)})
            elif route.path == "/api/wave":
                self._send(200, wave_view(query["id"][0]))
            elif route.path == "/api/transform":
                self._send(200, invoke({"read_chat": True}))
            elif route.path == "/api/attention":
                self._send(200, attention(force="force" in query))
            elif route.path == "/api/logs":
                self._send(200, {"events": agent_logs(int(query.get("minutes", ["30"])[0]))})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001 - report the failure in the panel, keep serving
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        # /api/ask reads the record back; /api/say writes into the job's chat. Two routes rather
        # than one, so the console never blurs asking the agent with speaking as it.
        if route not in ("/api/invoke", "/api/ask", "/api/say", "/api/reply"):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if route == "/api/reply":
                self._send(200, reply_to_agent(body["wave_id"], body.get("gate", ""),
                                               body.get("text", "")))
                return
            if route == "/api/ask":
                payload = {"ask": body["text"], "wave_id": body.get("wave_id", "")}
            elif route == "/api/say":
                payload = {"say": body["text"]}
            else:
                payload = body
            self._send(200, invoke(payload))
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, *_args) -> None:
        pass


class _Console(ThreadingHTTPServer):
    """Refuses to share the port.

    `HTTPServer` sets SO_REUSEADDR, and on Windows that lets a second console bind a port another
    one is already serving. The two then split the requests, and because `console.html` is read
    from disk per request while the Python is whatever each process imported at startup, the older
    one serves a new page against old handlers - a panel that renders a button the API has no field
    for. That cost an evening. Failing to start is the better outcome."""

    allow_reuse_address = False


def serve(port: int = PORT, host: str = HOST) -> ThreadingHTTPServer:
    try:
        return _Console((host, port), _Handler)
    except OSError as e:
        raise SystemExit(
            f"port {port} is already serving a console ({e}). Stop that one first - two consoles "
            f"on one port silently split the requests between them."
        ) from e


def main() -> None:
    print(f"console on http://{HOST}:{PORT} (runtime {'set' if RUNTIME_ARN else 'NOT set'})")
    serve().serve_forever()


if __name__ == "__main__":
    main()
