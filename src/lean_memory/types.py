"""Core data types for lean-memory.

These mirror the design-spec data model (episode / entity / fact). Phase 0 uses
the fact layer with the monotemporal spine; the bi-temporal audit columns exist
but are only populated when the `audit` extra is enabled (deferred past Phase 0).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


def now_ms() -> int:
    """Epoch milliseconds. Single source of wall-clock time so it can be faked in tests."""
    return int(time.time() * 1000)


def new_id() -> str:
    """uuidv7-ish: time-sortable id. stdlib has no uuid7, so we prefix a ms timestamp.

    Format: <48-bit ms hex>-<random>. Lexicographically sortable by creation time,
    which matches the spec's "uuidv7 (time-sortable)" intent for episode/fact ids.
    """
    return f"{now_ms():012x}-{uuid.uuid4().hex[:16]}"


@dataclass
class Episode:
    """Provenance layer: the verbatim ingested message/turn/JSON."""

    namespace: str
    raw: str
    t_ref: int  # reference (world) time for relative-date resolution, epoch ms
    source: str = "user"  # 'user'|'assistant'|'tool'|'doc'
    id: str = field(default_factory=new_id)
    created_at: int = field(default_factory=now_ms)


@dataclass
class Entity:
    """Canonical entity (person/org/place/...). Phase 0 resolves by (namespace, name, type)."""

    namespace: str
    name: str
    type: Optional[str] = None
    summary: Optional[str] = None
    resolved_id: Optional[str] = None  # canonical id if this row is an alias
    id: str = field(default_factory=new_id)
    created_at: int = field(default_factory=now_ms)


@dataclass
class Fact:
    """A typed, timestamped triple with surface text — the unit of memory & retrieval.

    The monotemporal spine (`valid_at`/`valid_to`/`superseded_by`/`is_latest`) is
    always populated. Bi-temporal audit columns default to mirroring ingest time
    and are only meaningfully diverged when the audit extra is on (Phase >0).
    """

    namespace: str
    subject_id: str
    predicate: str
    fact_text: str  # standalone sentence — what gets embedded + reranked
    valid_at: int  # world/event time the fact became true (epoch ms)
    episode_id: str

    object_id: Optional[str] = None  # null if object is a literal
    object_literal: Optional[str] = None

    valid_to: Optional[int] = None  # world time it stopped being true; None = still holds
    superseded_by: Optional[str] = None
    is_latest: int = 1

    # bi-temporal audit axis (opt-in)
    ingested_at: int = field(default_factory=now_ms)
    expired_at: Optional[int] = None
    invalidated_by: Optional[str] = None

    # scoring / governance
    confidence: float = 1.0
    salience: float = 0.0
    last_access: Optional[int] = None
    access_count: int = 0
    is_inference: int = 0
    tier: str = "hot"  # 'hot'|'cold'

    id: str = field(default_factory=new_id)
    created_at: int = field(default_factory=now_ms)


@dataclass
class RetrievedFact:
    """A fact returned from retrieval, with the scores that ranked it (for debugging/eval)."""

    fact: Fact
    final_score: float
    relevance: float  # reranker (or fused) score
    recency: float
    importance: float
    # provenance of the score, so the harness can ablate arms
    dense_rank: Optional[int] = None
    sparse_rank: Optional[int] = None
    rrf_score: Optional[float] = None
