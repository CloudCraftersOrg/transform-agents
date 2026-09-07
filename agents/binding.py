from __future__ import annotations

import threading
from contextlib import contextmanager

from tools.trace import fail, note


class Binding:
    """Everything a tool needs to touch the migration: the live AWS Transform workspace, the job it
    owns, and an Orchestrator built over the contract derived from that job's plan.

    A stdio MCP client is a child process, not a connection pool. Holding one for the life of a
    server is what makes an MCP surface viable at all - rebuilding it per tool call would respawn
    the Transform server and re-walk the plan on every question. Two rules follow:

      - one caller at a time. An AWS Transform conversation is single-flight and the stdio pipe is
        not reentrant, so `session()` serialises access rather than trusting callers to.
      - a dead child heals. If the process exits, the next call rebuilds instead of failing for
        the rest of the server's life.
    """

    def __init__(self, factory=None) -> None:
        self._factory = factory or _bind
        self._lock = threading.RLock()
        self._state: tuple | None = None

    @contextmanager
    def session(self):
        """Yield (orq, workspace, plan_job) with exclusive access. Rebuilds once on failure - a
        broken pipe from a dead child looks identical to a real error until you retry."""
        with self._lock:
            if self._state is None:
                self._state = self._factory()
            _client, orq, workspace, plan_job = self._state
            try:
                yield orq, workspace, plan_job
            except Exception as e:
                # Re-raised either way; the only decision here is whether the binding survived it.
                if _is_transport_failure(e):
                    fail(f"transform binding lost ({type(e).__name__}); it will be rebuilt")
                    self.close()
                raise

    def close(self) -> None:
        with self._lock:
            state, self._state = self._state, None
        if state is None:
            return
        try:
            state[0].__exit__(None, None, None)
        except Exception:  # noqa: BLE001, S110 - teardown must not mask the caller's result
            pass


TRANSPORT_SYMPTOMS = ("BrokenPipeError", "ClosedResourceError", "EndOfStream",
                      "ProcessLookupError", "McpError", "ConnectionError")


def _is_transport_failure(e: Exception) -> bool:
    """A tool refusing to act is a result; the pipe to Transform dying is not. Only the second one
    justifies throwing the binding away."""
    return type(e).__name__ in TRANSPORT_SYMPTOMS or "closed" in str(e).lower()


def _bind():
    """Resolve the live Transform workspace and build an Orchestrator over it. The contract is
    derived from the plan the first time and reused from the decision log afterwards."""
    from agents.runtime import _build, _from_workspace, _workspaces
    from tools.transform_mcp import make_transform_client, mcp_call

    client = make_transform_client()
    call = mcp_call(client.__enter__())
    workspace, plan_job, _ = _workspaces(call)
    if workspace is None:
        client.__exit__(None, None, None)
        raise RuntimeError("no AWS Transform workspace exposes the migration job")
    contract, _inputs, _ws = _from_workspace({"wave_id": "-"}, call, None)
    note(f"bound to {plan_job.get('jobName')} ({contract.case_id}, "
         f"{len(contract.constraints)} constraints)")
    return client, _build(contract, workspace=workspace), workspace, plan_job
