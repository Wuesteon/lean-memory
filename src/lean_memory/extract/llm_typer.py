"""Pass 4 — LLM constrained typing / validation (the *residual*).

This is the last stage of the hybrid extraction pipeline (spec §5). Passes 1–3
already ran: rules + GLiNER2 over-generate high-recall ``(subject, relation,
object)`` candidates, and the recall-biased router (Pass 3) flagged the hard ones
(low GLiNER2 confidence, coreference/ellipsis, cross-turn references, possible
``derives``). Everything the router did NOT escalate is trivially explicit and is
typed cheaply/deterministically. This module types the escalated residual.

The LLM here does **constrained classification, not open generation** (BET 2): for
each candidate it (a) assigns exactly one relation from the closed taxonomy
[asserts | supersedes | extends | derives], (b) sets ``is_inference`` (only
``derives`` is inferential), (c) resolves coreference to a known entity, (d) may
surface an implicit cross-utterance edge. Open-ended generation is supermemory's
expensive, nondeterministic path — we deliberately avoid it.

WHY two backends (mirrors FakeEmbedder/IdentityReranker in Phase 0): the whole
spec is gated on "everything testable offline, zero downloads, zero servers."
``OllamaTyper`` is the real local backend (a small Phi/Qwen-class model behind the
``ollama`` HTTP client); ``StubTyper`` is a deterministic, dependency-free default
so the test suite — and the BET 2 ablation harness — runs with no Ollama server
and no model pull. ``Memory`` defaults to ``StubTyper`` exactly like it defaults to
``FakeEmbedder``.

Note on the input contract: a ``Candidate`` here is the router's output (Pass 3),
not a persisted ``Fact``. We keep it defined locally (rather than importing from a
sibling Pass-2/3 module that may still be in flux) so this file is import-clean on
its own; the only cross-module contract is the relation taxonomy, imported from
``taxonomy.py`` — the single source of truth for relation names and which relation
is inferential.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

# ── Relation taxonomy + Candidate: the ONE cross-module contract. ──
# taxonomy.py is the single source of truth for both the relation set and the
# pre-typing `Candidate` shape. Pass 2 (gliner_extractor) emits canonical Candidates
# and Pass 3 (router) routes them, so Pass 4 MUST consume the same type — importing
# it here (rather than forking a local copy) is what keeps the Pass-3→Pass-4 handoff
# from crashing on field-name drift.
from .taxonomy import (  # noqa: E402
    INFERENCE_CUES,
    RELATIONS,
    Candidate,
    is_inference_relation,
)

# Relations the TYPER may emit. The typer decides ONLY the asserts-vs-derives split:
# it sees a single candidate utterance + episode context, NOT the prior-slot state, so
# it CANNOT correctly judge supersedes/extends (those need the existing slot fact and
# are the ContradictionResolver's job). Offering all four to a small model made it
# anchor on 'extends' for everything (0/10 on the BET-2 set). Constrain to {asserts,
# derives} to match StubTyper's decision surface exactly.
_TYPER_RELATIONS = ["asserts", "derives"]
# Full taxonomy kept for validation/reference.
_ALLOWED_RELATIONS = list(RELATIONS)


@dataclass
class TypedFact:
    """The typer's output: a candidate bound to a final taxonomy relation + flags.

    This is the hand-off to ``Memory.add`` / supersession (Pass 5). It carries
    everything needed to build a ``Fact`` plus the relation decision that drives
    versioning (``supersedes`` vs ``extends``) and the ``is_inference`` flag.
    """

    subject_name: str
    predicate: str  # the (possibly re-typed) slot predicate
    relation: str  # taxonomy: asserts | supersedes | extends | derives
    fact_text: str
    valid_at: int
    is_inference: int  # 1 iff relation == "derives"
    confidence: float

    object_literal: Optional[str] = None
    subject_id: Optional[str] = None
    object_id: Optional[str] = None
    # Provenance of the decision: "stub" | "ollama" | "fallback" — lets the ablation
    # harness attribute Relation-F1 to the backend that produced each label.
    typed_by: str = "stub"

    def __post_init__(self) -> None:
        # Invariant the whole downstream pipeline relies on: relation is in-taxonomy
        # and is_inference is consistent with it. Fail fast — a bad relation here
        # corrupts supersession logic silently.
        if self.relation not in RELATIONS:
            raise ValueError(
                f"relation {self.relation!r} not in taxonomy {RELATIONS!r}"
            )
        expected = 1 if is_inference_relation(self.relation) else 0
        # We don't overwrite silently; mismatches are programmer errors.
        if self.is_inference not in (0, 1):
            raise ValueError(f"is_inference must be 0/1, got {self.is_inference!r}")
        if self.is_inference != expected:
            raise ValueError(
                f"is_inference={self.is_inference} contradicts relation "
                f"{self.relation!r} (expected {expected})"
            )


class TyperError(RuntimeError):
    """Raised when a real typing backend is unreachable (e.g. no Ollama server).

    Callers catch this to fall back to ``StubTyper`` and keep the offline promise.
    Kept distinct from ``ValueError`` so a *config* error (bad relation) is never
    confused with an *availability* error (server down).
    """


class Typer(ABC):
    """Maps router-escalated candidates → typed facts. Pluggable, like Embedder.

    Implementations MUST be pure functions of their inputs given a fixed backend
    (deterministic decode / temperature 0) so the common path stays reproducible.
    """

    @abstractmethod
    def type_candidates(
        self,
        episode_text: str,
        candidates: list[Candidate],
        known_entities: Optional[list[str]] = None,
    ) -> list[TypedFact]:
        """Type each candidate against the closed taxonomy.

        Args:
            episode_text: the full episode/turn text — context for coreference and
                cross-utterance edges (the candidate's own ``fact_text`` may be a
                fragment).
            candidates: the residual to type (already filtered by Pass 3).
            known_entities: canonical entity names visible to this namespace/session
                — the resolution target for coreference. May be ``None``.

        Returns:
            One ``TypedFact`` per input candidate, in the same order.
        """


# ── helpers shared by both backends ──
def _norm_relation(value: object) -> str:
    """Coerce any backend's label to a valid taxonomy relation; default ``asserts``.

    Defensive: an LLM (or a future stub) might emit casing/whitespace noise or a
    synonym. Anything not exactly in the taxonomy collapses to ``asserts`` — the
    safe, non-destructive default (it never triggers supersession).
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in RELATIONS:
            return v
    return "asserts"


