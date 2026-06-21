"""Phase 0 extractor: rules only (regex + dateparser). No GLiNER2, no LLM.

The spec defers GLiNER2 to Phase 1 and the LLM-typing residual to Phase 1+. Phase 0
proves the spine end-to-end, so this extractor is deliberately simple and fully
deterministic: it turns an episode into one-or-more atomic facts with a parsed
`valid_at`, a coarse subject/predicate, and the standalone sentence as `fact_text`.

It is NOT meant to be good extraction — it is the reproducible candidate-generation
stub that the Phase 1 hybrid pipeline replaces. Each emitted fact is a standalone
sentence (matching supermemory's "atomic, standalone" rule), keyed off simple
subject-verb-object heuristics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from dateutil import parser as dateparser

from ..types import Episode, Fact, new_id, now_ms

# Very small relation lexicon → normalized predicate slot. Phase 1 replaces this
# with GLiNER2 schema relations + LLM typing.
_PREDICATE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(works?|working)\s+(at|for)\b", re.I), "works_at"),
    (re.compile(r"\b(lives?|living|based)\s+in\b", re.I), "lives_in"),
    (re.compile(r"\b(likes?|loves?|enjoys?|prefers?)\b", re.I), "likes"),
    (re.compile(r"\b(dislikes?|hates?)\b", re.I), "dislikes"),
    (re.compile(r"\b(is|am|are)\s+(a|an)\b", re.I), "is_a"),
    (re.compile(r"\b(has|have|owns?)\b", re.I), "has"),
    (re.compile(r"\b(uses?|using)\b", re.I), "uses"),
]

_FIRST_PERSON = re.compile(r"\b(I|I'm|I am|my|me|mine)\b", re.I)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class ExtractedFact:
    """An extractor's pre-persistence candidate (before entity resolution)."""

    subject_name: str
    predicate: str
    fact_text: str
    valid_at: int
    object_literal: Optional[str] = None
    confidence: float = 0.6  # rules-only → modest confidence; LLM typing would lift this


class RulesExtractor:
    def __init__(self, default_subject: str = "user") -> None:
        self.default_subject = default_subject

    def extract(self, episode: Episode) -> list[ExtractedFact]:
        out: list[ExtractedFact] = []
        for sentence in _split_sentences(episode.raw):
            valid_at = self._resolve_time(sentence, episode.t_ref)
            predicate = self._match_predicate(sentence)
            if predicate is None:
                continue
            subject = self.default_subject if _FIRST_PERSON.search(sentence) else _lead_noun(sentence)
            out.append(
                ExtractedFact(
                    subject_name=subject,
                    predicate=predicate,
                    fact_text=sentence.strip(),
                    valid_at=valid_at,
                )
            )
        return out

    def _match_predicate(self, sentence: str) -> Optional[str]:
        for pat, slot in _PREDICATE_PATTERNS:
            if pat.search(sentence):
                return slot
        return None

    def _resolve_time(self, sentence: str, t_ref: int) -> int:
        """Parse an explicit date in the sentence; else fall back to the episode t_ref.
        Deterministic: dateparser with a fixed default anchored at t_ref."""
        try:
            default = _ms_to_dt(t_ref)
            dt = dateparser.parse(sentence, fuzzy=True, default=default)
            if dt is not None:
                return int(dt.timestamp() * 1000)
        except (ValueError, OverflowError, TypeError):
            pass
        return t_ref


def to_fact(ef: ExtractedFact, *, namespace: str, subject_id: str, episode_id: str) -> Fact:
    """Bind an ExtractedFact to resolved ids → a persistable Fact."""
    ts = now_ms()
    return Fact(
        id=new_id(),
        namespace=namespace,
        subject_id=subject_id,
        predicate=ef.predicate,
        object_literal=ef.object_literal,
        fact_text=ef.fact_text,
        valid_at=ef.valid_at,
        episode_id=episode_id,
        confidence=ef.confidence,
        ingested_at=ts,
        created_at=ts,
    )


# ── helpers ──
def _split_sentences(text: str) -> list[str]:
    return [s for s in _SENT_SPLIT.split(text.strip()) if s.strip()]


def _lead_noun(sentence: str) -> str:
    """Crude subject guess: first capitalized token, else the first token. Phase 1's
    GLiNER2 NER replaces this."""
    for tok in sentence.split():
        clean = tok.strip(".,!?;:'\"")
        if clean and clean[0].isupper():
            return clean
    first = sentence.split()
    return first[0].strip(".,!?;:'\"") if first else "unknown"


def _ms_to_dt(ms: int):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
