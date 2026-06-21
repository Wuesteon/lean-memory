"""Phase 1, Pass 2: GLiNER2 candidate generation (the deterministic high-recall arm).

The hybrid pipeline (BET 2) is *not* "rules emit, LLM fills gaps" — it is "deterministic
over-generates candidates at high recall, LLM does mandatory constrained typing/validation."
This module is the over-generation step: turn an episode into a *broad* set of
`(subject, relation, object)` candidates with char spans + model confidence, then hand them
to the router (Pass 3) which decides which to escalate to the LLM (Pass 4).

Two backends, mirroring Phase 0's FakeEmbedder / IdentityReranker split:

  * ``Gliner2Generator``     — the real model (gliner2 1.3.1, ~205M, CPU-friendly). Lazy-loads
                               so importing this module never pulls torch/transformers. Raises
                               a clean ImportError pointing at the ``[extract]`` extra if the
                               package is missing.
  * ``StubCandidateGenerator`` — deterministic, zero-dependency heuristics (capitalized tokens
                               as entities + a tiny verb→relation lexicon). This is the OFFLINE
                               DEFAULT so the whole test suite runs with no downloads, no GPU,
                               no servers — exactly like FakeEmbedder.

Both implement the same ``CandidateGenerator.generate(episode) -> list[Candidate]`` contract,
so ``Memory`` can swap the real model in behind the interface without touching the pipeline.

Why over-generate (low threshold, broad schema): GLiNER2-class relation extraction is weak
unaided (~17.8% zero-shot Micro-F1), and surface extractors mis-type edges and cannot emit
inferential ones. We deliberately accept false positives here because the recall-biased router
+ constrained LLM typing downstream are what make the edges trustworthy. The cost guard is the
router's escalation rate (<20% target), not the candidate count.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Optional

from ..types import Episode

# Candidate is the cross-pass currency (Pass 2 emits → Pass 3 routes → Pass 4 types).
# It lives in taxonomy.py alongside the relation taxonomy so the router/typer share one
# definition. See integrationNotes for the exact taxonomy.py contents this import expects.
from .taxonomy import Candidate

# ── Default extraction schema (broad on purpose — over-generation, per spec Pass 2) ──
# These are intentionally generic, high-frequency types/relations. The LLM-typing pass maps
# the *relation* onto our structural taxonomy (asserts|supersedes|extends|derives); these
# labels are only the surface-form candidate handles, never the final stored predicate.
DEFAULT_ENTITY_TYPES: tuple[str, ...] = (
    "person",
    "organization",
    "location",
    "product",
    "date",
    "preference",
    "role",
    "skill",
)
DEFAULT_RELATION_TYPES: tuple[str, ...] = (
    "works_at",
    "lives_in",
    "located_in",
    "likes",
    "dislikes",
    "is_a",
    "has",
    "uses",
    "knows",
    "owns",
    "member_of",
)

# LOW threshold => high recall (over-generate). The real gliner2 default is 0.5; the spec
# wants us well below that so candidates that the LLM can later validate are not pre-filtered.
DEFAULT_THRESHOLD = 0.1
# A candidate at or below this model confidence is *pre-flagged* for LLM typing (needs_typing).
# The router (Pass 3) ORs this with its other triggers (coref/ellipsis/cross-turn/derives);
# we only set the cheap, locally-knowable signal here.
DEFAULT_TYPING_THRESHOLD = 0.5


class CandidateGenerator(ABC):
    """Abstract Pass-2 candidate generator: episode → over-generated relation candidates.

    Implementations MUST be side-effect free (no writes, no entity resolution) — they only
    propose. Resolution, routing, typing, and versioning happen in later passes. Returning an
    empty list is valid (an episode may contain no extractable edges).
    """

    #: candidate-gating recall knob; lower => more candidates. Surfaced so the harness/router
    #: can log it and ablate it.
    threshold: float = DEFAULT_THRESHOLD
    #: confidence at/below which a candidate is pre-marked needs_typing (router input).
    typing_threshold: float = DEFAULT_TYPING_THRESHOLD

    @abstractmethod
    def generate(self, episode: Episode) -> list[Candidate]:
        """Return Pass-2 ``Candidate``s for this episode (source-tagged, with char spans)."""


# ──────────────────────────────────────────────────────────────────────────────
# Real backend: gliner2 1.3.1
# ──────────────────────────────────────────────────────────────────────────────
class Gliner2Generator(CandidateGenerator):
    """GLiNER2-backed candidate generator (the real Pass-2 model).

    Lazy-loads ``gliner2`` on first ``generate`` (NOT at import / construction) so that merely
    importing this module — or running the offline test suite — never triggers a torch import
    or a HuggingFace download. The weights download on the first real call unless a local dir
    path or HF cache is provided.

    Configured to OVER-GENERATE: a single forward pass for entities and one for relations, both
    at a low ``threshold`` with a broad schema, ``include_confidence=True`` and
    ``include_spans=True`` so each candidate carries the model's confidence and real character
    offsets. We reconstruct ``(subject, relation, object)`` triples ourselves because
    ``extract_relations`` returns 2-tuples keyed by relation name under a ``relation_extraction``
    wrapper — the relation is the dict KEY, never a third tuple element.
    """

    def __init__(
        self,
        model_name: str = "fastino/gliner2-base-v1",
        *,
        entity_types: tuple[str, ...] = DEFAULT_ENTITY_TYPES,
        relation_types: tuple[str, ...] = DEFAULT_RELATION_TYPES,
        threshold: float = DEFAULT_THRESHOLD,
        typing_threshold: float = DEFAULT_TYPING_THRESHOLD,
        default_subject: str = "user",
        model: Optional[Any] = None,
    ) -> None:
        self.model_name = model_name
        self.entity_types = list(entity_types)
        self.relation_types = list(relation_types)
        self.threshold = threshold
        self.typing_threshold = typing_threshold
        self.default_subject = default_subject
        # `model` lets tests/quality-tier callers inject a preloaded GLiNER2 (or a duck-typed
        # fake exposing extract_entities/extract_relations) without a download. None => lazy load.
        self._model = model

    # ── lazy model load (the only place that imports gliner2) ──
    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from gliner2 import GLiNER2  # noqa: PLC0415 — intentional lazy import
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "Gliner2Generator requires the 'gliner2' package (with torch). "
                "Install the extraction extra:  pip install 'lean-memory[extract]'  "
                "(plain `pip install gliner2` omits torch; the [extract]/[local] extra adds it). "
                "For offline tests use StubCandidateGenerator instead — it needs no model."
            ) from exc
        # map_location="cpu" keeps us on CPU even if a checkpoint defaults elsewhere; the base
        # model is built for efficient CPU inference (no GPU required).
        self._model = GLiNER2.from_pretrained(self.model_name, map_location="cpu")
        return self._model

    def generate(self, episode: Episode) -> list[Candidate]:
        text = episode.raw
        if not text or not text.strip():
            return []
        model = self._ensure_model()

        # Two low-threshold forward passes with confidences + char spans.
        ent_result = model.extract_entities(
            text,
            self.entity_types,
            threshold=self.threshold,
            include_confidence=True,
            include_spans=True,
        )
        rel_result = model.extract_relations(
            text,
            self.relation_types,
            threshold=self.threshold,
            include_confidence=True,
            include_spans=True,
        )

        # Map each entity surface span → its model confidence, so a relation's head/tail
        # inherits the better-known entity confidence when the relation element omits it.
        entity_conf = self._index_entity_confidence(ent_result)
        return self._relations_to_candidates(rel_result, episode, entity_conf)

    # ── shape parsing (all per the verified gliner2 1.3.1 return shapes) ──
    @staticmethod
    def _index_entity_confidence(ent_result: dict[str, Any]) -> dict[str, float]:
        """{entity surface text → confidence}. ent_result is keyed by entity-type name; each
        value is a list of {"text","confidence","start","end"} dicts (both flags on)."""
        index: dict[str, float] = {}
        for items in (ent_result or {}).values():
            for it in items or []:
                if isinstance(it, dict) and "text" in it:
                    txt = it["text"]
                    conf = float(it.get("confidence", 1.0))
                    # keep the max confidence if a surface form appears under several types
                    if txt not in index or conf > index[txt]:
                        index[txt] = conf
        return index

    def _relations_to_candidates(
        self,
        rel_result: dict[str, Any],
        episode: Episode,
        entity_conf: dict[str, float],
    ) -> list[Candidate]:
        out: list[Candidate] = []
        # extract_relations always wraps under "relation_extraction"; the relation NAME is the
        # inner dict key, each element is a (head, tail) 2-tuple (default) or a {"head","tail"}
        # dict (when include_confidence/include_spans are set, which we always do).
        relmap = (rel_result or {}).get("relation_extraction", {}) or {}
        for relation_name, pairs in relmap.items():
            for p in pairs or []:
                head_txt, head_span, head_conf = _read_endpoint(p, "head")
                tail_txt, tail_span, tail_conf = _read_endpoint(p, "tail")
                if not head_txt or not tail_txt:
                    continue
                # Confidence: prefer the relation endpoints, fall back to entity-pass confidence.
                conf = _combine_confidence(
                    head_conf,
                    tail_conf,
                    entity_conf.get(head_txt),
                    entity_conf.get(tail_txt),
                )
                fact_text = _candidate_sentence(episode.raw, head_span, tail_span)
                out.append(
                    Candidate(
                        subject_name=head_txt,
                        predicate=relation_name,
                        object_literal=tail_txt,
                        fact_text=fact_text,
                        valid_at=episode.t_ref,
                        confidence=conf,
                        source="gliner2",
                        subject_span=head_span,
                        object_span=tail_span,
                        # Pre-flag low-confidence candidates for the LLM-typing batch. The router
                        # ORs this with coref/ellipsis/cross-turn/derives triggers (Pass 3).
                        needs_typing=conf < self.typing_threshold,
                    )
                )
        return out


# ──────────────────────────────────────────────────────────────────────────────
# Offline default backend: deterministic heuristics (no model)
# ──────────────────────────────────────────────────────────────────────────────
# Tiny verb→relation lexicon. This is deliberately *not* good — it is the reproducible,
# zero-download stand-in for GLiNER2 so Pass 2 is exercisable in tests. Patterns are ordered;
# first match wins per sentence. (RulesExtractor uses a similar lexicon for its predicate slot.)
_VERB_RELATIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:works?|working)\s+(?:at|for)\b", re.I), "works_at"),
    (re.compile(r"\b(?:lives?|living|based)\s+in\b", re.I), "lives_in"),
    (re.compile(r"\b(?:located|situated)\s+in\b", re.I), "located_in"),
    (re.compile(r"\b(?:dislikes?|hates?)\b", re.I), "dislikes"),
    (re.compile(r"\b(?:likes?|loves?|enjoys?|prefers?)\b", re.I), "likes"),
    (re.compile(r"\b(?:is|am|are)\s+(?:an?\s+)?", re.I), "is_a"),
    (re.compile(r"\b(?:uses?|using)\b", re.I), "uses"),
    (re.compile(r"\b(?:owns?|has|have)\b", re.I), "has"),
    (re.compile(r"\b(?:knows?)\b", re.I), "knows"),
]

#: Relations clear-cut enough that a first-person, single-object utterance about them
#: is "trivially explicit" → high confidence → the router routes it `direct` (no LLM).
#: The looser/structural ones (is_a, has, knows, located_in) stay heuristic and escalate.
_EXPLICIT_RELATIONS: frozenset[str] = frozenset(
    {"works_at", "lives_in", "likes", "dislikes", "uses"}
)

# First-person markers → the candidate subject is the configured default subject (the user),
# matching RulesExtractor's behaviour so stub and rules agree on "I/my/me" → "user".
_FIRST_PERSON = re.compile(r"\b(?:I|I'm|my|me|mine)\b")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
# A "named entity" heuristic: a (possibly multi-word) Capitalized run, e.g. "Acme", "San Francisco".
_CAP_RUN = re.compile(r"\b[A-Z][\w.&-]*(?:\s+[A-Z][\w.&-]*)*")


class StubCandidateGenerator(CandidateGenerator):
    """Deterministic, offline Pass-2 generator — the test/CI default (no model, no download).

    Heuristics only: split into sentences; for each sentence with a recognized relation verb,
    pick a subject (the user for first-person sentences, else the first capitalized run) and an
    object (a capitalized run after the verb, else the trailing noun phrase). Emits one
    ``Candidate`` per matched sentence with ``source="stub"`` and real ``str.find``-based char
    spans, so downstream span/confidence handling is exercised identically to the real backend.

    Determinism contract (mirrors FakeEmbedder): identical episode text → byte-identical
    candidate list across processes and machines. Confidence is a fixed, modest constant so the
    router's needs_typing path is reachable in tests without a model.
    """

    #: A trivially-explicit fact (known relation verb + first-person subject + clean
    #: object) is HIGH confidence → routes `direct`, skipping the LLM, per the spec's
    #: "trivially-explicit high-confidence intra-utterance facts skip the LLM." A
    #: heuristic guess (capitalized-run subject, unknown-ish structure) stays LOW so
    #: the router escalates it. This split is what makes the BET-2 <20%-escalation
    #: target reachable on the OFFLINE default backend (not just with real GLiNER2).
    STUB_CONFIDENCE_EXPLICIT = 0.9
    STUB_CONFIDENCE_HEURISTIC = 0.4

    def __init__(
        self,
        *,
        default_subject: str = "user",
        threshold: float = DEFAULT_THRESHOLD,
        typing_threshold: float = DEFAULT_TYPING_THRESHOLD,
    ) -> None:
        self.default_subject = default_subject
        self.threshold = threshold
        self.typing_threshold = typing_threshold

    def generate(self, episode: Episode) -> list[Candidate]:
        text = episode.raw or ""
        out: list[Candidate] = []
        cursor = 0  # track offset of each sentence within the episode for valid char spans
        for sentence in _split_sentences(text):
            # locate this sentence in the raw text to keep spans absolute (not sentence-relative)
            base = text.find(sentence, cursor)
            if base < 0:
                base = cursor
            cursor = base + len(sentence)

            matched = self._match_relation(sentence)
            if matched is None:
                continue
            relation, verb_end = matched

            first_person = bool(_FIRST_PERSON.search(sentence))
            subj_text, subj_span = self._subject(sentence, base, first_person)
            # Object scan starts after BOTH the subject span and the relation verb (S-V-O order),
            # so first-person sentences ("I work at Acme") pick "Acme", not the leading "I".
            subj_end_rel = (subj_span[1] - base) if subj_span else 0
            obj_text, obj_span = self._object(sentence, base, max(subj_end_rel, verb_end))
            if obj_text is None:
                continue

            # Explicit = first-person subject + a known/explicit relation verb + a clean
            # object span. Those are HIGH confidence and route `direct`; everything else
            # (capitalized-run subject guess, no inference cue) is a heuristic edge the
            # router should escalate. Gives a meaningful (sub-100%) escalation rate offline.
            explicit = first_person and relation in _EXPLICIT_RELATIONS
            conf = self.STUB_CONFIDENCE_EXPLICIT if explicit else self.STUB_CONFIDENCE_HEURISTIC

            out.append(
                Candidate(
                    subject_name=subj_text,
                    predicate=relation,
                    object_literal=obj_text,
                    fact_text=sentence.strip(),
                    valid_at=episode.t_ref,
                    confidence=conf,
                    source="stub",
                    subject_span=subj_span,
                    object_span=obj_span,
                    needs_typing=conf < self.typing_threshold,
                )
            )
        return out

    def _match_relation(self, sentence: str) -> Optional[tuple[str, int]]:
        """First matching relation verb → (relation, end-offset of the verb in the sentence).
        The end-offset anchors object selection to the post-verb span (S-V-O)."""
        for pat, rel in _VERB_RELATIONS:
            m = pat.search(sentence)
            if m:
                return rel, m.end()
        return None

    def _subject(
        self, sentence: str, base: int, first_person: bool
    ) -> tuple[str, Optional[tuple[int, int]]]:
        """First-person → default subject (no span; it is implicit). Else first capitalized run."""
        if first_person:
            return self.default_subject, None
        m = _CAP_RUN.search(sentence)
        if m:
            return m.group(0), (base + m.start(), base + m.end())
        # fall back to the lead token so we always have a subject to type against
        first = sentence.split()
        lead = first[0].strip(".,!?;:'\"") if first else self.default_subject
        return (lead or self.default_subject), None

    def _object(
        self, sentence: str, base: int, after: int
    ) -> tuple[Optional[str], Optional[tuple[int, int]]]:
        """Pick a capitalized run that starts at/after ``after`` (post subject+verb) as the
        object; else the trailing word. ``after`` is a sentence-relative offset. Returns
        (object_text, char_span) or (None, None) if nothing usable. Trailing punctuation
        (e.g. the sentence-final '.') is trimmed so spans stay clean."""
        for m in _CAP_RUN.finditer(sentence):
            if m.start() >= after:  # object must come after the subject + relation verb
                return _trim_span(sentence, base, m.start(), m.end())
        # no capitalized run after the verb → use the last meaningful token as a literal object
        tokens = [t.strip(".,!?;:'\"") for t in sentence.split() if t.strip(".,!?;:'\"")]
        if tokens:
            last = tokens[-1]
            idx = sentence.rfind(last)
            if idx >= 0:
                return _trim_span(sentence, base, idx, idx + len(last))
            return last, None
        return None, None


# ──────────────────────────────────────────────────────────────────────────────
# shared helpers
# ──────────────────────────────────────────────────────────────────────────────
_TRAILING_PUNCT = ".,!?;:'\""


def _split_sentences(text: str) -> list[str]:
    return [s for s in _SENT_SPLIT.split(text.strip()) if s.strip()]


def _trim_span(
    sentence: str, base: int, start: int, end: int
) -> tuple[str, tuple[int, int]]:
    """Strip trailing punctuation from a sentence-relative [start,end) run and return
    (text, absolute_char_span). Keeps span and text in lock-step so callers can slice raw."""
    while end > start and sentence[end - 1] in _TRAILING_PUNCT:
        end -= 1
    return sentence[start:end], (base + start, base + end)


def _read_endpoint(
    pair: Any, key: str
) -> tuple[Optional[str], Optional[tuple[int, int]], Optional[float]]:
    """Read (text, span, confidence) for the 'head'/'tail' of a gliner2 relation element.

    Handles both shapes: a plain 2-tuple ``(head_text, tail_text)`` (no flags) and the
    ``{"head": {"text","confidence","start","end"}, "tail": {...}}`` dict (flags on). Returns
    (None, None, None) when the endpoint is missing/malformed."""
    if isinstance(pair, dict):
        ep = pair.get(key)
        if isinstance(ep, dict):
            txt = ep.get("text")
            start, end = ep.get("start"), ep.get("end")
            span = (int(start), int(end)) if start is not None and end is not None else None
            conf = ep.get("confidence")
            return txt, span, (float(conf) if conf is not None else None)
        return None, None, None
    if isinstance(pair, (tuple, list)) and len(pair) >= 2:
        # default shape: index 0 = head text, index 1 = tail text; no spans/confidence available
        return (str(pair[0] if key == "head" else pair[1]) or None), None, None
    return None, None, None


def _combine_confidence(*vals: Optional[float]) -> float:
    """Conservative confidence for a candidate = min of the available endpoint/entity confidences.

    Using the min (not the mean) keeps over-generation honest: a relation is only as trustworthy
    as its weakest grounded span, which biases borderline candidates toward needs_typing."""
    present = [v for v in vals if v is not None]
    if not present:
        return 1.0  # no signal from the model element → defer the gating to the router
    return float(min(present))


def _candidate_sentence(
    text: str, span_a: Optional[tuple[int, int]], span_b: Optional[tuple[int, int]]
) -> str:
    """Best-effort standalone sentence for a candidate: the slice of `text` spanning both
    endpoints (so the LLM-typing pass and the embedder see a contiguous, readable string).
    Falls back to the whole text when spans are unavailable."""
    spans = [s for s in (span_a, span_b) if s is not None]
    if not spans:
        return text.strip()
    lo = min(s[0] for s in spans)
    hi = max(s[1] for s in spans)
    # widen to sentence-ish boundaries so the snippet reads as a clause, not a fragment
    left = text.rfind(".", 0, lo)
    left = 0 if left < 0 else left + 1
    right = text.find(".", hi)
    right = len(text) if right < 0 else right + 1
    return text[left:right].strip() or text[lo:hi].strip()
