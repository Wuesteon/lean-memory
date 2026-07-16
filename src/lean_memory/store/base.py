"""The `Store` interface — the single abstraction every other component talks to.

Two concrete implementations are planned (SqliteStore = default; LanceStore =
scale tier). Phase 0 ships SqliteStore only. Per BET 4, each namespace is its own
backing file (per-tenant isolation), so the interface is opened per-namespace.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

import numpy as np

from ..types import Entity, Episode, Fact


class Store(ABC):
    """Storage + index abstraction. Implementations own one namespace's data."""

    # ── provenance ──
    @abstractmethod
    def add_episode(self, episode: Episode) -> None: ...

    # ── entities ──
    @abstractmethod
    def upsert_entity(self, entity: Entity) -> Entity:
        """Resolve-or-create. If an entity with the same (namespace, name, type)
        exists, return it; otherwise insert `entity` and return it."""

    @abstractmethod
    def get_entity(self, entity_id: str) -> Optional[Entity]: ...

    # ── facts ──
    @abstractmethod
    def add_fact(self, fact: Fact, embedding: np.ndarray, embedding_256: np.ndarray) -> None:
        """Insert a fact row + its vec0 vectors + its FTS row, in one transaction."""

    @abstractmethod
    def supersede_fact(self, old_fact_id: str, new_fact_id: str, valid_to: int) -> None:
        """ADD-only supersession: point old→new, set old.valid_to, flip old.is_latest=0.
        Never deletes."""

    @abstractmethod
    def get_fact(self, fact_id: str) -> Optional[Fact]: ...

    @abstractmethod
    def find_latest_in_slot(
        self, subject_id: str, predicate: str
    ) -> Sequence[Fact]:
        """All currently-latest facts in a (subject, predicate) slot — for contradiction
        detection / supersession lookup."""

    # ── retrieval primitives (the Retriever composes these) ──
    @abstractmethod
    def dense_search(
        self,
        query_256: np.ndarray,
        query_768: np.ndarray,
        k: int,
        *,
        is_latest_only: bool = True,
        as_of: Optional[int] = None,
    ) -> list[tuple[str, float]]:
        """Two-stage Matryoshka dense search. Returns [(fact_id, distance)] best-first."""

    @abstractmethod
    def sparse_search(
        self, query_text: str, k: int, *, is_latest_only: bool = True,
        as_of: Optional[int] = None,
    ) -> list[tuple[str, float]]:
        """BM25 lexical search. Returns [(fact_id, score)] best-first.
        as_of applies the same interval predicate as the dense arm."""

    @abstractmethod
    def hydrate(self, fact_ids: Sequence[str]) -> dict[str, Fact]:
        """Bulk-load Fact rows by id (preserves caller's dedup needs)."""

    @abstractmethod
    def touch(self, fact_id: str, when_ms: int) -> None:
        """Record an access (recency/decay bookkeeping)."""

    # ── maintenance mutation surface (sleep-time job; design spec §4.0) ──
    @abstractmethod
    @contextmanager
    def batch(self) -> Iterator[None]:
        """Unit-of-work: BEGIN IMMEDIATE, suspend per-call commits, one COMMIT at
        exit, ROLLBACK on exception. Model/embedding work is forbidden inside the
        window (§7.1) — the lock-hold span must contain only row writes."""
        ...

    @abstractmethod
    def retire_duplicate(self, loser_id: str, survivor_id: str) -> None:
        """Retire an exact duplicate: flip loser is_latest=0 + superseded_by=survivor
        on fact and fact_vec; valid_to UNTOUCHED (verb (c), as-of-safe). Maintains the
        chain invariant — every open retired duplicate points DIRECTLY at an is_latest=1
        survivor — by (i) resolving `survivor_id` to its live canonical and (ii)
        re-pointing existing open losers of `loser_id` at that survivor."""

    @abstractmethod
    def set_tier(self, fact_id: str, tier: str) -> None:
        """Move a fact between the hot/cold tiers — fact.tier + fact_vec.tier, one txn."""

    @abstractmethod
    def get_embedding(self, fact_id: str) -> Optional[np.ndarray]:
        """Read a fact's stored full-dim vector back (no re-embed). None if absent."""

    @abstractmethod
    def iter_latest_facts(self, after_id: Optional[str] = None) -> Iterator[Fact]:
        """Id high-water scan over is_latest=1 rows (evict/summarize candidates)."""

    @abstractmethod
    def iter_slots_touched_since(self, cursor_id: str) -> Iterator[tuple[str, str]]:
        """DISTINCT (subject_id, predicate) slots that GAINED a member (a fact with
        id > cursor) since the cursor — including duplicates landing on long-quiet
        slots (the verified cursor gap)."""

    # ── maintenance ledger + proposal CRUD (schema v2, design spec §4.0/§5) ──
    # Pure row CRUD — no decide/apply logic (that is the proposal lifecycle, a
    # later task). The create-half is needed by the maintenance runner.
    @abstractmethod
    def create_run(
        self, namespace: str, trigger: str, started_at: int, config_hash: Optional[str]
    ) -> str:
        """Claim the maintenance lease: INSERT a status='running' maintenance_run and
        return its id. The ux_run_live partial-unique index makes this the atomic
        lease claim — a second live run for the same namespace raises
        sqlite3.IntegrityError (§7.2)."""

    @abstractmethod
    def heartbeat_run(self, run_id: str, at: int) -> None:
        """Bump maintenance_run.heartbeat_at — proof the run is still alive (§7.2)."""

    @abstractmethod
    def finish_run(
        self,
        run_id: str,
        status: str,
        finished_at: int,
        stats_json: Optional[str],
        cursor_id: Optional[str],
    ) -> None:
        """Close a run: stamp status ('ok'|'aborted'), finished_at, stats_json,
        cursor_id. Clearing status='running' releases the ux_run_live lease."""

    @abstractmethod
    def get_live_run(self, namespace: str) -> Optional[dict]:
        """The status='running' run for a namespace, or None. (At most one exists —
        ux_run_live enforces it.)"""

    @abstractmethod
    def stage_proposal(
        self,
        run_id: str,
        namespace: str,
        kind: str,
        payload_json: str,
        created_at: int,
        expires_at: int,
        evidence_backend: Optional[str] = None,
    ) -> str:
        """INSERT a status='pending' maintenance_proposal and return its id."""

    @abstractmethod
    def get_proposal(self, proposal_id: str) -> Optional[dict]:
        """A single proposal row as a dict, or None."""

    @abstractmethod
    def list_proposals(
        self,
        namespace: str,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Proposals for a namespace, newest first, optionally filtered by status
        and/or kind."""

    # ── lifecycle ──
    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
