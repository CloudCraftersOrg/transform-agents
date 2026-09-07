from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

# Client for the AWS Transform MCP server. Two transports:
#  - stdio (plan B): run the server as a subprocess with the execution role's credentials.
#  - AgentCore Gateway (preferred): the Gateway fronts the MCP; validate the IAM-only headless flow.
# TransformWorkspace on top is transport-agnostic: it takes any `call(name, **kwargs) -> dict`,
# so tests inject a fake and never touch the network.

TASK_OPEN = ("IN_PROGRESS", "OPEN", "PENDING")


def stdio_transform_client(command: str | None = None):
    """`uvx awslabs.aws-transform-mcp-server` over stdio. Wrap in `with client:` to use.
    TRANSFORM_MCP_COMMAND overrides the launcher (a venv's uvx is not on PATH on Windows)."""
    from mcp import StdioServerParameters, stdio_client
    from strands.tools.mcp import MCPClient

    if command or os.environ.get("TRANSFORM_MCP_COMMAND"):
        cmd = command or os.environ["TRANSFORM_MCP_COMMAND"]
        args = ["awslabs.aws-transform-mcp-server@latest"]
    elif importlib.util.find_spec("awslabs.aws_transform_mcp_server") is not None:
        # baked into the image: no uvx, no PyPI reach at runtime
        cmd, args = sys.executable, ["-m", "awslabs.aws_transform_mcp_server.server"]
    else:
        cmd, args = shutil.which("uvx") or "uvx", ["awslabs.aws-transform-mcp-server@latest"]
    env = {**os.environ, **stable_session_env()}
    return MCPClient(
        lambda: stdio_client(StdioServerParameters(command=cmd, args=args, env=env))
    )


