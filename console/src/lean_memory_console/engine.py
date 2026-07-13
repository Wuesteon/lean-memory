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

from .config import ConsoleConfig, is_reserved_namespace, ns_db_path
from .events import EventLog

T = TypeVar("T")


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
        # The default Memory constructor is fully offline (stub backends), so
        # both models="stub" and models="auto" build the offline engine here;
        # real-model wiring is a Task 9 boot concern, out of the gateway.
        self._memory = Memory(root=config.data_root)
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

    def close(self) -> None:
        # Close the engine on the same dedicated worker that opened its store
        # connections (sqlite3 forbids cross-thread use), then retire the pool.
        # Synchronous by contract — callers close without an event loop.
        self._pool.submit(self._memory.close).result()
        self._pool.shutdown(wait=True)
