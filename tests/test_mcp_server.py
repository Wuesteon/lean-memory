"""Tests for the lean-memory MCP server.

All tests run offline: the server is constructed with the default stub backends
(FakeEmbedder + IdentityReranker) by pointing LM_DATA_ROOT at a tmp dir BEFORE
the module is imported, and by importing inside the tests so each test gets a
fresh module-level Memory rooted at its own tmp dir.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_pyproject_declares_mcp_extra_and_script():
    data = tomllib.loads(PYPROJECT.read_text())
    extras = data["project"]["optional-dependencies"]
    assert "mcp" in extras, "an 'mcp' optional extra must be declared"
    assert any(req.startswith("mcp>=1.0") for req in extras["mcp"]), extras["mcp"]
    scripts = data["project"].get("scripts", {})
    assert scripts.get("lean-memory-mcp") == "lean_memory.mcp_server:main"


import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Import the server module fresh, rooted at a tmp dir with stub backends."""
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    import importlib

    import lean_memory.mcp_server as srv

    importlib.reload(srv)  # re-read LM_DATA_ROOT and rebuild MEM at the tmp root
    # Force offline stub backends regardless of whether sentence-transformers is
    # installed in this environment, so tests are deterministic and fast.
    from lean_memory import Memory

    srv.MEM = Memory(root=tmp_path)
    yield srv
    srv.MEM.close()


async def _call(server, name, args):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
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
    assert p.parent == server.MEM.root