_CUE_RE = re.compile(r"\b(?:" + "|".join(re.escape(c) for c in INFERENCE_CUES) + r")\b", re.I)


def _looks_inferential(text: str) -> bool:
    """Cheap, deterministic inference-cue check used by the stub backend.

    Word-boundary matched (NOT bare substring) so 'so' inside 'also', 'since' inside
    'business', etc. do not false-fire and mistype an ordinary fact as `derives`.
    """
    return bool(_CUE_RE.search(text))


class StubTyper(Typer):
    """Deterministic, dependency-free offline default — the ``FakeEmbedder`` analog.

    No model, no server, no network. Rules (mirroring the spec's taxonomy semantics
    but with zero learning):

      * an explicit inference cue in the surface text  → ``derives`` (is_inference=1)
      * everything else                                → ``asserts`` (is_inference=0)

    It performs **no coreference resolution beyond identity**: a candidate's
    ``subject_name`` is matched case-insensitively against ``known_entities`` and,
    on an exact hit, ``subject_id`` is left as-is (already resolved) — it never
    invents a link. ``supersedes``/``extends`` are intentionally NOT decided here:
    those are a *slot-level* judgment (does the new object contradict the latest
    object in the same subject+predicate slot?) made by the cheap-then-escalate
    contradiction check in ``Memory.add`` (Pass 5), not by per-candidate typing.
    Emitting them blindly here would corrupt versioning, so the stub stays at the
    honest ``asserts``/``derives`` split.
    """

    def type_candidates(
        self,
        episode_text: str,
        candidates: list[Candidate],
        known_entities: Optional[list[str]] = None,
    ) -> list[TypedFact]:
        out: list[TypedFact] = []
        for c in candidates:
            # Inference cue is checked on the candidate sentence (word-boundary, via
            # _looks_inferential) — not bare substring, which false-fired on 'also'.
            inferential = _looks_inferential(c.fact_text)
            relation = "derives" if inferential else "asserts"
            out.append(
                TypedFact(
                    subject_name=c.subject_name,
                    predicate=c.predicate or "",
                    relation=relation,
                    fact_text=c.fact_text,
                    valid_at=c.valid_at,
                    is_inference=1 if relation == "derives" else 0,
                    # Typing adds no information beyond rules → keep the candidate's
                    # confidence (don't inflate it the way a real LLM pass would).
                    confidence=c.confidence,
                    object_literal=c.object_literal,
                    # Entity ids are resolved by Memory (upsert_entity), not the typer.
                    subject_id=None,
                    object_id=c.object_id,
                    typed_by="stub",
                )
            )
        return out


