"""Deterministic hash-based embedder — the default test backend.

Why this exists: Phase 0 must be runnable offline with no model download and no GPU,
and the spec's reproducibility goal wants a byte-identical common path. FakeEmbedder
maps text → a fixed vector via a seeded hash, so the whole pipeline (ingest →
embed → store → retrieve → rerank) is testable in milliseconds with zero deps.

It is NOT semantically meaningful — it only guarantees identical text → identical
vector, and different text → different vector. Swap in SentenceTransformerEmbedder
for real retrieval quality.
"""

from __future__ import annotations

import hashlib

import numpy as np

from .base import Embedder


class FakeEmbedder(Embedder):
    """Seeded-hash embedder. Deterministic across processes and machines."""

    def __init__(self, dim: int = 768, coarse_dim: int = 256) -> None:
        self.dim = dim
        self.coarse_dim = coarse_dim

    def _vec(self, text: str) -> np.ndarray:
        # Seed a PRNG from a stable hash of the text → reproducible Gaussian vector.
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], "little")
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(self.dim).astype(np.float32)
        n = np.linalg.norm(v)
        return v / (n if n else 1.0)

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        return np.stack([self._vec(t) for t in texts])
