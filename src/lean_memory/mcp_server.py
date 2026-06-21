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
