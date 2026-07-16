"""Model lazy-loads must never write to stdout — it is the MCP stdio protocol channel.

Regression for the v0.1.2 launch blocker: gliner2's GLiNER2.from_pretrained prints a
model-configuration banner with bare print() calls. On the canonical
`[mcp,models,extract]` install the first memory_add tool call lazy-loads the model,
interleaving that banner with the JSON-RPC response on fd 1 and breaking the MCP
client's line-oriented parser. The same class of bug can appear in any heavy backend,
so every lazy loader routes load-time chatter to stderr.

Fakes are injected via sys.modules so the suite stays offline (no torch import,
no downloads) while exercising the real _ensure* code paths.
"""

import subprocess
import sys
import types

import pytest

from lean_memory.extract.gliner_extractor import Gliner2Generator


def _fake_gliner2_module() -> types.ModuleType:
    mod = types.ModuleType("gliner2")

    class GLiNER2:
        @classmethod
        def from_pretrained(cls, name, **kw):
            print("=" * 60)
            print("Model Configuration")  # mimics gliner2/model.py's stdout banner
            return cls()

    mod.GLiNER2 = GLiNER2
    return mod


def _fake_sentence_transformers_module() -> types.ModuleType:
    mod = types.ModuleType("sentence_transformers")

    class SentenceTransformer:
        def __init__(self, name, device=None):
            print(f"loading {name}")  # load-time chatter must not reach stdout

        def get_sentence_embedding_dimension(self):
            return 1024

    class CrossEncoder:
        def __init__(self, name):
            print(f"loading {name}")

    mod.SentenceTransformer = SentenceTransformer
    mod.CrossEncoder = CrossEncoder
    return mod


def test_gliner_lazy_load_keeps_stdout_clean(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "gliner2", _fake_gliner2_module())
    gen = Gliner2Generator()
    model = gen._ensure_model()
    assert model is not None
    out, err = capsys.readouterr()
    assert out == "", f"model load leaked to stdout: {out!r}"
    assert "Model Configuration" in err  # chatter still visible, on stderr


def test_embedder_lazy_load_keeps_stdout_clean(monkeypatch, capsys):
    from lean_memory.embed.sentence_transformer import SentenceTransformerEmbedder

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", _fake_sentence_transformers_module()
    )
    e = SentenceTransformerEmbedder()
    e._ensure()
    assert e.dim == 1024
    out, _ = capsys.readouterr()
    assert out == "", f"model load leaked to stdout: {out!r}"


def test_reranker_lazy_load_keeps_stdout_clean(monkeypatch, capsys):
    from lean_memory.retrieve.rerank import CrossEncoderReranker

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", _fake_sentence_transformers_module()
    )
    r = CrossEncoderReranker()
    r._ensure()
    out, _ = capsys.readouterr()
    assert out == "", f"model load leaked to stdout: {out!r}"


# ── auto-spawn stdout hygiene (LM_MAINT_AUTO=1, spec §6.5) ────────────────────
# The opt-in auto-spawn fires the maintenance CLI as a DETACHED child on the first
# tool call. The v0.1.3 lesson is that fd 1 (the JSON-RPC channel) must never be
# inherited: the child's stdout is DEVNULL and the exact Popen primitives are pinned.

mcp = pytest.importorskip("mcp", reason="optional [mcp] extra not installed")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _fresh_server(tmp_path, monkeypatch):
    """Reload the core server rooted at tmp_path with stub backends and auto ON."""
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("LM_MAINT_AUTO", "1")
    import importlib

    import lean_memory.mcp_server as srv

    importlib.reload(srv)
    from lean_memory import Memory

    srv._MEM = Memory(root=tmp_path)
    srv._AUTO_SPAWN_FIRED = False  # reload already reset it; be explicit
    return srv


async def _call(srv, name, args):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(srv.mcp._mcp_server) as s:
        result = await s.call_tool(name, args)
        return result.content[0].text


