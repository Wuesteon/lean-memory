"""Summarizer seam for SUMMARIZE (design spec §3.5, §4.3).

Offline-by-default discipline: the default summarizer is a deterministic extractive
stub (top-salience `fact_text`s, honestly labeled — NOT abstractive prose). `[llm]`
upgrades to an Ollama abstractive backend, import-guarded so this module imports
with no `ollama` installed and only fails on instantiation/use. Every proposal
records the backend that produced its evidence via `backend_id` — 'stub' or
'ollama:<model>' (§3.5).

`LM_FORCE_STUBS` is honored (as elsewhere in the codebase): `default_summarizer()`
returns the stub whenever it is set, even if `[llm]` is installed — the test suite
and CI never touch a model.
"""

from __future__ import annotations

import os
from typing import Optional, Protocol, Sequence, runtime_checkable

from ..types import Fact


@runtime_checkable
class Summarizer(Protocol):
    """Turns a cluster of source facts into one summary sentence (SUMMARIZE, §4.3).

    Implementations MUST be deterministic given a fixed backend (temperature 0 for
    a model) so a staged proposal is reproducible. `backend_id` labels the evidence
    backend for the proposal payload ('stub' | 'ollama:<model>').
    """

    #: Identifies the backend for `evidence_backend` on the staged proposal.
    backend_id: str

    def summarize(self, facts: Sequence[Fact]) -> str: ...


def _stable_top_salience(facts: Sequence[Fact]) -> list[Fact]:
    """Facts ordered by descending salience, tie-broken by ascending id (stable,
    deterministic regardless of input order)."""
    return sorted(facts, key=lambda f: (-f.salience, f.id))


class ExtractiveStubSummarizer:
    """Deterministic, dependency-free default — the offline SUMMARIZE backend.

    NOT abstractive: it selects the top-salience source `fact_text`s and joins them
    in a stable order behind an honest label, so a reviewer sees exactly which
    originals it stands for (no invented prose). Reproducible byte-for-byte given
    the same facts — pinned by test.
    """

    #: The label prefix — honest about being extractive, not model-written.
    LABEL = "Summary (extractive):"

    def __init__(self, max_facts: int = 5) -> None:
        #: Cap on how many source texts the extractive summary carries.
        self.max_facts = max_facts
        self.backend_id = "stub"

    def summarize(self, facts: Sequence[Fact]) -> str:
        ranked = _stable_top_salience(facts)[: self.max_facts]
        joined = " ".join(f.fact_text.strip() for f in ranked)
        return f"{self.LABEL} {joined}".rstrip()


class OllamaSummarizer:
    """Real abstractive backend behind the `[llm]` extra — import-guarded.

    Importing THIS module never requires `ollama` (mirrors OllamaTyper): the import
    lives inside `_client`, so instantiation of a bare-environment install fails with
    a clear, actionable error pointing at `lean-memory[llm]`, and the module itself
    stays import-clean for the offline suite.
    """

    DEFAULT_MODEL = "qwen2.5:3b"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        host: Optional[str] = None,
        temperature: float = 0.0,
    ) -> None:
        self.model = model
        self.host = host
        self.temperature = temperature
        self.backend_id = f"ollama:{model}"
        self._client_obj = None

    def _client(self):
        if self._client_obj is not None:
            return self._client_obj
        try:
            import ollama  # type: ignore
        except ImportError as e:  # optional dep absent → clear, actionable failure
            raise RuntimeError(
                "OllamaSummarizer needs the 'llm' extra: install with "
                "`pip install lean-memory[llm]`, or use ExtractiveStubSummarizer "
                "for offline summarization."
            ) from e
        self._client_obj = ollama.Client(host=self.host) if self.host else ollama
        return self._client_obj

    def summarize(self, facts: Sequence[Fact]) -> str:
        client = self._client()
        bullet = "\n".join(f"- {f.fact_text.strip()}" for f in _stable_top_salience(facts))
        prompt = (
            "Consolidate these memory facts about one subject into a single, concise "
            "factual summary sentence. Do not invent facts; only compress what is "
            "stated.\n\n" + bullet
        )
        resp = client.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": self.temperature},
        )
        msg = getattr(resp, "message", None)
        content = getattr(msg, "content", None) if msg is not None else None
        if content is None:
            try:
                content = resp["message"]["content"]  # type: ignore[index]
            except (TypeError, KeyError) as e:
                raise RuntimeError(f"unexpected Ollama response shape: {resp!r}") from e
        return content.strip()


def default_summarizer() -> Summarizer:
    """The summarizer to use when a caller supplies none.

    Honors `LM_FORCE_STUBS` (returns the extractive stub even if `[llm]` is
    installed) — the offline default. Today it always returns the stub; wiring the
    Ollama upgrade on `[llm]` is a v2 item (§9.2), but the seam is here.
    """
    if os.environ.get("LM_FORCE_STUBS"):
        return ExtractiveStubSummarizer()
    return ExtractiveStubSummarizer()
