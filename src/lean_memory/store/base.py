"""The `Store` interface — the single abstraction every other component talks to.

Two concrete implementations are planned (SqliteStore = default; LanceStore =
scale tier). Phase 0 ships SqliteStore only. Per BET 4, each namespace is its own
backing file (per-tenant isolation), so the interface is opened per-namespace.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Sequence

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
        self, query_text: str, k: int, *, is_latest_only: bool = True
    ) -> list[tuple[str, float]]:
        """BM25 lexical search. Returns [(fact_id, score)] best-first."""

    @abstractmethod
    def hydrate(self, fact_ids: Sequence[str]) -> dict[str, Fact]:
        """Bulk-load Fact rows by id (preserves caller's dedup needs)."""

    @abstractmethod
    def touch(self, fact_id: str, when_ms: int) -> None:
        """Record an access (recency/decay bookkeeping)."""

    # ── lifecycle ──
    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
