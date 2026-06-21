"""The Retriever — composes the Phase 0 retrieval pipeline (spec section 6):

  dense (two-stage Matryoshka) + sparse (BM25)
    → RRF fuse (k=10)
    → over-retrieve top 30-50
    → mandatory cross-encoder rerank
    → salience-decay re-score
    → temporal filter (is_latest by default; as_of=T → interval predicate)

All local, reproducible, no cloud key, no LLM at rank time.
"""

from __future__ import annotations

import math
from typing import Optional

from ..embed.base import Embedder, matryoshka_truncate
from ..store.base import Store
from ..types import RetrievedFact, now_ms
from .rerank import Reranker

# spec defaults
RRF_K = 10
OVER_RETRIEVE = 40  # top 30-50 fused candidates before rerank
W_REL, W_REC, W_IMP = 0.6, 0.2, 0.2
DECAY_LAMBDA = 1.0 / (1000 * 60 * 60 * 24 * 30)  # ~1/month in ms; recency=exp(-λ·age)


class Retriever:
    def __init__(self, store: Store, embedder: Embedder, reranker: Reranker) -> None:
        self.store = store
        self.embedder = embedder
        self.reranker = reranker

    def retrieve(
        self,
        query: str,
        k: int = 5,
        *,
        as_of: Optional[int] = None,
        is_latest_only: bool = True,
        now: Optional[int] = None,
    ) -> list[RetrievedFact]:
        now = now if now is not None else now_ms()

        # 1. embed query (full + coarse for the two-stage dense arm)
        q_full = self.embedder.embed_one(query)
        q_coarse = matryoshka_truncate(q_full, self.embedder.coarse_dim)

        # 2+3. dense and sparse arms, each over-retrieved
        dense = self.store.dense_search(
            q_coarse, q_full, OVER_RETRIEVE,
            is_latest_only=is_latest_only, as_of=as_of,
        )
        sparse = self.store.sparse_search(query, OVER_RETRIEVE, is_latest_only=is_latest_only)

        # 4. RRF fuse (k=10). score(d) = Σ 1/(k + rank_i(d)), rank 1-based.
        ranks: dict[str, dict[str, int]] = {}
        for rank, (fid, _) in enumerate(dense, start=1):
            ranks.setdefault(fid, {})["dense"] = rank
        for rank, (fid, _) in enumerate(sparse, start=1):
            ranks.setdefault(fid, {})["sparse"] = rank

        fused: list[tuple[str, float]] = []
        for fid, r in ranks.items():
            s = 0.0
            if "dense" in r:
                s += 1.0 / (RRF_K + r["dense"])
            if "sparse" in r:
                s += 1.0 / (RRF_K + r["sparse"])
            fused.append((fid, s))
        fused.sort(key=lambda x: x[1], reverse=True)
        fused = fused[:OVER_RETRIEVE]
        if not fused:
            return []

        # hydrate the fused candidates
        fact_map = self.store.hydrate([fid for fid, _ in fused])

        # 5. mandatory rerank over the fused candidate texts
        cand_ids = [fid for fid, _ in fused if fid in fact_map]
        cand_texts = [fact_map[fid].fact_text for fid in cand_ids]
        rel_scores = self.reranker.score(query, cand_texts)

        # 6. salience-decay re-score: final = w_rel·rel + w_rec·recency + w_imp·importance
        rel_norm = _minmax(rel_scores)
        rrf_lookup = dict(fused)
        dense_lookup = {fid: i + 1 for i, (fid, _) in enumerate(dense)}
        sparse_lookup = {fid: i + 1 for i, (fid, _) in enumerate(sparse)}

        results: list[RetrievedFact] = []
        for fid, rel, rel_n in zip(cand_ids, rel_scores, rel_norm):
            fact = fact_map[fid]
            age = max(0, now - (fact.last_access or fact.valid_at))
            recency = math.exp(-DECAY_LAMBDA * age)
            importance = fact.salience / 10.0
            final = W_REL * rel_n + W_REC * recency + W_IMP * importance
            results.append(
                RetrievedFact(
                    fact=fact, final_score=final, relevance=rel,
                    recency=recency, importance=importance,
                    dense_rank=dense_lookup.get(fid),
                    sparse_rank=sparse_lookup.get(fid),
                    rrf_score=rrf_lookup.get(fid),
                )
            )

        results.sort(key=lambda r: r.final_score, reverse=True)
        top = results[:k]
        for r in top:
            self.store.touch(r.fact.id, now)
        return top


def _minmax(xs: list[float]) -> list[float]:
    """Scale scores to [0,1] so they combine sanely with recency/importance."""
    if not xs:
        return []
    lo, hi = min(xs), max(xs)
    if hi - lo < 1e-9:
        return [1.0 for _ in xs]
    return [(x - lo) / (hi - lo) for x in xs]
