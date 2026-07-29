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

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from ._mcp_compat import make_stdio_server

from . import __version__
from .maintain import live_lease_is_fresh, mcp_support
from .maintain.cli import _report_to_dict
from .maintain.config import MaintenanceConfig
from .memory import _SAFE_NS, Memory
from .types import now_ms


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

    LM_FORCE_STUBS (env) pins the deterministic offline stubs even when the
    extras are installed — for tests/CI that must never load a model.
    """
    if os.environ.get("LM_FORCE_STUBS"):
        return Memory(root=root)

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


# serverInfo must report lean-memory's version, not the SDK's; the compat
# factory handles the per-major mechanics (2.x ctor kwarg vs 1.x low-level poke).
mcp = make_stdio_server("lean-memory", version=__version__)

# Lazy, build-once Memory. Import-time construction would block an MCP client's
# server spawn through a ~2 GB cold-cache model download with no handshake; the
# first tool call pays that cost instead, so the handshake is immediate. A plain
# check-and-build is thread-safe enough for MCP's single-process stdio transport
# (tool calls are serialized on one event loop; no concurrent _mem() races).
_MEM: Optional[Memory] = None

# Opt-in auto-spawn fires AT MOST ONCE per server process (§6.5). This flag flips on
# the first tool call regardless of whether a child was actually spawned, so a fresh
# (non-stale) namespace does not re-check on every subsequent call.
_AUTO_SPAWN_FIRED = False


def _maybe_auto_spawn(namespace: Optional[str] = None) -> None:
    """Opt-in (`LM_MAINT_AUTO=1`, default OFF) background maintenance spawn (§6.5).

    Fires AT MOST ONCE per server process, on the FIRST tool call, for the namespace
    that call touched. Reads the ledger cheaply (no model build) to decide staleness;
    if stale, spawns `lean-memory-maintain --apply --auto-only` detached with fd 1
    NEVER inherited (mcp_support.spawn_maintenance owns the exact Popen primitives).
    Never blocks, never waits on the child, never raises into the tool path — a spawn
    failure must not break a memory_add.
    """
    global _AUTO_SPAWN_FIRED
    if _AUTO_SPAWN_FIRED:
        return
    _AUTO_SPAWN_FIRED = True  # once-per-process, set before any work so it never retries
    if os.environ.get("LM_MAINT_AUTO") != "1" or not namespace:
        return
    root = _data_root()
    try:
        if not root.is_dir():
            return
        if mcp_support.is_stale(root, namespace, MaintenanceConfig()):
            mcp_support.spawn_maintenance(root, namespace)
    except Exception:
        # Auto-spawn is best-effort background hygiene; it must never fail a tool call.
        pass


def _mem(namespace: Optional[str] = None) -> Memory:
    """Return the module-level Memory, building it (once) on first use.

    The first call also triggers the opt-in auto-spawn staleness check for `namespace`
    (§6.5) — the lazy-build point is the only "first tool call" hook the stdio server
    has, and every tool passes the namespace it is operating on so the spawn targets it.
    """
    global _MEM
    _maybe_auto_spawn(namespace)
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
    written = _mem(namespace).add(namespace, text)
    n = len(written)
    return f"wrote {n} fact{'s' if n != 1 else ''}"


@mcp.tool()
def memory_search(namespace: str, query: str, k: int = 5) -> str:
    """Search a namespace's memory and return the top-k facts as a bulleted list."""
    hits = _mem(namespace).search(namespace, query, k=k)
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
    """Delete all memory for a namespace by removing its SQLite file. Irreversible.

    Refuses (returns an explanatory message, changing nothing) while a LIVE maintenance
    lease with a fresh heartbeat is held for the namespace (spec §7.3): a POSIX unlink
    cannot safely interrupt an in-flight maintenance run — the run's open handle would
    keep committing to the unlinked (ghost) inode, silently losing that work. So clear
    waits for the run to finish or its lease to go stale. A stale or absent lease clears
    normally; the maintenance runner itself independently skips a namespace cleared
    mid-run at its next batch boundary.

    Residual race (spec §7.3, known limitation): a clear that lands in the sliver
    BETWEEN this lease-check and the unlink is not prevented — full cross-process file
    locking is deliberately out of scope for v1. The two guards (this refusal + the
    runner's batch-boundary skip) shrink the window; they do not close it.
    """
    path = _namespace_path(namespace)
    # No file ⇒ nothing to clear and no lease possible. Opening a store here would
    # CREATE the file, so short-circuit (unlink below is a no-op on missing files).
    if path.exists():
        # Cheap read: open a dedicated maintenance store on the file and ask the runner's
        # own staleness rule whether a live lease is held (no raw SQL here — stdout must
        # stay the JSON-RPC channel, and the store/runner own the query).
        lease_store = _mem()._maintenance_store(namespace)
        try:
            if live_lease_is_fresh(lease_store, namespace, now_ms()):
                return (
                    f"refused: namespace '{namespace}' has a live maintenance run "
                    "holding it; clear again once maintenance finishes."
                )
        finally:
            lease_store.close()

    # Release any cached open connection so the file handle is freed before unlink.
    store = _mem()._stores.pop(namespace, None)
    if store is not None:
        store.close()
    for p in (path, path.with_suffix(".db-wal"), path.with_suffix(".db-shm")):
        p.unlink(missing_ok=True)
    return f"cleared namespace '{namespace}'"


