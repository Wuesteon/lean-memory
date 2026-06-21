# MCP Server for lean-memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose lean-memory as an MCP server so any MCP-compatible agent (Claude Desktop, Claude Code) can add, search, and clear agent memory through three tools.

**Architecture:** A single module `mcp_server.py` builds a module-level `FastMCP` instance and a module-level `Memory` instance. The `Memory` is wired with the real `SentenceTransformerEmbedder` + `CrossEncoderReranker` when the `models` extra is importable, otherwise it falls back to the offline stub backends (the `Memory` defaults). Three `@mcp.tool()` functions wrap `Memory.add`, `Memory.search`, and namespace-file deletion. The server runs over stdio via `python -m lean_memory.mcp_server` or the `lean-memory-mcp` console script.

**Tech Stack:** Python 3.10+, `mcp>=1.0` (`FastMCP` from `mcp.server.fastmcp`), the existing `lean_memory` package, pytest + anyio for in-memory MCP client tests.

## Global Constraints

- `requires-python = ">=3.10"` (already set in `pyproject.toml`; do not lower it).
- New dependency floor: `mcp>=1.0`, declared ONLY in a new optional extra named `mcp`. Never add `mcp` to the mandatory `[project].dependencies` — the core engine must stay installable without it.
- Use the stable `mcp>=1.0` FastMCP API exactly: `from mcp.server.fastmcp import FastMCP`. The in-memory test client is `create_connected_server_and_client_session` from `mcp.shared.memory`, and the underlying low-level server is reached via `mcp._mcp_server`. (A newer `mcp` v2 line renames `FastMCP`→`MCPServer` and exposes `mcp.client.Client`; `pip install 'mcp>=1.0'` resolves to the 1.x line this plan targets. If `create_connected_server_and_client_session` is genuinely absent after install, that means v2 was pulled — pin `mcp>=1.0,<2` in the extra and reinstall before proceeding.)
- Data root is configurable via the `LM_DATA_ROOT` environment variable, default `~/.lean_memory` (expand `~`). Read it once at module import.
- Backend selection is automatic: try to import `sentence_transformers`; on success use `SentenceTransformerEmbedder()` + `CrossEncoderReranker()`, else use the offline `Memory` defaults (`FakeEmbedder` + `IdentityReranker`). Never let an `ImportError` from the `models` extra crash the server.
- Tool return values are plain strings (MCP text content), never objects.
- `memory_add` returns exactly `"wrote N facts"` (N = number of facts written).
- `memory_search` returns a readable bulleted string, one bullet per hit, or `"No facts found."` when empty.
- `memory_clear` deletes the namespace's SQLite file AND its WAL/SHM sidecars (the store opens with `PRAGMA journal_mode=WAL`), and must first evict + close any cached open store for that namespace so the file handle is released before deletion.
- All work happens inside the project venv: `/Users/wuesteon/research/lean-memory/.venv` (python3.13). Activate it before running any command: `. /Users/wuesteon/research/lean-memory/.venv/bin/activate`.
- pytest is configured in `pyproject.toml` with `pythonpath = ["src"]` and `testpaths = ["tests"]`; run pytest from the repo root so the package resolves.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/lean_memory/mcp_server.py` | The MCP server: module-level `Memory` + `FastMCP`, backend auto-selection, the three tool functions, namespace→path helper, and a `main()` entry point. (~80 lines) |
| `examples/mcp_config.json` | Example Claude Desktop / Claude Code MCP client config pointing at the `lean-memory-mcp` console script. |
| `tests/test_mcp_server.py` | In-memory tests driving the three tools through the FastMCP client session; runs fully offline (stub backends), no network. |
| `pyproject.toml` (modify) | Add `mcp = ["mcp>=1.0"]` to `[project.optional-dependencies]` and a `[project.scripts]` `lean-memory-mcp` entry point. |

These changes are additive. No existing module is edited except `pyproject.toml`, and that edit only appends an extra and a script table — it cannot break the existing 17 tests.

---

### Reference: relevant existing API (read before starting, do not re-derive)

From `src/lean_memory/memory.py`:
- `Memory(root, *, embedder=None, reranker=None, generator=None, router=None, typer=None, contradiction=None)` — all backend args are keyword-only; passing `None` selects the offline stub.
- `Memory.add(namespace: str, text: str, *, t_ref=None, source="user") -> list[str]` — returns the ids of facts written. `t_ref` defaults to `now_ms()` when omitted.
- `Memory.search(namespace, query, k=5, *, as_of=None, is_latest_only=True) -> list[RetrievedFact]`.
- `Memory.close() -> None` — closes every cached store and clears the cache.
- Internals used by `memory_clear`: `Memory.root` is a `pathlib.Path`; `Memory._stores` is a `dict[str, SqliteStore]`; the per-namespace filename is computed as `_SAFE_NS.sub("_", namespace) or "default"` + `".db"` where `_SAFE_NS = re.compile(r"[^A-Za-z0-9_.-]")`. `SqliteStore.close()` closes the connection.

From `src/lean_memory/types.py`:
- `RetrievedFact` has `.fact` (a `Fact`) and scoring fields. The `Fact` has `.fact_text: str`, `.predicate: str`, `.object_literal: str | None`, `.valid_at: int`, `.subject_id: str`.

From `src/lean_memory/embed/sentence_transformer.py` and `src/lean_memory/retrieve/rerank.py`:
- `SentenceTransformerEmbedder(model_name="google/embeddinggemma-300m", ...)` and `CrossEncoderReranker(model_name="cross-encoder/ettin-reranker-32m-v1")`. Both are lazy — constructing them does NOT download a model; the download happens on first embed/score. They require `sentence_transformers` to be importable to be *useful*, but constructing `SentenceTransformerEmbedder` does not import it until `_ensure()`. Therefore the backend probe must import `sentence_transformers` directly to decide.

---

## Task 1: Add the `mcp` extra and console-script entry point to pyproject.toml

**Files:**
- Modify: `pyproject.toml` (the `[project.optional-dependencies]` table, lines 18-37; add a new `[project.scripts]` table)
- Test: `tests/test_mcp_server.py` (one config-presence test)

**Interfaces:**
- Consumes: nothing.
- Produces: an importable distribution metadata stating the `mcp` extra and a `lean-memory-mcp` console script mapped to `lean_memory.mcp_server:main`. Later tasks rely on `lean_memory.mcp_server:main` existing as the script target.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_server.py` with exactly this content (later tasks append to it):

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. /Users/wuesteon/research/lean-memory/.venv/bin/activate && pytest tests/test_mcp_server.py::test_pyproject_declares_mcp_extra_and_script -v`
Expected: FAIL with `KeyError: 'mcp'` (or an `AssertionError` on the script key).

- [ ] **Step 3: Edit pyproject.toml**

In `[project.optional-dependencies]`, add this block immediately after the `llm` extra (before `dev`):

```toml
# MCP server bridge: exposes lean-memory as memory tools to MCP-compatible
# agents (Claude Desktop, Claude Code). Optional — the core engine never imports mcp.
mcp = ["mcp>=1.0"]
```

Add a new top-level table (place it right after the `[project.optional-dependencies]` block and before `[build-system]`):

```toml
[project.scripts]
lean-memory-mcp = "lean_memory.mcp_server:main"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `. /Users/wuesteon/research/lean-memory/.venv/bin/activate && pytest tests/test_mcp_server.py::test_pyproject_declares_mcp_extra_and_script -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Install the mcp extra into the venv (needed for later tasks)**

Run: `. /Users/wuesteon/research/lean-memory/.venv/bin/activate && pip install -e '.[mcp,dev]'`
Expected: ends with `Successfully installed ... mcp-1.x.x ...`. Confirm the import works:
Run: `. /Users/wuesteon/research/lean-memory/.venv/bin/activate && python -c "from mcp.server.fastmcp import FastMCP; from mcp.shared.memory import create_connected_server_and_client_session; print('mcp OK')"`
Expected: `mcp OK`. If `create_connected_server_and_client_session` import fails (v2 was pulled), change the extra to `mcp = ["mcp>=1.0,<2"]`, re-run Step 4, then re-run this command.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tests/test_mcp_server.py
git commit -m "feat(mcp): add mcp optional extra and lean-memory-mcp console script

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: Implement the MCP server module

**Files:**
- Create: `src/lean_memory/mcp_server.py`
- Test: `tests/test_mcp_server.py` (append the tool tests)

**Interfaces:**
- Consumes: `lean_memory.Memory`, `lean_memory.memory._SAFE_NS` (the namespace-sanitizing regex), `RetrievedFact.fact.{fact_text,predicate,object_literal,valid_at}`, env var `LM_DATA_ROOT`.
- Produces:
  - module-level `mcp: FastMCP` (server object; tests reach the low-level server via `mcp._mcp_server`).
  - module-level `MEM: Memory`.
  - `memory_add(namespace: str, text: str) -> str` returning `"wrote N facts"`.
  - `memory_search(namespace: str, query: str, k: int = 5) -> str` returning a bulleted string or `"No facts found."`.
  - `memory_clear(namespace: str) -> str` returning `"cleared namespace '<ns>'"`.
  - `_namespace_path(namespace: str) -> pathlib.Path` (the `.db` file path for a namespace; used by `memory_clear` and tests).
  - `_build_memory(root) -> Memory` (backend auto-selection; used at import and re-usable in tests).
  - `main() -> None` (stdio entry point for the console script and `python -m`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
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
    for i in range(4):
        await _call(server, "memory_add", {"namespace": "u2", "text": f"Fact number {i} about widgets."})
    out = await _call(server, "memory_search", {"namespace": "u2", "query": "widgets", "k": 2})
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `. /Users/wuesteon/research/lean-memory/.venv/bin/activate && pytest tests/test_mcp_server.py -v`
Expected: the new tests FAIL/ERROR with `ModuleNotFoundError: No module named 'lean_memory.mcp_server'`. (The Task-1 pyproject test still passes.)

- [ ] **Step 3: Write the server implementation**

Create `src/lean_memory/mcp_server.py` with exactly this content:

```python
"""MCP server exposing lean-memory as agent-memory tools.

Run it:
    python -m lean_memory.mcp_server          # stdio transport
    lean-memory-mcp                            # console-script equivalent

Wire it into an MCP client (Claude Desktop / Claude Code) with examples/mcp_config.json.

Backends: if the `models` extra is installed (sentence-transformers importable), the
server uses the real SentenceTransformerEmbedder + CrossEncoderReranker for quality;
otherwise it falls back to lean-memory's offline stub defaults so it always runs.

Data root: LM_DATA_ROOT env var (default ~/.lean_memory). Each namespace is an
isolated SQLite file under that root (BET 4).
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .memory import _SAFE_NS, Memory


def _data_root() -> Path:
    root = os.environ.get("LM_DATA_ROOT", "~/.lean_memory")
    return Path(root).expanduser()


def _build_memory(root: Path) -> Memory:
    """Real backends when the `models` extra is present; offline stubs otherwise."""
    try:
        import sentence_transformers  # noqa: F401

        from .embed.sentence_transformer import SentenceTransformerEmbedder
        from .retrieve.rerank import CrossEncoderReranker

        return Memory(
            root=root,
            embedder=SentenceTransformerEmbedder(),
            reranker=CrossEncoderReranker(),
        )
    except ImportError:
        # `models` extra not installed — deterministic offline stubs.
        return Memory(root=root)


mcp = FastMCP("lean-memory")
MEM = _build_memory(_data_root())


def _namespace_path(namespace: str) -> Path:
    """The SQLite file backing a namespace (mirrors Memory._store's naming)."""
    safe = _SAFE_NS.sub("_", namespace) or "default"
    return MEM.root / f"{safe}.db"


@mcp.tool()
def memory_add(namespace: str, text: str) -> str:
    """Ingest text into the namespace's memory. Returns how many facts were written."""
    written = MEM.add(namespace, text)
    n = len(written)
    return f"wrote {n} fact{'s' if n != 1 else ''}"


@mcp.tool()
def memory_search(namespace: str, query: str, k: int = 5) -> str:
    """Search a namespace's memory and return the top-k facts as a bulleted list."""
    hits = MEM.search(namespace, query, k=k)
    if not hits:
        return "No facts found."
    return "\n".join(f"- {h.fact.fact_text}" for h in hits)


@mcp.tool()
def memory_clear(namespace: str) -> str:
    """Delete all memory for a namespace by removing its SQLite file. Irreversible."""
    # Release any cached open connection so the file handle is freed before unlink.
    store = MEM._stores.pop(namespace, None)
    if store is not None:
        store.close()
    path = _namespace_path(namespace)
    for p in (path, path.with_suffix(".db-wal"), path.with_suffix(".db-shm")):
        p.unlink(missing_ok=True)
    return f"cleared namespace '{namespace}'"


def main() -> None:
    """Console-script / module entry point: serve over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. /Users/wuesteon/research/lean-memory/.venv/bin/activate && pytest tests/test_mcp_server.py -v`
Expected: PASS — all tests in the file pass (the pyproject test plus the 8 server tests).

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `. /Users/wuesteon/research/lean-memory/.venv/bin/activate && pytest -q`
Expected: all tests pass (the original 17 plus the new ones), `<2s`, e.g. `25 passed`.

- [ ] **Step 6: Smoke-test the entry points (manual, optional but recommended)**

Run: `. /Users/wuesteon/research/lean-memory/.venv/bin/activate && python -c "import lean_memory.mcp_server as s; print(type(s.mcp).__name__, s.memory_add('smoke', 'I work at Acme.'), s.memory_search('smoke', 'work'))"`
Expected: prints `FastMCP wrote 1 fact - I work at Acme.` (fact text may vary slightly; the point is it does not raise). This writes under `~/.lean_memory/smoke.db`; remove it after with the `memory_clear` smoke if desired.

- [ ] **Step 7: Commit**

```bash
git add src/lean_memory/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): add FastMCP server with add/search/clear memory tools

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Add the example MCP client config and document usage

**Files:**
- Create: `examples/mcp_config.json`
- Test: `tests/test_mcp_server.py` (append a JSON-validity + shape test)

**Interfaces:**
- Consumes: the `lean-memory-mcp` console script defined in Task 1.
- Produces: `examples/mcp_config.json`, a drop-in `mcpServers` block for Claude Desktop / Claude Code.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_server.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. /Users/wuesteon/research/lean-memory/.venv/bin/activate && pytest tests/test_mcp_server.py::test_example_config_is_valid_and_points_at_script -v`
Expected: FAIL with `FileNotFoundError` for `examples/mcp_config.json`.

- [ ] **Step 3: Create the example config**

Create `examples/mcp_config.json` with exactly this content:

```json
{
  "mcpServers": {
    "lean-memory": {
      "command": "lean-memory-mcp",
      "args": [],
      "env": {
        "LM_DATA_ROOT": "~/.lean_memory"
      }
    }
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `. /Users/wuesteon/research/lean-memory/.venv/bin/activate && pytest tests/test_mcp_server.py::test_example_config_is_valid_and_points_at_script -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Run the full suite**

Run: `. /Users/wuesteon/research/lean-memory/.venv/bin/activate && pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add examples/mcp_config.json tests/test_mcp_server.py
git commit -m "docs(mcp): add example Claude Desktop/Code MCP client config

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage** — every requirement maps to a task:
- Three tools `memory_add` / `memory_search` / `memory_clear` with the exact signatures and return contracts → Task 2 (functions + tests for count string, bulleted output, empty case, k-limit, file deletion, post-clear-empty).
- `FastMCP` from the `mcp` SDK → Task 2 (`from mcp.server.fastmcp import FastMCP`, module-level `mcp`).
- Module-level `Memory` with real backends if `models` installed else stubs → Task 2 (`_build_memory` probes `sentence_transformers`).
- Root via `LM_DATA_ROOT` default `~/.lean_memory` → Task 2 (`_data_root`, tested via the `server` fixture's `monkeypatch.setenv` + reload).
- Runnable as `python -m lean_memory.mcp_server` and `lean-memory-mcp` → Task 2 (`main()` + `__main__` guard) and Task 1 (`[project.scripts]`).
- `src/lean_memory/mcp_server.py` (~80 lines) → Task 2 (the file is ~85 lines including docstring).
- `examples/mcp_config.json` → Task 3.
- `tests/test_mcp_server.py` using FastMCP's test client → Task 2 (`create_connected_server_and_client_session(mcp._mcp_server)`).
- `mcp = ["mcp>=1.0"]` extra + `lean-memory-mcp` script → Task 1.
- "create this dir" `examples/` → created implicitly by writing `examples/mcp_config.json` in Task 3.

**2. Placeholder scan** — no `TBD`/`TODO`/"handle edge cases"/"add validation"/"similar to Task N"; every code step shows full code; every run step shows the exact command and expected output. None found.

**3. Type consistency** — `memory_clear` returns `"cleared namespace '<ns>'"` and the Task-2 test asserts exactly that string. `_namespace_path` is defined once and referenced by `memory_clear`, the `test_memory_clear_deletes_file` test, and `test_namespace_path_sanitizes` — same name everywhere. `MEM` (not `mem`) is the module-level Memory in both the implementation and the fixture's `srv.MEM = ...` override. `mcp._mcp_server` is used consistently in `_call`. The bullet prefix is `"- "` in both implementation and the `lstrip().startswith("-")` assertion. The WAL/SHM suffixes `.db-wal` / `.db-shm` match between `memory_clear` and `test_memory_clear_deletes_file`.

One watch-item for the implementer, not a gap: `Fact.predicate`/`object_literal`/`valid_at` exist on `RetrievedFact.fact` and are available if you want a richer bullet later; this plan deliberately renders only `fact_text` to satisfy the "readable bulleted string" contract with the simplest correct output.
