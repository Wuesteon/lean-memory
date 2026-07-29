"""Tests for the lean-memory MCP server.

All tests run offline: the server is constructed with the default stub backends
(FakeEmbedder + IdentityReranker) by pointing LM_DATA_ROOT at a tmp dir BEFORE
the module is imported, and by importing inside the tests so each test gets a
fresh module-level Memory rooted at its own tmp dir. Memory is built LAZILY on
first tool call (see test_module_import_does_not_build_memory), so the fixture
injects a stub-backed Memory into `srv._MEM` to short-circuit that build.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The MCP server (and thus every test that imports it) needs the optional [mcp]
# extra. Skip the whole module with a clear reason when it is absent, so a bare
# `pip install -e '.[dev]'` + pytest run skips these tests instead of erroring
# at collection. CI installs `.[dev,mcp]`, so all tests still run there.
mcp = pytest.importorskip("mcp", reason="optional [mcp] extra not installed")

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_pyproject_declares_mcp_extra_and_script():
    data = tomllib.loads(PYPROJECT.read_text())
    extras = data["project"]["optional-dependencies"]
    assert "mcp" in extras, "an 'mcp' optional extra must be declared"
    # The <2 cap is load-bearing: mcp 2.0.0 removed mcp.server.fastmcp, which
    # the server imports. Lift only with a verified 2.0 MCPServer migration.
    assert any(req.startswith("mcp>=") and req.endswith(",<2") for req in extras["mcp"]), extras["mcp"]
    scripts = data["project"].get("scripts", {})
    assert scripts.get("lean-memory-mcp") == "lean_memory.mcp_server:main"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Import the server module fresh, rooted at a tmp dir with stub backends."""
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    import importlib

    import lean_memory.mcp_server as srv

    importlib.reload(srv)  # re-read LM_DATA_ROOT; MEM is now built lazily on first use
    # Force offline stub backends regardless of whether sentence-transformers is
    # installed in this environment, so tests are deterministic and fast. Inject
    # directly into the lazy slot so _mem() returns this instead of building one.
    from lean_memory import Memory

    srv._MEM = Memory(root=tmp_path)
    yield srv
    srv._MEM.close()


async def _call(server, name, args):
    from mcp_client_compat import client_session

    async with client_session(server.mcp) as session:
        result = await session.call_tool(name, args)
        # CallToolResult.content is a list of content blocks; the first is text.
        return result.content[0].text


@pytest.mark.anyio
async def test_memory_add_reports_count(server):
    out = await _call(server, "memory_add", {"namespace": "u1", "text": "I work at Acme."})
    assert out.startswith("wrote ")
    assert "fact" in out
    # the stub extractor emits >=1 fact for a 'works at' sentence
    n = int(out.split()[1])
    assert n >= 1


@pytest.mark.anyio
async def test_memory_search_returns_bullets(server):
    await _call(server, "memory_add", {"namespace": "u1", "text": "I work at Acme."})
    out = await _call(server, "memory_search", {"namespace": "u1", "query": "where does the user work?"})
    assert out != "No facts found."
    assert out.lstrip().startswith("-"), out
    assert "Acme" in out


@pytest.mark.anyio
async def test_memory_search_empty_namespace(server):
    out = await _call(server, "memory_search", {"namespace": "nobody", "query": "anything"})
    assert out == "No facts found."


@pytest.mark.anyio
async def test_memory_search_respects_k(server):
    # Sentences the offline stub extractor recognizes (first-person + a known
    # relation verb), each with a distinct object, so 4 facts are written and a
    # shared-theme query can return more than k before the k cap is applied.
    for i in range(4):
        await _call(server, "memory_add", {"namespace": "u2", "text": f"I use widget Alpha{i}."})
    out = await _call(server, "memory_search", {"namespace": "u2", "query": "widget", "k": 2})
    # at most k bullets
    bullets = [ln for ln in out.splitlines() if ln.lstrip().startswith("-")]
    assert 1 <= len(bullets) <= 2


@pytest.mark.anyio
async def test_memory_clear_deletes_file(server):
    await _call(server, "memory_add", {"namespace": "u3", "text": "I work at Acme."})
    path = server._namespace_path("u3")
    assert path.exists()
    out = await _call(server, "memory_clear", {"namespace": "u3"})
    assert out == "cleared namespace 'u3'"
    assert not path.exists()
    # WAL/SHM sidecars are gone too
    assert not path.with_suffix(".db-wal").exists()
    assert not path.with_suffix(".db-shm").exists()


@pytest.mark.anyio
async def test_memory_clear_then_search_is_empty(server):
    await _call(server, "memory_add", {"namespace": "u4", "text": "I work at Acme."})
    await _call(server, "memory_clear", {"namespace": "u4"})
    out = await _call(server, "memory_search", {"namespace": "u4", "query": "work"})
    assert out == "No facts found."


def test_namespace_path_sanitizes(server):
    p = server._namespace_path("a/b user")
    assert p.name == "a_b_user.db"
    assert p.parent == server._MEM.root


