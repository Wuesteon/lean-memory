"""Tiny quality smoke harness — the seed of the Phase 2 eval (spec section 9).

Run with the OFFLINE defaults (FakeEmbedder/IdentityReranker) it only checks the
*plumbing* (does the relevant fact come back at all). Run with --real it loads
EmbeddingGemma + Ettin-32M and measures whether ranking actually puts the gold fact
on top — i.e. the first taste of the BET 1 "reranker is the accuracy lever" claim.

This is NOT LongMemEval/LoCoMo — it's a 10-line sanity check so plugging real models
is a one-command verification, not a leap of faith.

    python bench/smoke_quality.py            # offline plumbing check
    python bench/smoke_quality.py --real     # needs: pip install 'lean-memory[models]'
    python bench/smoke_quality.py --real --embedder Qwen/Qwen3-Embedding-0.6B
"""

from __future__ import annotations

import argparse
import sys
import tempfile

# tiny corpus of (text) facts + (query, gold-substring) probes
CORPUS = [
    "The user works at Globex Corporation.",
    "The user enjoys dark roast coffee every morning.",
    "The user lives in Berlin, Germany.",
    "The user is allergic to peanuts.",
    "The user drives a blue Toyota.",
]
PROBES = [
    ("where is the user employed?", "Globex"),
    ("what beverage does the user like?", "coffee"),
    ("which city does the user live in?", "Berlin"),
    ("what food allergy does the user have?", "peanut"),
    ("what car does the user own?", "Toyota"),
]


def build_memory(real: bool, embedder_name: str):
    from lean_memory import Memory

    if not real:
        return Memory(root=tempfile.mkdtemp())

    from lean_memory.embed.sentence_transformer import SentenceTransformerEmbedder
    from lean_memory.retrieve.rerank import CrossEncoderReranker

    return Memory(
        root=tempfile.mkdtemp(),
        embedder=SentenceTransformerEmbedder(embedder_name),
        reranker=CrossEncoderReranker(),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="use real EmbeddingGemma + Ettin-32M")
    ap.add_argument("--embedder", default="google/embeddinggemma-300m")
    args = ap.parse_args()

    mem = build_memory(args.real, args.embedder)
    ns = "bench"
    for i, text in enumerate(CORPUS):
        # rules extractor only fires on known predicates; bench needs every line stored,
        # so we bypass extraction by writing one fact per line via the low-level path.
        _store_raw_fact(mem, ns, text, t_ref=1_700_000_000_000 + i)

    top1 = 0
    for query, gold in PROBES:
        hits = mem.search(ns, query, k=3)
        got = hits[0].fact.fact_text if hits else "(none)"
        ok = bool(hits) and gold.lower() in hits[0].fact.fact_text.lower()
        top1 += ok
        print(f"[{'OK ' if ok else 'MISS'}] q={query!r}\n        top1={got!r}")
    print(f"\nTop-1 accuracy: {top1}/{len(PROBES)}"
          f"  ({'real models' if args.real else 'offline FakeEmbedder — plumbing only'})")
    mem.close()
    return 0


def _store_raw_fact(mem, namespace: str, text: str, t_ref: int) -> None:
    """Write one fact per sentence directly, skipping the predicate-gated rules extractor
    (the bench wants full control over the corpus)."""
    from lean_memory.types import Entity, Episode, Fact, new_id, now_ms

    store = mem._store(namespace)
    ep = Episode(namespace=namespace, raw=text, t_ref=t_ref)
    store.add_episode(ep)
    subj = store.upsert_entity(Entity(namespace=namespace, name="user", type="person"))
    f = Fact(
        namespace=namespace, subject_id=subj.id, predicate="about",
        fact_text=text, valid_at=t_ref, episode_id=ep.id, id=new_id(),
        ingested_at=now_ms(), created_at=now_ms(),
    )
    full, coarse = mem.embedder.embed_with_coarse(text)
    store.add_fact(f, full, coarse)


if __name__ == "__main__":
    sys.exit(main())