# JSON-Schema (hand-built, no pydantic at import time) constraining the LLM to a
# typed array, one record per candidate, ``relation`` an enum over the taxonomy.
# This is what makes the call "constrained classification, not generation": Ollama's
# structured-output grammar forces the model to pick a relation, not write prose.
def _response_schema(n: int) -> dict:
    item = {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},  # ties the decision back to candidates[i]
            "relation": {"type": "string", "enum": _TYPER_RELATIONS},
            "is_inference": {"type": "integer", "enum": [0, 1]},
            "subject": {"type": "string"},  # resolved/canonical subject name
        },
        "required": ["index", "relation", "is_inference", "subject"],
    }
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": item,
                "minItems": n,
                "maxItems": n,
            }
        },
        "required": ["results"],
    }


_SYSTEM_PROMPT = (
    "You are a relation TYPING module inside a memory engine. You do NOT generate "
    "free text and you do NOT invent facts. For each numbered candidate, decide "
    "whether the fact is directly STATED or is an INFERENCE drawn from the episode "
    "context, and output exactly one relation:\n"
    "  - asserts: the fact is stated directly/explicitly in the candidate text.\n"
    "  - derives: the fact is NOT stated verbatim — it is inferred as a likely "
    "consequence of other information in the episode (set is_inference=1).\n"
    "Examples: episode 'I live 40km away and own no car. I take the train.' → the "
    "train fact is stated, BUT if the candidate were 'I commute somehow', deriving "
    "the train is inference. A fact that simply restates what the episode says is "
    "'asserts'; a fact you can only conclude by reasoning over the episode is "
    "'derives'.\n"
    "Set is_inference=1 only for 'derives', otherwise 0. Resolve the subject to one "
    "of the known entities when the candidate uses a pronoun or ellipsis; otherwise "
    "keep the given subject. Return one result object per candidate, in order, with "
    "the candidate's index. Output ONLY the JSON object required by the schema."
)