def test_autospawn_uses_exact_popen_primitives(tmp_path, monkeypatch):
    """A stale namespace + LM_MAINT_AUTO=1 spawns with the EXACT §6.5 Popen kwargs.

    We seed a namespace DB (so there is something to maintain), monkeypatch
    subprocess.Popen to capture kwargs WITHOUT launching, then fire a tool call and
    assert fd 1 is never inherited (stdout=DEVNULL) and the detach primitives hold.
    """
    from lean_memory import Memory

    seed = Memory(root=tmp_path)
    seed.add("proj", "I work at Acme.")
    seed.close()

    srv = _fresh_server(tmp_path, monkeypatch)

    captured: dict = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs

        def poll(self):
            return 0

    monkeypatch.setattr(srv.mcp_support.subprocess, "Popen", _FakePopen)

    import asyncio

    asyncio.run(_call(srv, "memory_maintenance_status", {"namespace": "proj"}))
    # status is model-free and does not go through _mem(); the FIRST tool call that
    # touches _mem() triggers the spawn — drive one.
    if "argv" not in captured:
        asyncio.run(_call(srv, "memory_add", {"namespace": "proj", "text": "hi"}))

    assert "argv" in captured, "auto-spawn did not fire for a stale namespace"
    argv, kwargs = captured["argv"], captured["kwargs"]
    # The CLI invocation: apply + auto-only, module form under this interpreter.
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "lean_memory.maintain.cli"]
    assert "--apply" in argv and "--auto-only" in argv
    assert "proj" in argv
    # fd 1 NEVER inherited — the load-bearing hygiene assertion.
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True


def test_autospawn_fires_at_most_once(tmp_path, monkeypatch):
    """The spawn check runs once per process, even across many tool calls (§6.5)."""
    from lean_memory import Memory

    seed = Memory(root=tmp_path)
    seed.add("proj", "I work at Acme.")
    seed.close()

    srv = _fresh_server(tmp_path, monkeypatch)
    calls = {"n": 0}

    def _count_spawn(root, namespace):
        calls["n"] += 1

    monkeypatch.setattr(srv.mcp_support, "spawn_maintenance", _count_spawn)

    import asyncio

    asyncio.run(_call(srv, "memory_add", {"namespace": "proj", "text": "one"}))
    asyncio.run(_call(srv, "memory_add", {"namespace": "proj", "text": "two"}))
    asyncio.run(_call(srv, "memory_search", {"namespace": "proj", "query": "x"}))
    # One namespace, one stale spawn, and the once-flag blocks re-checking after.
    assert calls["n"] == 1


def test_autospawn_off_by_default(tmp_path, monkeypatch):
    """Without LM_MAINT_AUTO=1 (default), no child is ever spawned."""
    from lean_memory import Memory

    seed = Memory(root=tmp_path)
    seed.add("proj", "I work at Acme.")
    seed.close()

    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("LM_MAINT_AUTO", raising=False)
    import importlib

    import lean_memory.mcp_server as srv

    importlib.reload(srv)
    srv._MEM = Memory(root=tmp_path)
    fired = {"n": 0}
    monkeypatch.setattr(
        srv.mcp_support, "spawn_maintenance", lambda r, n: fired.__setitem__("n", 1)
    )

    import asyncio

    asyncio.run(_call(srv, "memory_add", {"namespace": "proj", "text": "hi"}))
    srv._MEM.close()
    assert fired["n"] == 0


def test_autospawn_real_child_runs_to_completion(tmp_path, monkeypatch):
    """A REAL detached child runs to completion against a tmp root; parent fd 1 is
    untouched. This exercises the actual Popen call end to end (not a monkeypatch),
    proving the CLI module invocation is well-formed and the child exits cleanly.
    """
    from lean_memory import Memory

    seed = Memory(root=tmp_path)
    seed.add("proj", "I work at Acme.")
    seed.close()

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "lean_memory.maintain.cli",
            "--root", str(tmp_path), "--namespace", "proj",
            "--apply", "--auto-only",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    _out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, f"child failed: {err!r}"
