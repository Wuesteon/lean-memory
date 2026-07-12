"""The default real embedder + reranker must be UNGATED HF repos (launch gate).

google/embeddinggemma-300m requires a license accept and breaks the canonical
`pip install 'lean-memory[mcp,models,extract]'` first run. Both the embedder and
the reranker sit on that same first-run path, so both defaults are pinned here.
Construction is lazy — these tests never load a model and stay offline.
"""
from lean_memory.embed.sentence_transformer import SentenceTransformerEmbedder
from lean_memory.retrieve.rerank import CrossEncoderReranker


def test_default_is_ungated_qwen3():
    e = SentenceTransformerEmbedder()
    assert e.model_name == "Qwen/Qwen3-Embedding-0.6B"
    assert e.dim == 1024


def test_reranker_default_is_ungated_ettin():
    # Mirrors the embedder guard: the default reranker must stay an UNGATED repo
    # so the canonical first-run download never hits a license-accept wall.
    # Construction is lazy (the CrossEncoder loads only on first .score()), so
    # this stays offline — verify laziness, then assert the pinned default.
    r = CrossEncoderReranker()
    assert r._model is None  # lazy: no model loaded at construction
    assert r.model_name == "cross-encoder/ettin-reranker-32m-v1"
