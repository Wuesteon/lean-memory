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
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .memory import _SAFE_NS, Memory


def _data_root() -> Path:
    root = os.environ.get("LM_DATA_ROOT", "~/.lean_memory")
    return Path(root).expanduser()


def _build_memory(root: Path) -> Memory:
    """Opportunistically upgrade each backend when its optional extra is present.

    Two INDEPENDENT upgrades, each guarded by its own import so either can succeed
    without the other (`[models]` without `[extract]`, or vice versa):
      * `models`  (sentence_transformers) → real embedder + reranker for retrieval.
      * `extract` (gliner2)               → Gliner2Generator for real extraction.
    Anything not installed falls back to lean-memory's deterministic offline stubs,
    so the server always runs. GLiNER matters especially here: the frozen escalation/
    granularity constants were calibrated ON the GLiNER path, so shipping it aligns
    the canonical install with what the engine was tuned against.

    Each successful upgrade logs ONE line to stderr (stdout is the MCP protocol
    channel — NEVER print to stdout here) so cold-cache users, who wait through a
    ~2 GB download on first tool call, see progress in their client's server logs.
    """
    kwargs: dict = {}

    try:
        import sentence_transformers  # noqa: F401

        from .embed.sentence_transformer import SentenceTransformerEmbedder
        from .retrieve.rerank import CrossEncoderReranker

        kwargs["embedder"] = SentenceTransformerEmbedder()
        kwargs["reranker"] = CrossEncoderReranker()
        print("[lean-memory] models extra active: real embedder + reranker", file=sys.stderr)
    except ImportError:
        # `models` extra not installed — keep the deterministic offline stub backends.
        pass

    try:
        import gliner2  # noqa: F401

        from .extract.gliner_extractor import Gliner2Generator

        kwargs["generator"] = Gliner2Generator()
        print("[lean-memory] extract extra active: GLiNER2 extraction", file=sys.stderr)
    except ImportError:
        # `extract` extra not installed — keep the stub candidate generator.
        pass

    return Memory(root=root, **kwargs)


mcp = FastMCP("lean-memory")

# Lazy, build-once Memory. Import-time construction would block an MCP client's
# server spawn through a ~2 GB cold-cache model download with no handshake; the
# first tool call pays that cost instead, so the handshake is immediate. A plain
# check-and-build is thread-safe enough for MCP's single-process stdio transport
# (tool calls are serialized on one event loop; no concurrent _mem() races).
_MEM: Optional[Memory] = None


def _mem() -> Memory:
    """Return the module-level Memory, building it (once) on first use."""
    global _MEM
    if _MEM is None:
        _MEM = _build_memory(_data_root())
    return _MEM


def _namespace_path(namespace: str) -> Path:
    """The SQLite file backing a namespace (mirrors Memory._store's naming)."""
    safe = _SAFE_NS.sub("_", namespace) or "default"
    return _mem().root / f"{safe}.db"


@mcp.tool()
def memory_add(namespace: str, text: str) -> str:
    """Ingest text into the namespace's memory. Returns how many facts were written."""
    written = _mem().add(namespace, text)
    n = len(written)
    return f"wrote {n} fact{'s' if n != 1 else ''}"


@mcp.tool()
def memory_search(namespace: str, query: str, k: int = 5) -> str:
    """Search a namespace's memory and return the top-k facts as a bulleted list."""
    hits = _mem().search(namespace, query, k=k)
    if not hits:
        return "No facts found."
    # De-duplicate by exact fact_text, keeping the highest-ranked instance and
    # preserving order: GLiNER over-generation can surface the same sentence
    # multiple times in top-k, which would print duplicate bullets.
    seen: set[str] = set()
    bullets = []
    for h in hits:
        text = h.fact.fact_text
        if text in seen:
            continue
        seen.add(text)
        bullets.append(f"- {text}")
    return "\n".join(bullets)


@mcp.tool()
def memory_clear(namespace: str) -> str:
    """Delete all memory for a namespace by removing its SQLite file. Irreversible."""
    # Release any cached open connection so the file handle is freed before unlink.
    store = _mem()._stores.pop(namespace, None)
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