@pytest.mark.anyio
async def test_memory_search_dedups_identical_fact_text(server):
    """Identical fact_text appearing twice in top-k yields a single bullet.

    GLiNER over-generation can surface the same sentence multiple times in the
    same top-k window; memory_search must collapse exact-fact_text duplicates.
    We seed the duplicate directly via Memory.add (adding the same sentence
    twice) so the assertion holds regardless of extractor stub specifics.
    """
    sentence = "I use widget Zeta."
    await _call(server, "memory_add", {"namespace": "dup", "text": sentence})
    await _call(server, "memory_add", {"namespace": "dup", "text": sentence})
    out = await _call(server, "memory_search", {"namespace": "dup", "query": "widget Zeta", "k": 5})
    bullets = [ln for ln in out.splitlines() if ln.lstrip().startswith("-")]
    texts = [ln.lstrip()[2:] for ln in bullets]
    # No fact_text repeats across the returned bullets.
    assert len(texts) == len(set(texts)), texts
    # And the seeded sentence appears exactly once, not twice.
    assert texts.count(sentence) <= 1
    assert sum(1 for t in texts if t == sentence) == 1


def test_module_import_does_not_build_memory(tmp_path, monkeypatch):
    """Importing/reloading the module must NOT construct Memory (lazy load, P1-4).

    Cold-cache first spawn would otherwise block an MCP client through a ~2 GB
    model download at import time. We make _build_memory raise, reload the
    module, and assert the import succeeds; the first _mem() call then builds it.
    """
    import importlib

    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    import lean_memory.mcp_server as srv

    importlib.reload(srv)  # baseline reload under the tmp root

    calls = {"n": 0}

    def boom(root):
        calls["n"] += 1
        raise RuntimeError("Memory must not be built at import time")

    monkeypatch.setattr(srv, "_build_memory", boom)
    monkeypatch.setattr(srv, "_MEM", None)

    # Reload again with the exploding builder in place: import alone must not build.
    importlib.reload(srv)
    # reload rebinds the module's own name, so re-patch after reload for the call check.
    monkeypatch.setattr(srv, "_build_memory", boom)
    monkeypatch.setattr(srv, "_MEM", None)
    assert calls["n"] == 0, "import/reload built Memory eagerly"

    # First _mem() call triggers exactly one build attempt.
    with pytest.raises(RuntimeError):
        srv._mem()
    assert calls["n"] == 1


import json

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "examples" / "mcp_config.json"


def test_example_config_is_valid_and_points_at_script():
    data = json.loads(EXAMPLE_CONFIG.read_text())
    servers = data["mcpServers"]
    assert "lean-memory" in servers
    entry = servers["lean-memory"]
    assert entry["command"] == "lean-memory-mcp"
    # LM_DATA_ROOT is documented in the example env block
    assert "LM_DATA_ROOT" in entry.get("env", {})


# ── _build_memory opportunistic upgrades (P0-3) ──────────────────────────────
# _build_memory has two INDEPENDENT opportunistic upgrades: sentence_transformers
# (embedder+reranker) and gliner2 (extraction). These tests pin the fallback and
# the GLiNER-active behaviour offline, without any model download, by controlling
# what is importable via sys.modules.


def test_build_memory_falls_back_to_stubs_when_no_extras(tmp_path, monkeypatch):
    """Neither extra importable → default Memory: stub embedder AND StubCandidateGenerator."""
    import sys

    import lean_memory.mcp_server as srv
    from lean_memory.embed.fake import FakeEmbedder
    from lean_memory.extract.gliner_extractor import StubCandidateGenerator
    from lean_memory.retrieve.rerank import IdentityReranker

    # Make both optional extras un-importable regardless of the environment: block
    # the import and evict any cached module so the try/except takes the fallback.
    real_import = __import__

    def blocked_import(name, *args, **kwargs):
        if name in ("sentence_transformers", "gliner2"):
            raise ImportError(f"blocked {name} for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    monkeypatch.delitem(sys.modules, "gliner2", raising=False)
    monkeypatch.setattr("builtins.__import__", blocked_import)

    mem = srv._build_memory(tmp_path)
    try:
        assert isinstance(mem.embedder, FakeEmbedder)
        assert isinstance(mem.reranker, IdentityReranker)
        assert isinstance(mem.generator, StubCandidateGenerator)
    finally:
        mem.close()


def test_build_memory_uses_gliner2_when_importable(tmp_path, monkeypatch):
    """gliner2 importable → generator is a Gliner2Generator (no model download).

    We inject a fake `gliner2` module so the `import gliner2` guard succeeds; the
    real weights never load because Gliner2Generator lazy-loads GLiNER2 only on the
    first generate() call, which this test never makes.
    """
    import sys
    import types as _types

    import lean_memory.mcp_server as srv
    from lean_memory.extract.gliner_extractor import Gliner2Generator

    fake_gliner2 = _types.ModuleType("gliner2")
    monkeypatch.setitem(sys.modules, "gliner2", fake_gliner2)

    mem = srv._build_memory(tmp_path)
    try:
        assert isinstance(mem.generator, Gliner2Generator)
    finally:
        mem.close()
