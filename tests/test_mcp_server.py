"""The tool surface as MCP. What matters is that it is the same surface: an AgentCore Harness calls
these tools, so a guard that exists in Strands and not here would be a guard the harness bypasses."""
import asyncio
import json

import pytest

from agents.binding import Binding, _is_transport_failure
from agents.tools import build_tools
from tools.mcp_server import TOOLS, _proxy, mcp


def _listed():
    return asyncio.run(mcp.list_tools())


def test_the_mcp_surface_is_the_strands_surface():
    """Two definitions of the same tool is how the harness ends up with a weaker one."""
    assert TOOLS == [t.tool_name for t in build_tools(None)]
    assert {t.name for t in _listed()} == set(TOOLS)


def test_every_tool_keeps_its_own_description():
    strands = {t.tool_name: t.tool_spec["description"] for t in build_tools(None)}
    for tool in _listed():
        assert tool.description.strip() == strands[tool.name].strip()


def test_schemas_carry_required_arguments_and_defaults():
    by_name = {t.name: t.inputSchema for t in _listed()}
    assert by_name["migration_status"]["required"] == ["wave_id"]
    assert by_name["mgn_replication_health"].get("required", []) == []
    wave = by_name["run_migration_wave"]
    assert wave["required"] == ["wave_id"]
    assert wave["properties"]["approve_gates"]["default"] == ""
    assert "kind" not in by_name["ask_engineer"]["properties"]


def test_a_failing_tool_answers_with_a_reason_not_a_dropped_connection():
    """An agent told why a tool refused can choose differently; one handed a transport error can
    only retry."""

    def boom():
        raise RuntimeError("transform is unreachable")

    def _fn(wave_id: str) -> str:
        """doc"""

    proxy = _proxy("migration_status", _fn, Binding(factory=boom))
    out = json.loads(proxy(wave_id="w1"))
    assert out["tool"] == "migration_status"
    assert "transform is unreachable" in out["error"]


def test_the_binding_is_built_once_and_reused():
    calls = []

    def factory():
        calls.append(1)
        return (_Client(), "orq", "ws", {"jobId": "j"})

    bind = Binding(factory=factory)
    for _ in range(3):
        with bind.session() as (orq, _ws, _job):
            assert orq == "orq"
    assert len(calls) == 1


def test_a_tool_refusing_does_not_throw_the_binding_away():
    """A refusal is a result. Rebuilding on one would respawn Transform on every guard that fires."""
    bind = Binding(factory=lambda: (_Client(), "orq", "ws", {}))
    with pytest.raises(ValueError), bind.session():
        raise ValueError("that option is destructive")
    assert bind._state is not None


def test_a_dead_pipe_is_rebuilt_on_the_next_call():
    built = []

    def factory():
        built.append(1)
        return (_Client(), "orq", "ws", {})

    bind = Binding(factory=factory)
    with pytest.raises(BrokenPipeError), bind.session():
        raise BrokenPipeError("child exited")
    assert bind._state is None
    with bind.session():
        pass
    assert len(built) == 2


def test_transport_failures_are_told_apart_from_refusals():
    assert _is_transport_failure(BrokenPipeError("x"))
    assert _is_transport_failure(RuntimeError("stream is closed"))
    assert not _is_transport_failure(ValueError("no green test verdict exists for this wave"))


class _Client:
    def __exit__(self, *a):
        return False


def test_the_container_serves_what_serve_protocol_says(monkeypatch):
    """One image, two roles. Getting this wrong means the container starts and serves the wrong
    protocol, which AgentCore reports only as a failed health check."""
    import importlib

    called = []
    monkeypatch.setattr("tools.mcp_server.main", lambda: called.append("mcp"))
    monkeypatch.setattr("agents.agentic.main", lambda: called.append("http"))

    monkeypatch.setenv("SERVE_PROTOCOL", "MCP")
    importlib.reload(importlib.import_module("agents.serve")).main()
    monkeypatch.setenv("SERVE_PROTOCOL", "HTTP")
    importlib.reload(importlib.import_module("agents.serve")).main()
    monkeypatch.delenv("SERVE_PROTOCOL")
    importlib.reload(importlib.import_module("agents.serve")).main()

    assert called == ["mcp", "http", "http"]
