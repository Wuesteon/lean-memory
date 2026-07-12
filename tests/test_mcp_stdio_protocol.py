"""Protocol-level smoke test: the real stdio server must emit ONLY JSON on stdout.

The v0.1.2 launch blockers (missing [mcp] extra in the registry manifest, model
banner corrupting JSON-RPC) were exactly the class of bug the in-process tool
tests cannot catch: they never spawn the real server or watch fd 1. This test
launches `python -m lean_memory.mcp_server` as a subprocess, performs the MCP
handshake, CALLS a tool (load-time/ingest-path chatter only ever hits stdout
mid-call, never during the bare handshake), lists tools, and asserts every
stdout line is valid JSON.

Stays offline: LM_FORCE_STUBS pins the deterministic stub backends so no model
is ever loaded, and PYTHONUNBUFFERED makes any stray print hit fd 1 immediately
instead of hiding in a block buffer until exit.
"""

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="requires the [mcp] extra")

_SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_build_memory_honors_force_stubs(monkeypatch, tmp_path):
    """LM_FORCE_STUBS pins the offline stub backends regardless of installed
    extras — what keeps this module's subprocess test model-free everywhere."""
    from lean_memory.embed.fake import FakeEmbedder
    from lean_memory.extract.gliner_extractor import StubCandidateGenerator
    from lean_memory.mcp_server import _build_memory

    monkeypatch.setenv("LM_FORCE_STUBS", "1")
    mem = _build_memory(tmp_path)
    assert isinstance(mem.embedder, FakeEmbedder)
    assert isinstance(mem.generator, StubCandidateGenerator)
    mem.close()


class _StdioClient:
    """Minimal line-oriented JSON-RPC client over a server subprocess."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.lines: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)

    def send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def expect(self, msg_id: int, timeout: float = 30.0) -> dict:
        """Read stdout lines (each MUST be JSON) until the response with msg_id."""
        while True:
            line = self.lines.get(timeout=timeout)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(f"non-JSON bytes on the MCP protocol channel: {line!r}")
            if msg.get("id") == msg_id:
                return msg


def test_stdio_server_speaks_pure_json(tmp_path):
    env = dict(os.environ)
    env["LM_DATA_ROOT"] = str(tmp_path)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["LM_FORCE_STUBS"] = "1"  # no model loads — offline and fast everywhere
    env["PYTHONUNBUFFERED"] = "1"  # stray prints hit fd 1 immediately, not at exit

    proc = subprocess.Popen(
        [sys.executable, "-m", "lean_memory.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True,
    )
    try:
        client = _StdioClient(proc)
        client.send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "stdio-smoke", "version": "0"},
            },
        })
        client.expect(1)
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        client.send({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "memory_add",
                "arguments": {"namespace": "smoke", "text": "I work at Acme."},
            },
        })
        add_reply = client.expect(2)
        assert not add_reply["result"].get("isError"), f"memory_add failed: {add_reply}"

        client.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        tools_reply = client.expect(3)
        names = {t["name"] for t in tools_reply["result"]["tools"]}
        assert {"memory_add", "memory_search", "memory_clear"} <= names

        proc.stdin.close()  # EOF → clean shutdown
        proc.wait(timeout=30)
    except queue.Empty:
        pytest.fail(f"no response from server; stderr: {proc.stderr.read()[-500:]}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
