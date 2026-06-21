"""Salience (importance) scoring at write time — spec section 6B.

The spec says importance is "rated once at write and cached" and consumed at rank
time as `importance = salience/10` (see Retriever's salience-decay re-score). The
*real* Phase >1 plan is an LLM rater that judges how memorable/consequential a fact
is. Phase 1 keeps it DETERMINISTIC and cheap — no LLM, no network, pure stdlib —
exactly like RulesExtractor is the deterministic stub for GLiNER2/LLM extraction.

This is a STUB on purpose: `score_salience` is the seam an LLM rater drops into
later (same signature, same [0, 10] contract). Until then it uses transparent
heuristics so the value is reproducible in tests and explainable in eval:

  * facts with concrete grounding (dates, numbers, proper nouns) matter more —
    "moved to Berlin on 2025-03-01" is more consequential than "likes coffee";
  * directly asserted observations outrank inferred ones — an inference is a
    derived guess, so it starts lower (the LLM rater would also discount these);
  * non-user sources (assistant/tool/doc) are background context, so they get a
    mild discount versus first-party user statements;
  * very short / filler facts score low; longer specific facts score higher,
    with diminishing returns so a wall of text can't dominate.

The output is clamped to [0, 10] to match the `salience REAL ... DEFAULT 0.0`
column and the `importance = salience/10 ∈ [0, 1]` rank-time normalization.
"""

from __future__ import annotations

import re

# ── tunable weights (kept module-level + named so the heuristic is auditable) ──
# A neutral baseline so an unremarkable-but-valid fact lands mid-scale, leaving
# head-room for both boosts and penalties.
_BASELINE = 4.0

# Signals that a fact is concrete/consequential rather than vague preference talk.
# Each present signal adds its weight (once), so grounding compounds.
_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"  # ISO date
    r"|\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"  # 1/2 or 01/02/2025
    r"|\b\d{1,2}:\d{2}\b"  # clock time
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\b",
    re.I,
)
_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?\b")  # any standalone number/quantity
# Proper noun = a capitalized token that is NOT the sentence's first word. We strip
# the lead token before matching so "I moved" doesn't count "I" as a proper noun.
_PROPER_NOUN = re.compile(r"(?<!^)\b[A-Z][a-zA-Z]+\b")

_DATE_BOOST = 1.6
_NUMBER_BOOST = 0.9
_PROPER_NOUN_BOOST = 1.2

# Penalties.
# Inferred facts are derived guesses (Fact.is_inference / relation 'derives'); the
# LLM rater would trust them less, so the deterministic stub mirrors that prior.
_INFERENCE_PENALTY = 2.0
# First-party user statements are the primary signal; everything else is context.
_NON_USER_PENALTY = 1.0

# Length shaping: reward specificity up to a point, then flatten so verbosity alone
# can't win. Word counts (cheap, locale-free) are good enough for a stub.
_FILLER_MAX_WORDS = 3  # "ok thanks", "yes" → near-floor importance
_FILLER_PENALTY = 2.5
_LEN_BONUS_PER_WORD = 0.18
_LEN_BONUS_CAP = 1.8  # reached around ~10 content words

# Score bounds match the DB column + importance=salience/10 normalization.
_MIN, _MAX = 0.0, 10.0

_PROPER_NOUN_LEAD = re.compile(r"^\W*\w+\s*")  # drop leading token for proper-noun test


def score_salience(fact_text: str, *, source: str, is_inference: bool) -> float:
    """Rate how important/memorable a fact is, in [0, 10]. Deterministic stub.

    Cheap, pure-stdlib heuristic computed once at write and cached in
    `Fact.salience` (the Retriever later reads it as `importance = salience/10`).
    Replaceable by an LLM rater with this exact signature and range.

    Args:
        fact_text: the standalone fact sentence (what gets embedded/ranked).
        source: provenance of the originating episode — 'user'|'assistant'|'tool'|'doc'.
            User statements are first-party; other sources are discounted slightly.
        is_inference: True for derived/inferred facts (relation 'derives'); these
            start lower because they are guesses, not direct observations.

    Returns:
        A float in [0.0, 10.0]. Empty/whitespace text scores 0.0.
    """
    text = (fact_text or "").strip()
    if not text:
        # Nothing to weigh — degenerate input shouldn't crash the write path.
        return _MIN

    score = _BASELINE

    # ── grounding boosts (concrete > vague) ──
    if _DATE.search(text):
        score += _DATE_BOOST
    if _NUMBER.search(text):
        score += _NUMBER_BOOST
    # Test proper nouns on the text minus its leading token so a leading capital
    # (sentence start, "I") doesn't masquerade as a named entity.
    if _PROPER_NOUN.search(_PROPER_NOUN_LEAD.sub("", text, count=1)):
        score += _PROPER_NOUN_BOOST

    # ── length / specificity shaping ──
    n_words = len(text.split())
    if n_words <= _FILLER_MAX_WORDS:
        # Short acknowledgements / filler carry little durable information.
        score -= _FILLER_PENALTY
    else:
        # Diminishing reward for elaboration; capped so length alone can't dominate.
        score += min(_LEN_BONUS_CAP, (n_words - _FILLER_MAX_WORDS) * _LEN_BONUS_PER_WORD)

    # ── provenance / inference penalties ──
    if is_inference:
        score -= _INFERENCE_PENALTY
    if source != "user":
        score -= _NON_USER_PENALTY

    # Clamp to the cached-salience contract [0, 10].
    return max(_MIN, min(_MAX, score))
