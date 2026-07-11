"""The default real embedder must be an UNGATED HF repo (launch gate item 1).

google/embeddinggemma-300m requires a license accept and breaks the canonical
`pip install 'lean-memory[mcp,models]'` first run. Construction is lazy — this
test never loads a model and stays offline.
"""
from lean_memory.embed.sentence_transformer import SentenceTransformerEmbedder


def test_default_is_ungated_qwen3():
    e = SentenceTransformerEmbedder()
    assert e.model_name == "Qwen/Qwen3-Embedding-0.6B"
    assert e.dim == 1024
