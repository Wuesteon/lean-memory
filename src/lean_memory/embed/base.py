"""Embedder interface + the Matryoshka truncation helper.

Default behaviour (per BET 1, corrected 2026-06): produce one full 768-dim vector,
plus a 256-dim Matryoshka truncation (256 is the verified retrieval-loss knee; 128
is a speed-only tier). Truncation is slice-then-L2-renormalize — pure, deterministic,
no second inference pass.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


def matryoshka_truncate(vec: np.ndarray, dim: int) -> np.ndarray:
    """Slice the first `dim` components and L2-renormalize. Deterministic, no inference.

    This is exactly the MRL recipe: the model was trained so that prefixes of the
    embedding are themselves valid (renormalized) embeddings.
    """
    head = vec[..., :dim].astype(np.float32)
    norm = np.linalg.norm(head, axis=-1, keepdims=True)
    norm = np.where(norm == 0.0, 1.0, norm)
    return head / norm


class Embedder(ABC):
    """Maps text → a full-dim L2-normalized float32 vector.

    The store quantizes to int8 for vec0; the embedder works in float32 so callers
    can derive Matryoshka truncations before quantization.
    """

    #: full embedding dimensionality (e.g. 768 for EmbeddingGemma, 1024 for Qwen3-0.6B)
    dim: int = 768
    #: coarse Matryoshka dim for the two-stage dense arm
    coarse_dim: int = 256

    @abstractmethod
    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an (N, dim) float32 array of L2-normalized embeddings."""

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]

    def embed_with_coarse(self, text: str) -> tuple[np.ndarray, np.ndarray]:
        """Return (full_dim_vec, coarse_dim_vec) for a single text — what add_fact wants."""
        full = self.embed_one(text)
        coarse = matryoshka_truncate(full, self.coarse_dim)
        return full, coarse
