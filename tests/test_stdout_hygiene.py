"""Model lazy-loads must never write to stdout — it is the MCP stdio protocol channel.

Regression for the v0.1.2 launch blocker: gliner2's GLiNER2.from_pretrained prints a
model-configuration banner with bare print() calls. On the canonical
`[mcp,models,extract]` install the first memory_add tool call lazy-loads the model,
interleaving that banner with the JSON-RPC response on fd 1 and breaking the MCP
client's line-oriented parser. The same class of bug can appear in any heavy backend,
so every lazy loader routes load-time chatter to stderr.

Fakes are injected via sys.modules so the suite stays offline (no torch import,
no downloads) while exercising the real _ensure* code paths.
"""

import sys
import types

from lean_memory.extract.gliner_extractor import Gliner2Generator


def _fake_gliner2_module() -> types.ModuleType:
    mod = types.ModuleType("gliner2")

    class GLiNER2:
        @classmethod
        def from_pretrained(cls, name, **kw):
            print("=" * 60)
            print("Model Configuration")  # mimics gliner2/model.py's stdout banner
            return cls()

    mod.GLiNER2 = GLiNER2
    return mod


def _fake_sentence_transformers_module() -> types.ModuleType:
    mod = types.ModuleType("sentence_transformers")

    class SentenceTransformer:
        def __init__(self, name, device=None):
            print(f"loading {name}")  # load-time chatter must not reach stdout

        def get_sentence_embedding_dimension(self):
            return 1024

    class CrossEncoder:
        def __init__(self, name):
            print(f"loading {name}")

    mod.SentenceTransformer = SentenceTransformer
    mod.CrossEncoder = CrossEncoder
    return mod


def test_gliner_lazy_load_keeps_stdout_clean(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "gliner2", _fake_gliner2_module())
    gen = Gliner2Generator()
    model = gen._ensure_model()
    assert model is not None
    out, err = capsys.readouterr()
    assert out == "", f"model load leaked to stdout: {out!r}"
    assert "Model Configuration" in err  # chatter still visible, on stderr


def test_embedder_lazy_load_keeps_stdout_clean(monkeypatch, capsys):
    from lean_memory.embed.sentence_transformer import SentenceTransformerEmbedder

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", _fake_sentence_transformers_module()
    )
    e = SentenceTransformerEmbedder()
    e._ensure()
    assert e.dim == 1024
    out, _ = capsys.readouterr()
    assert out == "", f"model load leaked to stdout: {out!r}"


def test_reranker_lazy_load_keeps_stdout_clean(monkeypatch, capsys):
    from lean_memory.retrieve.rerank import CrossEncoderReranker

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", _fake_sentence_transformers_module()
    )
    r = CrossEncoderReranker()
    r._ensure()
    out, _ = capsys.readouterr()
    assert out == "", f"model load leaked to stdout: {out!r}"
