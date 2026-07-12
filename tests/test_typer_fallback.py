"""Memory.add must survive an unreachable LLM typing backend.

The [llm] extra's OllamaTyper raises TyperError when the server is down or the
package is missing, and its docstring promises "the facade catches [it] to fall
back to StubTyper" — but Memory.add called the typer bare, so every add() with
an escalated candidate crashed for users whose Ollama wasn't running (the exact
default failure for the extra the launch copy promotes).
"""

from lean_memory import Memory
from lean_memory.extract.llm_typer import Typer, TyperError


class DownTyper(Typer):
    """Simulates OllamaTyper with the server unreachable."""

    def __init__(self):
        self.calls = 0

    def type_candidates(self, episode_text, candidates, known_entities=None):
        self.calls += 1
        raise TyperError("cannot reach Ollama (is the server running?)")


# An inference cue ("so") escalates this sentence's candidate to the typer.
_ESCALATING_TEXT = "I moved, so I live in Berlin now."


def test_add_falls_back_to_stub_when_typer_unreachable(tmp_path, capsys):
    typer = DownTyper()
    mem = Memory(root=tmp_path, typer=typer)
    ids = mem.add("ns", _ESCALATING_TEXT, t_ref=0)
    assert typer.calls == 1  # the escalated batch really reached the typer
    assert ids, "escalated facts must still be written via the stub fallback"
    err = capsys.readouterr().err
    assert "falling back" in err.lower()
    mem.close()


def test_fallback_facts_are_searchable(tmp_path):
    mem = Memory(root=tmp_path, typer=DownTyper())
    mem.add("ns", _ESCALATING_TEXT, t_ref=0)
    hits = mem.search("ns", "where does the user live?", k=3)
    assert any("Berlin" in h.fact.fact_text for h in hits)
    mem.close()
