"""Relation taxonomy + the pre-typing extraction `Candidate` — shared by Pass 2-4.

This module is the small, dependency-light vocabulary that the Phase 1 hybrid
extractor speaks. It pins down two things the spec (section 5) leans on:

1. The **four-relation taxonomy** — `asserts | supersedes | extends | derives` —
   our improvement over supermemory's `updates|extends|derives`. These are
   *structural* edge types (how a new fact relates to an existing `(subject,
   predicate)` slot), NOT domain predicates like `works_at`. The domain predicate
   lives on `Fact.predicate`; the taxonomy relation governs *versioning behaviour*.

     - `asserts`    — a new fact about a slot (no prior fact, or unrelated). Default.
     - `supersedes` — new fact contradicts/replaces the object of an existing slot.
                      Triggers versioning: old.valid_to / superseded_by / is_latest=0.
     - `extends`    — new fact adds detail to the same slot WITHOUT contradiction
                      (co-valid; both rows stay is_latest=1).
     - `derives`    — inferential, cross-utterance. `is_inference=1`, and per the
                      spec it is **LLM-only**: rules/GLiNER2 surface-form passes can
                      never emit it, only the Pass 4 constrained-typing step can.

2. The **`Candidate`** — the over-generated extraction unit BEFORE routing/typing.
   It is the shared currency between Pass 2 (GLiNER2 generates many at high recall),
   Pass 3 (the recall-biased router flags `needs_typing`), and Pass 4 (the LLM
   assigns a taxonomy relation + `is_inference` + resolves coreference). It is
   deliberately looser than `ExtractedFact`/`Fact`: predicate and object may be
   missing/guessed, and `needs_typing` records the router's escalation decision.

Kept to stdlib + dataclasses on purpose: this is imported by the rules pass (no
heavy deps), the GLiNER2 pass, the router, and the LLM-typing pass, so it must stay
import-clean and offline-testable — no torch, no ollama, no model download here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Relation(str, Enum):
    """The four structural relations a candidate can hold against an existing slot.

    Subclasses `str` so the value round-trips through SQLite / JSON as a plain
    string (e.g. stored as a column, or emitted by the LLM-typing schema) while
    still giving us a closed enum to validate against. `Relation("supersedes")`
    and `Relation.SUPERSEDES == "supersedes"` both work.
    """

    ASSERTS = "asserts"
    SUPERSEDES = "supersedes"
    EXTENDS = "extends"
    DERIVES = "derives"


#: The relation assigned when nothing else applies (new fact, fresh slot). Used as
#: the deterministic default so the rules/GLiNER2 passes can label candidates
#: without an LLM, and the router only escalates the genuinely ambiguous ones.
DEFAULT_RELATION = Relation.ASSERTS

#: Relations the surface-form (deterministic) passes are allowed to emit on their
#: own. `derives` is excluded: it is inferential and, per spec section 5, only the
#: LLM-typing pass may produce it. The router uses this to force escalation of any
#: candidate that looks like a possible `derives`.
DETERMINISTIC_RELATIONS: frozenset[Relation] = frozenset(
    {Relation.ASSERTS, Relation.SUPERSEDES, Relation.EXTENDS}
)

#: Relations that the LLM-typing pass (Pass 4) may assign. It is the full taxonomy:
#: the LLM both validates the cheap deterministic verdicts AND is the *only* source
#: of `derives`.
LLM_TYPED_RELATIONS: frozenset[Relation] = frozenset(Relation)

# ── compatibility aliases (single source of truth for sibling passes) ──
# llm_typer.py imports these names; export them here so there is ONE taxonomy and
# the relation list cannot drift between modules.
#: Tuple of relation string values, in taxonomy order.
RELATIONS: tuple[str, ...] = tuple(r.value for r in Relation)

#: Word-boundary inference cue tokens shared by the router (Pass 3) and the
#: StubTyper (Pass 4) so the two deterministic passes AGREE on what looks
#: inferential. Used with a \\b...\\b regex, never bare substring membership
#: (substring matching gave false positives like 'so' inside 'also').
INFERENCE_CUES: tuple[str, ...] = (
    "so", "therefore", "thus", "hence", "because", "since", "must",
    "probably", "likely", "means", "consequently", "implies", "suggests",
)


def is_inference_relation(value) -> bool:
    """True if the (string or Relation) names the inferential `derives` relation.

    Compatibility helper imported by llm_typer.py; defers to `relation_from_str`
    so messy LLM output ('Derives', ' derives ') still resolves correctly.
    """
    return relation_from_str(value) is Relation.DERIVES


def relation_from_str(value: Optional[str], *, default: Relation = DEFAULT_RELATION) -> Relation:
    """Coerce a (possibly None / LLM-emitted / messy) string into a `Relation`.

    Forgiving on input so the LLM-typing pass can hand us whatever its constrained
    schema produced without crashing the pipeline; falls back to `default` on
    anything unrecognized. Case- and whitespace-insensitive.
    """
    if isinstance(value, Relation):
        return value
    if value is None:
        return default
    key = value.strip().lower()
    for rel in Relation:
        if rel.value == key:
            return rel
    return default


def is_inference_flag(relation: Relation) -> int:
    """Map a taxonomy relation → the `Fact.is_inference` column value (0/1).

    Only `derives` is inferential. Returns an int (not bool) to match the schema's
    `is_inference INTEGER` column and `Fact.is_inference: int` field exactly.
    """
    return 1 if relation is Relation.DERIVES else 0


def is_llm_only(relation: Relation) -> bool:
    """True if only the LLM-typing pass may emit this relation (currently `derives`).

    The router consults this (via `DETERMINISTIC_RELATIONS`) to guarantee a
    deterministic pass never ships an inferential edge unreviewed.
    """
    return relation not in DETERMINISTIC_RELATIONS


def triggers_versioning(relation: Relation) -> bool:
    """True if assigning this relation must run the supersession path (Pass 5).

    Only `supersedes` flips the old slot fact's `valid_to`/`superseded_by`/
    `is_latest`. `asserts`/`extends` are co-valid (nothing retired); `derives` adds
    a new inferential row but does not retire the surface facts it was derived from.
    """
    return relation is Relation.SUPERSEDES


# ── predicate-slot helpers ──
# The versioning "slot" is the (subject_id, predicate) pair — the same key the
# store indexes (ix_fact_slot) and `find_latest_in_slot` looks up. Centralizing the
# slot construction here keeps the router, the contradiction check, and the store
# call agreeing on exactly one canonical key shape.

#: Canonical placeholder predicate for a candidate whose relation slot the
#: deterministic passes could not guess (GLiNER2 gives head/tail spans but the
#: predicate may be unknown until LLM typing). Router treats this as needs_typing.
UNTYPED_PREDICATE = "_untyped"


def normalize_predicate(predicate: Optional[str]) -> str:
    """Normalize a raw predicate string into a stable slot token.

    Lowercase, collapse internal whitespace to single underscores, strip surrounding
    punctuation. Mirrors the rules-pass convention (`works_at`, `lives_in`) so a
    GLiNER2 relation name and a rules predicate collapse to the same slot when they
    mean the same thing. Empty/None → `UNTYPED_PREDICATE`.
    """
    if not predicate:
        return UNTYPED_PREDICATE
    cleaned = predicate.strip().lower().strip("\"'.,!?;:")
    if not cleaned:
        return UNTYPED_PREDICATE
    return "_".join(cleaned.split())


def slot_key(subject_id: str, predicate: Optional[str]) -> tuple[str, str]:
    """The canonical `(subject_id, predicate)` versioning slot key.

    This is the key for supersession lookup (`store.find_latest_in_slot`) and the
    contradiction → supersession flow. The predicate is normalized so callers never
    have to remember to do it themselves.
    """
    return (subject_id, normalize_predicate(predicate))


@dataclass
class Candidate:
    """An over-generated extraction candidate BEFORE routing/typing (Pass 2-4 currency).

    Emitted at high recall by the deterministic passes (rules + GLiNER2). The fields
    are intentionally loose — `predicate`/`object_*` may be absent or only guessed —
    because the whole point of Pass 2 is to over-generate and let Pass 3 (router) and
    Pass 4 (LLM typing) decide which candidates are real and how to type them.

    Fields:
      subject_name   — resolved later to a `subject_id`; the head span / subject text.
      predicate      — guessed relation slot, or None if GLiNER2 gave only spans.
      object_name    — entity-valued object span (tail), if any (resolved to object_id).
      object_literal — literal-valued object (date/price/string), if any.
      fact_text      — the standalone sentence (what eventually gets embedded).
      valid_at       — world/event time (epoch ms), resolved against episode.t_ref.
      confidence     — generator confidence in [0,1] (GLiNER2 span/relation score, or
                       the rules-pass default). The router escalates low-confidence ones.
      source         — which generator emitted it: 'rules' | 'gliner2' | 'stub'.
      subject_span   — (start, end) char offsets of the head/subject span, if known
                       (GLiNER2 reports these; rules may not) — for coref/provenance.
      object_span    — (start, end) char offsets of the tail/object span, if known.
      object_id      — resolved entity id for the object, once the resolver runs.
      needs_typing   — the router's verdict (Pass 3). False until the router sets it;
                       True means "escalate to the Pass-4 LLM-typing batch". This is
                       the field the escalation-rate metric is computed over.
      escalation_reasons — the router's recorded reasons for escalating (for the
                       first-class escalation-rate-by-reason metric and debugging).
    """

    subject_name: str
    fact_text: str
    valid_at: int
    predicate: Optional[str] = None
    object_name: Optional[str] = None
    object_literal: Optional[str] = None
    object_id: Optional[str] = None
    confidence: float = 0.6
    source: str = "gliner2"  # 'rules' | 'gliner2' | 'stub'
    subject_span: Optional[tuple[int, int]] = None
    object_span: Optional[tuple[int, int]] = None
    needs_typing: bool = False
    escalation_reasons: list[str] = field(default_factory=list)

    # The taxonomy relation, if/when assigned. None until typed (deterministic
    # passes may set a tentative ASSERTS/EXTENDS/SUPERSEDES; Pass 4 may overwrite,
    # and is the only writer of DERIVES). Kept here so the candidate carries the
    # routing result through to the supersession/persistence step.
    relation: Optional[Relation] = None

    def slot(self, subject_id: str) -> tuple[str, str]:
        """Canonical `(subject_id, predicate)` versioning slot for this candidate."""
        return slot_key(subject_id, self.predicate)

    @property
    def normalized_predicate(self) -> str:
        """The slot-token form of `predicate` (`UNTYPED_PREDICATE` if unset)."""
        return normalize_predicate(self.predicate)

    @property
    def is_inference(self) -> int:
        """`Fact.is_inference` value implied by the assigned relation (0 if untyped)."""
        return is_inference_flag(self.relation) if self.relation is not None else 0
