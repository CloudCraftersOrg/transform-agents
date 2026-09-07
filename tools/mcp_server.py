from __future__ import annotations

import inspect
import json
import os

from mcp.server.fastmcp import FastMCP

from agents.binding import Binding
from agents.tools import build_tools
from tools.trace import fail, note

# The tool surface as a Model Context Protocol server, so an AgentCore Harness can call it. The
# tools themselves are not redefined here: `agents.tools.build_tools` remains the single definition,
# and this module only re-publishes it over a different transport. Two surfaces that drift apart
# are worse than one surface, and every guard in those tools is a guard the Harness inherits.
#
# AgentCore Runtime expects an MCP server on 0.0.0.0:8000/mcp. Stateless, because each tool call is
# self-contained - the conversation lives in the Harness, not here.

PORT = int(os.environ.get("MCP_PORT", "8000"))

mcp = FastMCP(name="transform-agents", host="0.0.0.0", port=PORT, stateless_http=True)

binding = Binding()


def _publish(server: FastMCP, bind: Binding) -> list[str]:
    """Register one MCP tool per Strands tool, keeping the original name, docstring and signature.
    The Strands surface is built twice: once unbound, purely to read its shape, and again inside
    each call against the live workspace. Building it unbound is safe - the tools are closures and
    touch nothing until invoked."""
    published = []
    for spec in build_tools(None):
        published.append(spec.tool_name)
        server.add_tool(_proxy(spec.tool_name, spec._tool_func, bind), name=spec.tool_name)
    return published


def _proxy(name: str, fn, bind: Binding):
    """A tool that resolves the live binding at call time. Failures come back as JSON rather than
    as a transport error: an agent that is told why a tool refused can choose differently, and one
    that sees a stack trace can only retry."""

    def call(*args, **kwargs):
        try:
            with bind.session() as (orq, workspace, plan_job):
                live = {t.tool_name: t._tool_func
                        for t in build_tools(orq, workspace=workspace, plan_job=plan_job)}
                note(f"mcp {name}({json.dumps(kwargs, default=str)[:160]})")
                return live[name](*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - the agent needs the reason, not a dropped connection
            fail(f"mcp {name}: {type(e).__name__}: {e}")
            return json.dumps({"error": f"{type(e).__name__}: {e}"[:600], "tool": name})

    call.__name__ = name
    call.__doc__ = fn.__doc__
    call.__annotations__ = dict(getattr(fn, "__annotations__", {}))
    call.__signature__ = inspect.signature(fn)
    return call


TOOLS = _publish(mcp, binding)


def main() -> None:
    note(f"MCP server on 0.0.0.0:{PORT}/mcp exposing {TOOLS}")
    try:
        mcp.run(transport="streamable-http")
    finally:
        binding.close()


if __name__ == "__main__":
    main()
