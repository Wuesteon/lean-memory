"""Real local embedder, lazily loaded. Default model = EmbeddingGemma-300m (int8-friendly).

Per BET 1 (verified 2026-06): the production default is a *harness-decided* choice
between EmbeddingGemma-300m and Qwen3-Embedding-0.6B — EmbeddingGemma for
multilingual/cross-lingual, Qwen3-0.6B for English-only retrieval quality (it beats
EmbeddingGemma on MTEB Retrieval, 64.65 vs 62.49). Both load through this one class;
the bench harness picks the winner. Requires the `models` extra (sentence-transformers).
"""

from __future__ import annotations

import numpy as np

from .base import Embedder

# Known-good local embedders and their full dims. The bench harness compares these.
KNOWN_MODELS = {
    "google/embeddinggemma-300m": 768,
    "Qwen/Qwen3-Embedding-0.6B": 1024,
    "BAAI/bge-small-en-v1.5": 384,  # speed tier only (per spec, NOT the default)
}


class SentenceTransformerEmbedder(Embedder):
    """Lazy wrapper over sentence-transformers. Model is fetched on first embed()."""

    def __init__(
        self,
        model_name: str = "google/embeddinggemma-300m",
        coarse_dim: int = 256,
        device: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.coarse_dim = coarse_dim
        self._device = device
        self._model = None
        # dim is known up front for the schema; verified on first load.
        self.dim = KNOWN_MODELS.get(model_name, 768)

    def _ensure(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:  # pragma: no cover - install-path guidance
                raise ImportError(
                    "SentenceTransformerEmbedder needs the 'models' extra: "
                    "pip install 'lean-memory[models]'"
                ) from e
            self._model = SentenceTransformer(self.model_name, device=self._device)
            self.dim = self._model.get_sentence_embedding_dimension() or self.dim
        return self._model

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        model = self._ensure()
        vecs = model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vecs.astype(np.float32)