# ── sleep-time maintenance tools (design spec §6.3) ──────────────────────────
# Identical tool names/signatures to the two console surfaces (observe_mcp.py,
# routes/mcp.py). memory_maintenance_run is DRY-RUN by default, symmetric with the
# CLI. memory_maintenance_status is provably model-free (it never calls _mem()).


@mcp.tool()
def memory_maintenance_run(namespace: str, apply: bool = False) -> dict[str, Any]:
    """Run one sleep-time maintenance pass on a namespace (§6.3).

    DRY-RUN by default (apply=False): computes the full would-do report with ZERO
    writes — no ledger row, no proposals. apply=True claims the lease, runs the
    provably-safe auto band (exact-dup retirement + auto-band eviction) AND stages the
    judgment-call proposals for human review. Symmetric with `lean-memory-maintain`.
    NOTE the asymmetry with the LM_MAINT_AUTO auto-spawn path: that fires
    `--apply --auto-only` (auto band only, never stages proposals), so unattended
    runs cannot grow the review queue — only interactive apply=True stages.

    Returns the run summary: mode, staged/merged/demoted counts, and threshold stats.
    """
    report = _mem(namespace).maintain(namespace, apply=apply, trigger="mcp")
    return _report_to_dict(namespace, report, apply=apply, auto_only=False)


@mcp.tool()
def memory_maintenance_status(namespace: str) -> dict[str, Any]:
    """Report a namespace's maintenance ledger — runs + pending proposals (§6.3).

    MODEL-FREE by contract: this reads the namespace DB directly and NEVER builds the
    embedder/reranker (it does not call _mem()). Answering "when did maintenance last
    run?" must never trigger the ~2 GB first-run model download.
    """
    return mcp_support.read_status(_data_root(), namespace)


@mcp.tool()
def memory_review_queue(
    namespace: str, kind: Optional[str] = None, limit: int = 20
) -> str:
    """List pending maintenance proposals, grouped by entity, with evidence (§6.3).

    Each group carries the subject entity and its proposals; each proposal includes its
    parsed `payload` (the evidence) so a reviewer sees what would change. `kind` filters
    to one of 'dedup_near' | 'summarize' | 'evict'. Overdue proposals lazily expire.
    Returns a JSON string (the grouped list).
    """
    groups = _mem(namespace).review_queue(namespace, kind=kind, limit=limit)
    return json.dumps(groups, sort_keys=True, default=str)


@mcp.tool()
def memory_review_decide(
    namespace: str,
    proposal_id: str,
    decision: str,
    edited_text: Optional[str] = None,
) -> str:
    """Decide a maintenance proposal: approve | reject | edit | promote (§6.3).

    approve applies the proposal's verbs at decide-time (with apply-time target
    re-validation); reject leaves the spine byte-identical; edit (summarize only)
    approves the human-edited text; promote (evict only) rejects the eviction and
    lifts the fact back to the hot tier. Returns a JSON result string.
    """
    result = _mem(namespace).decide(
        proposal_id, decision, namespace=namespace,
        edited_text=edited_text, decided_by="mcp",
    )
    return json.dumps(result, sort_keys=True, default=str)


def main() -> None:
    """Console-script / module entry point: serve over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
