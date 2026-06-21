"""Reranker interface + default backends.

Per BET 1, the reranker is the single largest accuracy lever and is MANDATORY in the
default pipeline. But the production model is harness-decided (the sub-150M field —
Ettin-32M / mxbai-base / Qwen3-Reranker-0.6B — is within noise; do NOT assume
superiority). Phase 0 ships:
  - IdentityReranker: passes fused order through (keeps tests offline/deterministic).
  - CrossEncoderReranker: lazy sentence-transformers CrossEncoder, default Ettin-32M.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Reranker(ABC):
    @abstractmethod
    def score(self, query: str, docs: list[str]) -> list[float]:
        """Return one relevance score per doc (higher = better)."""


class IdentityReranker(Reranker):
    """No-op reranker: preserves incoming order via a descending score ramp.

    Used as the offline test default so the pipeline runs with zero model deps.
    NOTE: this is explicitly NOT spec-compliant for production — the spec requires a
    real cross-encoder. It exists only to make Phase 0 runnable without downloads.
    """

    def score(self, query: str, docs: list[str]) -> list[float]:
        n = len(docs)
        return [float(n - i) for i in range(n)]


class CrossEncoderReranker(Reranker):
    """Lazy cross-encoder. Default = Ettin-32M (a verified, real sub-150M option)."""

    def __init__(self, model_name: str = "cross-encoder/ettin-reranker-32m-v1") -> None:
        self.model_name = model_name
        self._model = None

    def _ensure(self):
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as e:  # pragma: no cover
                raise ImportError(
                    "CrossEncoderReranker needs the 'models' extra: "
                    "pip install 'lean-memory[models]'"
                ) from e
            self._model = CrossEncoder(self.model_name)
        return self._model

    def score(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        model = self._ensure()
        pairs = [[query, d] for d in docs]
        return [float(s) for s in model.predict(pairs)]