def stable_session_env() -> dict:
    """Transform keys workspace collaborators on <role-id>:<session-name>. AgentCore gives every
    invocation a fresh session name, so re-assume TRANSFORM_ASSUME_ROLE_ARN under a fixed name and
    hand those credentials to the MCP subprocess. Unset -> use the ambient credentials."""
    role = os.environ.get("TRANSFORM_ASSUME_ROLE_ARN")
    if not role:
        return {}
    import boto3

    c = boto3.client("sts").assume_role(
        RoleArn=role, RoleSessionName=os.environ.get("TRANSFORM_SESSION_NAME", "transform-agent")
    )["Credentials"]
    return {
        "AWS_ACCESS_KEY_ID": c["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": c["SecretAccessKey"],
        "AWS_SESSION_TOKEN": c["SessionToken"],
    }


def gateway_transform_client(gateway_url: str | None = None, access_token: str | None = None):
    """AgentCore Gateway-fronted MCP over streamable HTTP. `gateway_url` / `access_token` default
    to env (AGENTCORE_GATEWAY_URL / AGENTCORE_GATEWAY_TOKEN)."""
    from mcp.client.streamable_http import streamablehttp_client
    from strands.tools.mcp import MCPClient

    url = gateway_url or os.environ["AGENTCORE_GATEWAY_URL"]
    token = access_token or os.environ.get("AGENTCORE_GATEWAY_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return MCPClient(lambda: streamablehttp_client(url, headers=headers))


def make_transform_client():
    """Gateway if AGENTCORE_GATEWAY_URL is set, otherwise stdio."""
    return gateway_transform_client() if os.environ.get("AGENTCORE_GATEWAY_URL") else stdio_transform_client()


def transform_tools(client) -> dict:
    """Name -> tool for every tool the Transform MCP exposes. Call inside `with client:`."""
    return {t.tool_name: t for t in client.list_tools_sync()}


def unwrap(result):
    """MCP results nest the payload as JSON inside {"content":[{"text": ...}]}, sometimes twice."""
    node = result
    for _ in range(6):
        if isinstance(node, str):
            try:
                node = json.loads(node)
            except ValueError:
                return node
        if isinstance(node, dict) and isinstance(node.get("content"), list):
            texts = [b.get("text") for b in node["content"] if isinstance(b, dict) and "text" in b]
            if not texts:
                return node
            node = texts[0]
            continue
        break
    return node


def mcp_call(client):
    """Adapt a strands MCPClient into the `call(name, **kwargs) -> dict` TransformWorkspace wants."""

    def call(name: str, **kwargs):
        return unwrap(client.call_tool_sync(str(uuid.uuid4()), name, kwargs))

    return call


class TransformError(RuntimeError):
    pass


class TransformBusy(TransformError):
    """The assistant is still working on the previous turn. A conversation is single-flight:
    the caller waits and comes back rather than queueing another message."""


class TransformWorkspace:
    """The workspace as the Orchestrator sees it: jobs, artifacts, HITL tasks and job control.
    Nothing about the migration is hard-coded here - every id comes from the service."""

    def __init__(self, call, workspace_id: str) -> None:
        self._call = call
        self.workspace_id = workspace_id
        self._instructed: set[str] = set()

    @staticmethod
    def _data(res) -> dict:
        if isinstance(res, dict) and res.get("success") is False:
            raise TransformError(json.dumps(res)[:400])
        return (res or {}).get("data", {}) if isinstance(res, dict) else {}

    def _job_call(self, name: str, job_id: str, **kwargs):
        """Every job-scoped call goes through load_instructions first: the server refuses
        otherwise (INSTRUCTIONS_REQUIRED), and the instructions themselves are model input."""
        if job_id not in self._instructed:
            self.load_instructions(job_id)
        return self._call(name, workspaceId=self.workspace_id, jobId=job_id, **kwargs)

    # --- discovery ---

    @classmethod
    def list_workspaces(cls, call) -> list[dict]:
        return (cls._data(call("list_resources", resource="workspaces")) or {}).get("items", [])

    def load_instructions(self, job_id: str) -> dict:
        res = self._call("load_instructions", workspaceId=self.workspace_id, jobId=job_id)
        self._instructed.add(job_id)
        return self._data(res)

    def jobs(self) -> list[dict]:
        d = self._data(self._call("list_resources", resource="jobs", workspaceId=self.workspace_id))
        return d.get("items", [])

    def job_by_name(self, needle: str) -> dict | None:
        return next((j for j in self.jobs() if needle.lower() in (j.get("jobName") or "").lower()), None)

    def job_status(self, job_id: str, *, detailed: bool = False) -> dict:
        return self._data(self._job_call("get_job_status", job_id, detailed=detailed))

    # --- artifacts ---

    def artifacts(self, job_id: str, prefix: str | None = None) -> tuple[list[dict], list[str]]:
        kwargs = {"resource": "artifacts"}
        if prefix:
            kwargs["pathPrefix"] = prefix
        d = self._data(self._job_call("list_resources", job_id, **kwargs))
        return d.get("artifacts", []) or [], d.get("folders", []) or []

    def walk_artifacts(self, job_id: str) -> list[dict]:
        """Every artifact under the job, depth-first over the folder listing."""
        out, seen, queue = [], set(), [None]
        while queue:
            prefix = queue.pop(0)
            arts, folders = self.artifacts(job_id, prefix)
            out.extend(arts)
            for f in folders:
                if f not in seen:
                    seen.add(f)
                    queue.append(f)
        return out

    def fetch(self, job_id: str, artifact_id: str, dest_dir: str | Path, file_name: str = "") -> Path:
        """The server refuses to write outside its working directory, so dest must be under CWD."""
        dest = Path(dest_dir).resolve()
        cwd = Path.cwd().resolve()
        if not dest.is_relative_to(cwd):
            raise TransformError(f"savePath must be under {cwd}, got {dest}")
        dest.mkdir(parents=True, exist_ok=True)
        self._job_call(
            "get_resource", job_id, resource="artifact", artifactId=artifact_id,
            savePath=str(dest), **({"fileName": file_name} if file_name else {}),
        )
        return dest

    def fetch_all(self, job_id: str, dest_dir: str | Path, *, names: tuple[str, ...] = ()) -> Path:
        """Download the job's artifacts into dest. `names` filters on fileMetadata.path prefixes;
        empty means everything. Later generations sort last, so the newest wins on name collision."""
        dest = Path(dest_dir)
        arts = sorted(
            self.walk_artifacts(job_id),
            key=lambda a: str(a.get("artifactCreatedTimestamp") or ""),
        )
        for a in arts:
            # path carries the folder ("migration_planning/applications-....csv"); match the file,
            # and pass it as fileName or the server saves under the opaque artifact id.
            path = ((a.get("fileMetadata") or {}).get("path") or "").strip()
            base = path.rsplit("/", 1)[-1]
            if not base:
                continue
            if names and not any(base.lower().startswith(n.lower()) for n in names):
                continue
            self.fetch(job_id, a["artifactId"], dest, file_name=base)
        return dest

    # --- HITL: Transform owns the queue, the Orchestrator only reads and answers it ---

    def tasks(self, job_id: str, *, open_only: bool = False) -> list[dict]:
        d = self._data(self._job_call("list_resources", job_id, resource="tasks"))
        items = d.get("items", [])
        return [t for t in items if t.get("status") in TASK_OPEN] if open_only else items

    def blocking_tasks(self, job_id: str) -> list[dict]:
        """Open CRITICAL tasks: the job cannot advance until a human answers these."""
        return [t for t in self.tasks(job_id, open_only=True) if t.get("severity") == "CRITICAL"]

    def complete_task(self, job_id: str, task_id: str, *, content: str = "", action: str = "") -> dict:
        kwargs = {"taskId": task_id}
        if content:
            kwargs["content"] = content
        if action:
            kwargs["action"] = action
        return self._data(self._job_call("complete_task", job_id, **kwargs))

    # --- chat: AWS Transform advances a job when its pending interaction is answered ---

    def messages(self, job_id: str, limit: int = 10) -> list[dict]:
        d = self._data(self._job_call("list_resources", job_id, resource="messages", maxResults=limit))
        return d.get("messages", []) or []

    def latest_reply(self, job_id: str) -> str:
        """The newest thing the assistant actually said. Prerequisites and refusals arrive as prose
        with no interaction attached, so `pending_interaction` cannot see them."""
        said = [
            m for m in self.messages(job_id)
            if m.get("messageOrigin") == "SYSTEM"
            and (m.get("processingInfo") or {}).get("messageType") == "FINAL_RESPONSE"
        ]
        said.sort(key=lambda m: str(m.get("createdAt") or ""), reverse=True)
        return said[0].get("text", "") if said else ""

    def pending_interaction(self, job_id: str) -> dict | None:
        """The newest assistant message waiting on an answer. A job can sit in EXECUTING
        indefinitely with nothing blocked: the step only advances once this is answered.
        Scans every message rather than stopping at the first - THINKING placeholders interleave
        with the real response and the service does not promise an order."""
        candidates = [
            m for m in self.messages(job_id)
            if m.get("messageOrigin") == "SYSTEM" and (m.get("interactions") or [])
        ]
        candidates.sort(key=lambda m: str(m.get("createdAt") or ""), reverse=True)
        for m in candidates:
            for i in m.get("interactions") or []:
                data = i.get("data") or {}
                opts = (data.get("selectInteractionData") or {}).get("options") or []
                if opts:
                    return {"messageId": m.get("messageId"), "text": m.get("text", ""),
                            "actionType": i.get("actionType"), "options": opts}
        return None

    def send_message(self, job_id: str, text: str, *, skip_polling: bool = True) -> dict:
        try:
            return self._data(
                self._job_call("send_message", job_id, text=text, skipPolling=skip_polling)
            )
        except TransformError as e:
            if "already being processed" in str(e):
                raise TransformBusy(str(e)) from e
            raise

    def upload_artifact(
        self, job_id: str, content: str, file_name: str, *, file_type: str = "MARKDOWN",
        category: str = "CUSTOMER_INPUT",
    ) -> dict:
        """Artifacts are shared across collaborators, unlike chat threads. This is how the agent's
        reasoning reaches the humans looking at the job in the console."""
        try:
            return self._data(self._job_call(
                "upload_artifact", job_id, content=content, fileName=file_name,
                fileType=file_type, categoryType=category,
            ))
        except TransformError as e:
            if "already exists" in str(e):  # same record, already published - not a failure
                return {"alreadyPublished": file_name}
            raise

    # --- control ---

    def control_job(self, job_id: str, action: str) -> dict:
        return self._data(self._job_call("control_job", job_id, action=action))

    def poll(self, seconds: int = 30, follow_up: str = "") -> dict:
        return self._data(self._call("adaptive_poll", seconds=seconds, follow_up=follow_up))
