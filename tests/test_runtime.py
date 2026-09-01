import json
import threading
import urllib.request

import agents.runtime as rt
from agents.orchestrator import WaveOutcome
from tools.spec import load_contract


def test_invoke_runs_the_wave(monkeypatch):
    seen = {}

    class _FakeOrq:
        def run_wave(self, wave_id, inputs, *, require_contract_approval=False):
            seen.update(wave_id=wave_id, inputs=inputs, gate=require_contract_approval)
            return WaveOutcome.DONE

    monkeypatch.setattr(rt, "_build", lambda _contract: _FakeOrq())
    out = rt.invoke(
        {
            "contract": load_contract().model_dump(mode="json"),
            "wave_id": "w1",
            "inputs": {"interpret_objective": "x"},
            "require_contract_approval": True,
        }
    )
    assert out == {"wave_id": "w1", "outcome": "DONE"}
    assert seen["wave_id"] == "w1" and seen["gate"] is True
    assert seen["inputs"].interpret_objective == "x"


def test_ping_and_unknown_path():
    server = rt.serve(port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=5) as r:
            assert r.status == 200 and json.loads(r.read())["status"] == "ok"
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()
        server.server_close()
