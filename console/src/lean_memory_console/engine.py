"""EngineGateway — the console's write path over a single lean_memory.Memory.

Wraps every engine write in a bounded SQLITE_BUSY retry (the engine sets no
busy_timeout, §6), serializes per-namespace writes with an asyncio.Lock,
detects supersession by reading fact.superseded_by on the returned ids while
still holding the lock (§5), and records add/search events. Event-recording
never masks the operation's own result (§5 failure contract).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from lean_memory import Memory
from lean_memory.maintain import mcp_support
from lean_memory.maintain.cli import _report_to_dict as _maint_report_to_dict

from .config import ConsoleConfig, is_reserved_namespace, ns_db_path
from .events import EventLog

T = TypeVar("T")


def _maintenance_report_to_dict(namespace: str, report, *, apply: bool) -> dict:
    """The maintenance run summary, shaped identically to the core MCP surface.

    Reuses the core CLI's RunReport→dict projection so the console tool's return is
    byte-for-byte the same shape as core `memory_maintenance_run` (tool-name and
    return parity across all three surfaces, §6.3). auto_only is always False from the
    gateway path — the console never runs the auto-only preview mode."""
    return _maint_report_to_dict(namespace, report, apply=apply, auto_only=False)


def _build_memory(config: ConsoleConfig) -> Memory:
    """Build the console's Memory, opportunistically upgrading to real backends.

    Mirror of the core engine's own wiring in
    ``lean_memory.mcp_server._build_memory`` (src/lean_memory/mcp_server.py,
    ~lines 34-82); kept as a mirror (not a private-symbol import) exactly as the
    namespace sanitizer is mirrored in config.py. Two INDEPENDENT upgrades, each
    guarded by its own import so either can succeed without the other:
      * ``models``  (sentence_transformers) → real embedder + reranker.
      * ``extract`` (gliner2)               → Gliner2Generator for extraction.
    Anything not installed falls back to lean-memory's deterministic offline
    stubs, so the console always runs.

    ``config.models`` gates the whole upgrade:
      * ``"stub"`` → force the deterministic offline stubs (tests/CI, and the
        console's own default behavior on a stub-only install).
      * ``"auto"`` → upgrade each backend whose optional extra is importable.
    """
    if config.models == "stub":
        return Memory(root=config.data_root)

    kwargs: dict = {}

    try:
        import sentence_transformers  # noqa: F401

        from lean_memory.embed.sentence_transformer import (
            SentenceTransformerEmbedder,
        )
        from lean_memory.retrieve.rerank import CrossEncoderReranker

        kwargs["embedder"] = SentenceTransformerEmbedder()
        kwargs["reranker"] = CrossEncoderReranker()
    except ImportError:
        # `models` extra not installed — keep the deterministic stub backends.
        pass

    try:
        import gliner2  # noqa: F401

        from lean_memory.extract.gliner_extractor import Gliner2Generator

        kwargs["generator"] = Gliner2Generator()
    except ImportError:
        # `extract` extra not installed — keep the stub candidate generator.
        pass

    return Memory(root=config.data_root, **kwargs)


def resolved_models_mode(config: ConsoleConfig) -> str:
    """Report whether the built Memory uses real or stub retrieval backends.

    "stub" is forced when config.models == "stub"; otherwise "real" only when
    the [models] extra (sentence_transformers) is importable — matching the
    embedder/reranker upgrade in _build_memory. This is the resolved value the
    whoami view surfaces so the UI can warn that semantic scores are stubbed.
    """
    if config.models == "stub":
        return "stub"
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return "stub"
    return "real"


@dataclass
class AddResult:
    fact_ids: list = field(default_factory=list)
    superseded_fact_ids: list = field(default_factory=list)
    superseded_count: int = 0
    duration_ms: float = 0.0


@dataclass
class SearchResult:
    hits: list = field(default_factory=list)
    duration_ms: float = 0.0


def retry_busy(fn: Callable[[], T], attempts: int = 3) -> T:
    """Call fn, retrying on SQLITE_BUSY / 'database is locked'.

    Catches sqlite3.OperationalError whose message contains 'locked' or
    'busy'; sleeps 0.05 * 2**i between attempts; re-raises after the last.
    Any other error propagates immediately.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last = exc
            if i < attempts - 1:
                time.sleep(0.05 * (2 ** i))
    assert last is not None
    raise last


class EngineGateway:
    def __init__(self, config: ConsoleConfig, event_log: EventLog) -> None:
        self._config = config
        self._events = event_log
        # models="stub" forces the deterministic offline backends; models="auto"
        # opportunistically upgrades to the real embedder/reranker (and GLiNER2
        # extractor) when the optional extras are importable — see _build_memory.
        self._memory = _build_memory(config)
        self._locks: dict[str, asyncio.Lock] = {}
        # A single dedicated worker thread owns every engine call. The engine's
        # per-namespace SqliteStore connections are opened with the sqlite3
        # default check_same_thread=True, so the thread that first touches a
        # namespace (via add/search) must be the same thread that later closes
        # it. asyncio.to_thread's shared pool rotates workers and never lands on
        # the main thread, so store connections opened on a pool worker cannot be
        # closed from a synchronous main-thread close() — pinning to one worker
        # keeps create/use/close thread-consistent.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lm-engine")

    async def _run(self, fn: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn)

    def _lock(self, namespace: str) -> asyncio.Lock:
        if namespace not in self._locks:
            self._locks[namespace] = asyncio.Lock()
        return self._locks[namespace]

    def _detect_superseded(self, namespace: str, fact_ids: list) -> list:
        """SELECT id FROM fact WHERE superseded_by IN (<my returned ids>).

        Read-only connection to the namespace DB; scoped to my add's ids so a
        concurrent writer's supersessions cannot leak in (§5). Never raises —
        a read failure degrades supersession reporting to empty, not an error.
        """
        if not fact_ids:
            return []
        path = ns_db_path(self._config.data_root, namespace)
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            return []
        try:
            con.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(fact_ids))
            rows = con.execute(
                f"SELECT id FROM fact WHERE superseded_by IN ({placeholders})",
                list(fact_ids),
            ).fetchall()
            return [r["id"] for r in rows]
        except sqlite3.Error:
            return []
        finally:
            con.close()

    async def add(
        self,
        namespace: str,
        text: str,
        source: str = "user",
        t_ref: int | None = None,
    ) -> AddResult:
        if is_reserved_namespace(namespace):
            raise ValueError(f"reserved namespace rejected: {namespace!r}")
        start = time.perf_counter()
        async with self._lock(namespace):
            fact_ids = await self._run(
                lambda: retry_busy(
                    lambda: self._memory.add(
                        namespace, text, t_ref=t_ref, source=source
                    )
                )
            )
            superseded = self._detect_superseded(namespace, fact_ids)
        duration_ms = (time.perf_counter() - start) * 1000.0
        result = AddResult(
            fact_ids=list(fact_ids),
            superseded_fact_ids=superseded,
            superseded_count=len(superseded),
            duration_ms=duration_ms,
        )
        self._events.record(
            namespace,
            "add",
            duration_ms,
            {
                "episode_text_chars": len(text),
                "source": source,
                "t_ref": t_ref,
                "fact_ids": result.fact_ids,
                "fact_count": len(result.fact_ids),
                "superseded_fact_ids": result.superseded_fact_ids,
                "superseded_count": result.superseded_count,
            },
        )
        return result

    async def search(
        self,
        namespace: str,
        query: str,
        k: int = 5,
        latest_only: bool = True,
        origin: str = "agent",
    ) -> SearchResult:
        start = time.perf_counter()
        retrieved = await self._run(
            lambda: retry_busy(
                lambda: self._memory.search(
                    namespace, query, k=k, is_latest_only=latest_only
                )
            )
        )
        duration_ms = (time.perf_counter() - start) * 1000.0
        hits = [
            {
                "fact_id": rf.fact.id,
                "fact_text": rf.fact.fact_text,
                "final_score": rf.final_score,
                "relevance": rf.relevance,
                "recency": rf.recency,
                "importance": rf.importance,
                "dense_rank": rf.dense_rank,
                "sparse_rank": rf.sparse_rank,
                "rrf_score": rf.rrf_score,
            }
            for rf in retrieved
        ]
        self._events.record(
            namespace,
            "search",
            duration_ms,
            {
                "query": query,
                "k": k,
                "latest_only": latest_only,
                "origin": origin,
                "hits": hits,
            },
        )
        return SearchResult(hits=hits, duration_ms=duration_ms)

    # ── sleep-time maintenance (design spec §8, §6.3) ──────────────────────────
    # Four public methods mirroring add/search EXACTLY: retry_busy inside the single
    # worker thread, under the per-namespace asyncio lock. Both console MCP surfaces
    # (observe_mcp.py stdio + routes/mcp.py HTTP) reach the engine ONLY through these.
    async def maintain(
        self, namespace: str, *, apply: bool = False
    ) -> dict:
        """Run one maintenance pass on `namespace` (§6.3). DRY-RUN by default.

        Lock consequence (§8): a console-invoked maintain() holds this namespace's
        gateway lock for the whole run, so it blocks live add/search on that namespace
        until the run returns. The underlying MaintenanceRunner works in SHORT per-batch
        commits (it releases the SQLite write lock between batches), so this is
        tolerable without any chunking logic here — we keep the wrapper simple and let
        the runner's own batch cadence bound the stall. Returns the run summary dict.
        """
        if is_reserved_namespace(namespace):
            raise ValueError(f"reserved namespace rejected: {namespace!r}")
        async with self._lock(namespace):
            report = await self._run(
                lambda: retry_busy(
                    lambda: self._memory.maintain(
                        namespace, apply=apply, trigger="console"
                    )
                )
            )
        return _maintenance_report_to_dict(namespace, report, apply=apply)

    async def maintenance_status(self, namespace: str) -> dict:
        """The namespace's maintenance ledger — runs + pending proposals (§6.3).

        A pure ledger read via the model-free ``mcp_support.read_status`` against this
        gateway's data root, shaped IDENTICALLY to the core server's status tool. No
        lock and no worker thread: it opens its own read-only connection and never
        touches the serving store, so it cannot contend with a live add/search."""
        return await self._run(
            lambda: retry_busy(
                lambda: mcp_support.read_status(
                    self._config.data_root, namespace
                )
            )
        )

    async def review_queue(
        self, namespace: str, *, kind: str | None = None, limit: int = 20
    ) -> list:
        """Pending proposals grouped by entity, with evidence (§6.3). Read-shaped but
        routed through the write worker + lock because review_queue lazily EXPIRES
        overdue proposals (a write) — it must not race a concurrent add on the file."""
        async with self._lock(namespace):
            return await self._run(
                lambda: retry_busy(
                    lambda: self._memory.review_queue(
                        namespace, kind=kind, limit=limit
                    )
                )
            )

    async def decide(
        self,
        namespace: str,
        proposal_id: str,
        decision: str,
        *,
        edited_text: str | None = None,
    ) -> dict:
        """Decide a proposal: approve | reject | edit | promote (§6.3). Applies on
        approve through the lifecycle, all under the per-namespace lock + retry_busy."""
        async with self._lock(namespace):
            return await self._run(
                lambda: retry_busy(
                    lambda: self._memory.decide(
                        proposal_id, decision, namespace=namespace,
                        edited_text=edited_text, decided_by="console",
                    )
                )
            )

    async def promote(self, namespace: str, fact_id: str) -> dict:
        """Explicitly promote a fact back to the hot tier (§4.4). Explicit-only —
        there is no automatic promotion anywhere. Lock + retry_busy like the rest."""
        async with self._lock(namespace):
            return await self._run(
                lambda: retry_busy(
                    lambda: self._memory.promote(fact_id, namespace=namespace)
                )
            )

    def close(self) -> None:
        # Close the engine on the same dedicated worker that opened its store
        # connections (sqlite3 forbids cross-thread use), then retire the pool.
        # Synchronous by contract — callers close without an event loop.
        self._pool.submit(self._memory.close).result()
        self._pool.shutdown(wait=True)