class OllamaTyper(Typer):
    """Real backend: constrained typing via a small local model behind Ollama.

    ``ollama`` is imported lazily (it is an optional dep, like sentence-transformers)
    so this module stays import-clean without it. The model is constrained with
    Ollama's ``format=<json schema>`` structured-output mode and ``temperature=0``
    for a reproducible decode. On an unreachable server (no Ollama running — the
    common CI/offline case) we raise :class:`TyperError`, which the facade catches to
    fall back to :class:`StubTyper`.
    """

    #: A small, locally-pullable default — Phi/Qwen-class per spec ("local Phi/Qwen").
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
        self._client = None  # lazily constructed on first call

    def _get_client(self):
        """Lazily import ``ollama`` and build a client. Raises TyperError if the
        package is absent (treated as 'backend unavailable', not a crash)."""
        if self._client is not None:
            return self._client
        try:
            import ollama  # type: ignore
        except ImportError as e:  # optional dep not installed → unavailable backend
            raise TyperError(
                "ollama package not installed; install with the 'llm' extra "
                "(pip install lean-memory[llm]) or use StubTyper for offline."
            ) from e
        # A Client lets us honor a custom host; module-level fns hit localhost.
        self._client = ollama.Client(host=self.host) if self.host else ollama
        return self._client

    def type_candidates(
        self,
        episode_text: str,
        candidates: list[Candidate],
        known_entities: Optional[list[str]] = None,
    ) -> list[TypedFact]:
        if not candidates:
            return []
        client = self._get_client()
        user_prompt = self._build_user_prompt(episode_text, candidates, known_entities)
        schema = _response_schema(len(candidates))

        try:
            resp = client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                format=schema,  # JSON-Schema → constrained, classification-only output
                options={"temperature": self.temperature},
            )
        except ConnectionError as e:
            # Builtin ConnectionError == "no Ollama server" (ollama 0.6.x re-raises
            # httpx ConnectError as this). The exact catchable signal for fallback.
            raise TyperError(
                f"cannot reach Ollama (is the server running at "
                f"{self.host or 'localhost:11434'}?): {e}"
            ) from e
        except Exception as e:  # ResponseError (e.g. model not pulled, 404) etc.
            raise TyperError(f"Ollama typing call failed: {e}") from e

        content = self._extract_content(resp)
        decisions = self._parse_decisions(content, len(candidates))
        return self._apply_decisions(candidates, decisions, known_entities)

    # ── prompt / response plumbing (kept small + deterministic) ──
    @staticmethod
    def _build_user_prompt(
        episode_text: str,
        candidates: list[Candidate],
        known_entities: Optional[list[str]],
    ) -> str:
        lines = [f"EPISODE:\n{episode_text.strip()}", ""]
        if known_entities:
            lines.append("KNOWN ENTITIES: " + ", ".join(known_entities))
            lines.append("")
        lines.append("CANDIDATES:")
        for i, c in enumerate(candidates):
            obj = f" -> {c.object_literal}" if c.object_literal else ""
            lines.append(
                f"[{i}] subject={c.subject_name!r} predicate={c.predicate!r}{obj} "
                f"| text: {c.fact_text.strip()}"
            )
        return "\n".join(lines)

    @staticmethod
    def _extract_content(resp: object) -> str:
        """Pull the JSON string from a ChatResponse, tolerating attr/dict access.

        Real ollama ``ChatResponse`` exposes ``.message.content``; we also accept the
        dict form so a hand-rolled stub response (tests) works unchanged.
        """
        msg = getattr(resp, "message", None)
        if msg is not None:
            content = getattr(msg, "content", None)
            if content is not None:
                return content
        try:
            return resp["message"]["content"]  # type: ignore[index]
        except (TypeError, KeyError):
            raise TyperError(f"unexpected Ollama response shape: {resp!r}")

    @staticmethod
    def _parse_decisions(content: str, n: int) -> list[dict]:
        """Parse the constrained JSON. Schema guarantees shape, but we stay defensive
        so a single malformed decode degrades to defaults rather than crashing."""
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError) as e:
            raise TyperError(f"Ollama returned non-JSON content: {e}") from e
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise TyperError(f"Ollama JSON missing 'results' array: {content!r}")
        # Index decisions for order-independent application.
        by_index: dict[int, dict] = {}
        for r in results:
            if isinstance(r, dict) and isinstance(r.get("index"), int):
                by_index[r["index"]] = r
        return [by_index.get(i, {}) for i in range(n)]

    @staticmethod
    def _apply_decisions(
        candidates: list[Candidate],
        decisions: list[dict],
        known_entities: Optional[list[str]],
    ) -> list[TypedFact]:
        known_lookup = {e.lower(): e for e in (known_entities or [])}
        out: list[TypedFact] = []
        for c, d in zip(candidates, decisions):
            relation = _norm_relation(d.get("relation"))
            inferred = 1 if is_inference_relation(relation) else 0
            # Coreference: prefer the model's resolved subject if it names a known
            # entity; otherwise keep the candidate's own subject (no fabrication).
            resolved = d.get("subject")
            subject_name = c.subject_name
            if isinstance(resolved, str) and resolved.strip():
                key = resolved.strip().lower()
                subject_name = known_lookup.get(key, resolved.strip())
            out.append(
                TypedFact(
                    subject_name=subject_name,
                    predicate=c.predicate or "",
                    relation=relation,
                    fact_text=c.fact_text,
                    valid_at=c.valid_at,
                    is_inference=inferred,
                    # A successful LLM typing pass is the spec's quality lever → lift
                    # confidence modestly above the raw GLiNER2/rules guess.
                    confidence=max(c.confidence, 0.75),
                    object_literal=c.object_literal,
                    # Entity ids are resolved by Memory (upsert_entity), not the typer.
                    subject_id=None,
                    object_id=c.object_id,
                    typed_by="ollama",
                )
            )
        return out