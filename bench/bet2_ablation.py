"""BET-2 ablation harness — hybrid extraction vs 100%-LLM (spec §8, Phase 1 gate).

REBUILT (the previous version was invalid by construction — see README "negative
finding"). The old harness fed single sentences in isolation and read one prediction
column off the Typer, while 3/8 of its gold labels were supersedes/extends the Typer
can NEVER emit and the Resolver — which needs prior-slot context — was never invoked.
It measured near-noise (F1 0.056 / 0.163).

WHAT CHANGED. The four relations are produced by TWO components on TWO input
contracts, and the BET-2 ablation toggles exactly one knob in each. So we report TWO
metrics on ONE frozen, hashed gold set (``bench/bet2_goldset.py``), driving the real
interfaces DIRECTLY (never ``Memory.add``, which returns only fact ids — it cannot
tell ``extends`` from ``asserts`` and cannot see ``Decision.route``):

  METRIC A (Typer / derives-detection + coref) — the PRIMARY BET-2 gate.
      Drive ``Typer.type_candidates(episode_text, [cand], known_entities)``. Gold in
      {asserts, derives}. The hybrid-vs-100%-LLM contrast lives ENTIRELY here: hybrid
      types the router's 'direct' bucket with StubTyper and the escalated bucket with
      the real Typer; 100%-LLM types EVERY candidate with the real Typer. The ≤3pp
      gate is computed ONLY over the 'direct' bucket — the only set where the arms
      differ (on the escalated bucket both arms call the same LLM and are identical by
      construction, so including them dilutes the delta toward 0 and FAKES a pass).

  METRIC B (Resolver / asserts-extends-supersedes) — a SEPARATE correctness audit,
      NOT folded into the ablation delta. Drive ``ContradictionResolver.classify(
      new_fact, prior_facts, embedder, llm_typer=ARM_ADAPTER)`` with an explicitly
      populated prior-slot. Report macro-F1 + a 3x3 confusion matrix.

  ESCALATION RATE — the second BET-2 gate, read from the router over Metric A's
      candidates, gated <20% (Wilson CI), with a by_reason breakdown. PASS REQUIRES
      BOTH the A-delta gate AND the escalation gate jointly.

OFFLINE-FIRST (load-bearing). Default backends are the offline stubs
(StubCandidateGenerator / StubTyper / FakeEmbedder / StubAdjudicator). Offline the
arms are IDENTICAL BY CONSTRUCTION (both share StubTyper; FakeEmbedder's near-
orthogonal hash collapses every non-empty slot to ``low_supersedes`` so the ambiguous
band never fires and escalation is 0%). The harness PROVES that (determinism +
unbiasedness checks) and then REFUSES to emit a BET-2 verdict offline. The verdict
requires ``--real`` (SentenceTransformerEmbedder + Gliner2Generator + OllamaTyper +
an OllamaAdjudicator built here — OllamaTyper does NOT implement
``adjudicate_contradiction``, so the resolver's LLM band needs this thin adapter).

VARIANCE (BET-5). Every F1 is reported as mean ± 95% bootstrap CI (≥1000 resamples);
the ablation uses a PAIRED bootstrap. ``--real`` OllamaTyper is run ≥5 times (temp 0
is not batch-deterministic on qwen2.5:3b) and between-run std is reported SEPARATELY.
If the gate sits inside the CI, the verdict is "INCONCLUSIVE — widen set", not
PASS/FAIL. n-per-class prints beside every F1 so a denominator error is visible.

    python bench/bet2_ablation.py            # offline — plumbing/determinism check only
    python bench/bet2_ablation.py --real     # the actual BET-2 verdict (needs ollama + extras)
    python bench/bet2_ablation.py --real --decodes 5 --bootstrap 2000
    python bench/bet2_ablation.py --sweep    # escalation-vs-F1 threshold sweep
"""

from __future__ import annotations

import argparse
import math
import random
import statistics
import sys
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional

# Make `bench/` importable as a sibling and `src/` layout work when run directly.
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from bet2_goldset import (  # noqa: E402
    FROZEN_CONF_THRESHOLD,
    FROZEN_HIGH_SIM,
    FROZEN_LOW_SIM,
    GOLD_CASES,
    GoldCase,
    goldset_hash,
    lint_goldset,
    resolver_cases,
    typer_cases,
    validate_goldset,
)
from lean_memory.extract.contradiction import (  # noqa: E402
    EXTENDS,
    SUPERSEDES,
    ContradictionResolver,
)
from lean_memory.extract.router import RecallBiasedRouter  # noqa: E402
from lean_memory.extract.taxonomy import Candidate  # noqa: E402
from lean_memory.types import Fact  # noqa: E402

class BackendUnavailable(RuntimeError):
    """A --real backend (ollama server / model) is unreachable. NOT a BET-2 FAIL —
    an environment error that must abort with guidance, never a verdict."""


def _type(typer, episode_text: str, candidates: list, known: list):
    """Call a Typer, converting a backend-availability TyperError into a clean abort.

    Keeps the StubTyper path (offline) raising nothing, while a down Ollama server on
    --real surfaces as actionable guidance instead of a stack trace. A *config* error
    (bad relation → ValueError) is NOT swallowed — only availability errors are."""
    from lean_memory.extract.llm_typer import TyperError

    try:
        return typer.type_candidates(episode_text, candidates, known)
    except TyperError as exc:
        raise BackendUnavailable(
            f"{exc}\n  Start the model first:  ollama serve  &&  ollama pull qwen2.5:3b"
        ) from exc


# ── gate constants (frozen before scoring; BET-5) ──────────────────────────────
DELTA_GATE_PP = 3.0  # ≤ 2-3pp Relation-F1 vs 100%-LLM
ESCALATION_GATE = 0.20  # < 20%
DEFAULT_BOOTSTRAP = 1000
DEFAULT_DECODES = 5
BOOTSTRAP_SEED = 20260620


# ══════════════════════════════════════════════════════════════════════════════
# LLM-adjudication adapters (the resolver's ambiguous-band rung; LLMTyper protocol)
# ══════════════════════════════════════════════════════════════════════════════
class StubAdjudicator:
    """Deterministic offline adjudicator (FakeEmbedder analog) for the resolver's LLM band.

    Implements the ``LLMTyper`` protocol (``adjudicate_contradiction``) the resolver
    calls at ``contradiction.py`` line 209. Offline, FakeEmbedder collapses the bands
    so this is essentially never reached — but it MUST exist so the 100%-LLM arm can
    be constructed without a server and the plumbing is exercised. Token-subsumption
    mirrors ``_is_refinement``: superset ⇒ extends, else supersedes.
    """

    def adjudicate_contradiction(self, new_fact: Fact, existing_fact: Fact) -> str:
        new_toks = _toks(new_fact.object_literal or new_fact.fact_text)
        old_toks = _toks(existing_fact.object_literal or existing_fact.fact_text)
        if new_toks and old_toks and (new_toks > old_toks or old_toks > new_toks):
            return EXTENDS
        return SUPERSEDES


class OllamaAdjudicator:
    """REAL resolver-band adapter — a REQUIRED build artifact for ``--real`` Metric B.

    OllamaTyper implements only ``type_candidates``, NOT ``adjudicate_contradiction``
    (the method ``contradiction.py`` line 209 calls), so it cannot drive the resolver's
    LLM rung directly. This thin adapter makes the single constrained extends|supersedes
    call the resolver needs, implementing the ``LLMTyper`` protocol. On any transport
    error it falls back to the StubAdjudicator (mirrors the typer's Ollama→stub
    fallback) so the resolver never sees a transport exception.

    NOTE (reported, not hidden): ``Memory.add`` hard-wires ``llm_typer=None``, so in
    production today the resolver's LLM rung is dead. Metric B's --real arm is a
    FORWARD-LOOKING instrument for when that wiring is enabled.
    """

    _SYSTEM = (
        "You are a contradiction adjudicator inside a memory engine. Given a NEW fact "
        "and an EXISTING fact that fill the same (subject, predicate) slot, decide ONE "
        "label:\n"
        "  extends    — the new fact ADDS non-contradicting detail (both stay true).\n"
        "  supersedes — the new fact REPLACES/contradicts the existing value.\n"
        "Answer with exactly one word: extends or supersedes."
    )

    def __init__(self, model: str = "qwen2.5:3b", *, host: Optional[str] = None,
                 temperature: float = 0.0) -> None:
        self.model = model
        self.host = host
        self.temperature = temperature
        self._client = None
        self._fallback = StubAdjudicator()

    def _get_client(self):
        if self._client is not None:
            return self._client
        import ollama  # type: ignore  # noqa: PLC0415 — optional dep, lazy

        self._client = ollama.Client(host=self.host) if self.host else ollama
        return self._client

    def adjudicate_contradiction(self, new_fact: Fact, existing_fact: Fact) -> str:
        prompt = (
            f"EXISTING: {existing_fact.fact_text!r} "
            f"(object={existing_fact.object_literal!r})\n"
            f"NEW: {new_fact.fact_text!r} (object={new_fact.object_literal!r})\n"
            "Label (extends|supersedes):"
        )
        try:
            client = self._get_client()
            resp = client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                options={"temperature": self.temperature},
            )
            content = _ollama_content(resp).strip().lower()
        except Exception:
            # transport / package / decode error → deterministic fallback (never raise)
            return self._fallback.adjudicate_contradiction(new_fact, existing_fact)
        if "extend" in content:
            return EXTENDS
        if "supersede" in content:
            return SUPERSEDES
        return self._fallback.adjudicate_contradiction(new_fact, existing_fact)


def _ollama_content(resp: object) -> str:
    msg = getattr(resp, "message", None)
    if msg is not None:
        content = getattr(msg, "content", None)
        if content is not None:
            return content
    try:
        return resp["message"]["content"]  # type: ignore[index]
    except (TypeError, KeyError):
        return ""


def _toks(text: str) -> set[str]:
    import re

    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


# ══════════════════════════════════════════════════════════════════════════════
# Backend wiring (offline stubs by default; --real swaps in the heavy backends)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Backends:
    """The (generator, embedder, typer, adjudicator) tuple shared by both arms.

    The ONLY differences between arms are (i) which Typer types the router's 'direct'
    bucket and (ii) whether ``classify`` is handed ``adjudicator``. The generator,
    embedder, and the real Typer instance are SHARED so the contrast is the route/skip
    decision, nothing else.
    """

    embedder: object
    real_typer: object  # the LLM (or its stub) — types escalated + 100%-LLM
    stub_typer: object  # cheap typer for the router's de-escalated 'direct' bucket
    adjudicator: object  # resolver LLM-band adapter
    high_sim: float = FROZEN_HIGH_SIM
    low_sim: float = FROZEN_LOW_SIM
    conf_threshold: float = FROZEN_CONF_THRESHOLD
    real: bool = False


def build_backends(real: bool, *, embedder_model: str, ollama_model: str,
                   high_sim: float, low_sim: float, conf_threshold: float) -> Backends:
    from lean_memory.extract.llm_typer import StubTyper

    if not real:
        from lean_memory.embed.fake import FakeEmbedder

        return Backends(
            embedder=FakeEmbedder(),
            real_typer=StubTyper(),  # offline "LLM" arm == stub (arms identical by design)
            stub_typer=StubTyper(),
            adjudicator=StubAdjudicator(),
            high_sim=high_sim, low_sim=low_sim, conf_threshold=conf_threshold, real=False,
        )

    from lean_memory.embed.sentence_transformer import SentenceTransformerEmbedder
    from lean_memory.extract.llm_typer import OllamaTyper

    return Backends(
        embedder=SentenceTransformerEmbedder(embedder_model),
        real_typer=OllamaTyper(ollama_model),
        stub_typer=StubTyper(),
        adjudicator=OllamaAdjudicator(ollama_model),
        high_sim=high_sim, low_sim=low_sim, conf_threshold=conf_threshold, real=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# METRIC A — Typer / derives-detection. Drive type_candidates() directly.
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class TyperPred:
    """One scored Typer prediction with everything the per-bucket audit needs."""

    case_id: str
    gold_relation: str
    pred_relation: str
    bucket: str  # "direct" | "escalated"
    gold_subject: str
    pred_subject: str

    @property
    def correct(self) -> bool:
        return self.pred_relation == self.gold_relation

    @property
    def coref_correct(self) -> bool:
        return self.pred_subject.strip().lower() == self.gold_subject.strip().lower()


def _candidate_for(case: GoldCase) -> Candidate:
    """Compile a typer GoldCase into the exact Candidate the router/typer consume.

    subject_name is the RAW text the typer sees — the unresolved pronoun for a coref
    case (candidate_subject), NOT the gold answer. This makes coreference a real
    resolution task the typer must perform, not a pre-filled field (audit fix #4)."""
    return Candidate(
        subject_name=case.candidate_subject or case.subject_id,
        fact_text=case.new_fact_text,
        valid_at=case.valid_at,
        predicate=case.predicate,
        object_literal=case.new_object_literal,
        confidence=0.6,
        source="stub",
    )


def run_metric_a(cases: list[GoldCase], be: Backends, *, arm: str,
                 router: RecallBiasedRouter) -> tuple[list[TyperPred], dict]:
    """Run one arm of Metric A over the typer cases.

    HYBRID  (arm="hybrid")  : router.route() splits cands; escalated→real_typer,
                              direct→stub_typer. Bucket tagged per prediction.
    100%-LLM (arm="full")   : EVERY candidate typed by real_typer. To hold batch
                              composition constant we still call route() to record
                              the IDENTICAL per-episode escalation stats, but type the
                              whole per-episode batch with the real typer.

    Returns (predictions, router_cumulative_stats). The router is reset per call so
    its cumulative stats describe exactly this arm's pass over the cases.
    """
    router.reset_stats()
    # group cases by episode so OllamaTyper batches the same shape in both arms
    by_episode: dict[str, list[GoldCase]] = {}
    for c in cases:
        by_episode.setdefault(c.episode_text, []).append(c)

    preds: list[TyperPred] = []
    for episode_text, ep_cases in by_episode.items():
        cands = [_candidate_for(c) for c in ep_cases]
        known = sorted({e for c in ep_cases for e in c.known_entities})
        # route() runs in BOTH arms so the escalation stats are identical by
        # construction — the only thing that differs is which typer types 'direct'.
        to_type, direct = router.route(cands, known_entities=known)
        direct_ids = {id(c) for c in direct}

        if arm == "hybrid":
            typed_escalated = _type(be.real_typer, episode_text, to_type, known)
            typed_direct = be.stub_typer.type_candidates(episode_text, direct, known)
        elif arm == "full":
            # type the SAME per-episode batch with the real typer; only the
            # route/skip decision differs from hybrid, not the batch shape.
            typed_escalated = _type(be.real_typer, episode_text, to_type, known)
            typed_direct = _type(be.real_typer, episode_text, direct, known)
        else:  # pragma: no cover
            raise ValueError(f"unknown arm {arm!r}")

        for cand, case, tf in zip(to_type + direct,
                                  _reorder(ep_cases, cands, to_type + direct),
                                  typed_escalated + typed_direct):
            bucket = "direct" if id(cand) in direct_ids else "escalated"
            preds.append(
                TyperPred(
                    case_id=case.case_id,
                    gold_relation=case.gold_relation,
                    pred_relation=tf.relation,
                    bucket=bucket,
                    gold_subject=case.gold_subject or case.subject_id,
                    pred_subject=tf.subject_name,
                )
            )
    # stable order by case_id so paired bootstrap aligns arms exactly
    preds.sort(key=lambda p: p.case_id)
    return preds, router.cumulative_stats


def _reorder(ep_cases: list[GoldCase], cands: list[Candidate],
             ordered_cands: list[Candidate]) -> list[GoldCase]:
    """Map a router-reordered candidate list back to its source GoldCases (by identity)."""
    cand_to_case = {id(cand): case for cand, case in zip(cands, ep_cases)}
    return [cand_to_case[id(c)] for c in ordered_cands]


# ══════════════════════════════════════════════════════════════════════════════
# METRIC B — Resolver / asserts-extends-supersedes. Drive classify() directly.
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class ResolverPred:
    case_id: str
    gold_relation: str
    pred_relation: str
    gold_route: str
    pred_route: str

    @property
    def correct(self) -> bool:
        return self.pred_relation == self.gold_relation

    @property
    def route_match(self) -> bool:
        return self.gold_route == self.pred_route


def _prior_facts(case: GoldCase) -> list[Fact]:
    return [
        Fact(
            namespace="bench",
            subject_id=p["subject_id"],
            predicate=p["predicate"],
            fact_text=p["fact_text"],
            valid_at=p["valid_at"],
            episode_id="prior",
            object_literal=p.get("object_literal"),
        )
        for p in case.prior_slot
    ]


def _new_fact(case: GoldCase) -> Fact:
    return Fact(
        namespace="bench",
        subject_id=case.subject_id,
        predicate=case.predicate,
        fact_text=case.new_fact_text,
        valid_at=case.valid_at,
        episode_id="ep",
        object_literal=case.new_object_literal,
    )


def run_metric_b(cases: list[GoldCase], be: Backends, *, arm: str) -> list[ResolverPred]:
    """Run one arm of Metric B over the resolver cases.

    HYBRID  : ``llm_typer=None`` — the resolver only escalates the ambiguous band by
              design, and offline/hybrid it falls back to the safe default there.
    100%-LLM: ``llm_typer=adjudicator`` — every ambiguous slot escalates to the LLM band.
    The cheap bands (high_*, low_*, no_slot) are identical in both arms by construction;
    the arms can only differ on cases whose cosine lands in (low_sim, high_sim).
    """
    resolver = ContradictionResolver(high_sim=be.high_sim, low_sim=be.low_sim)
    adjudicator = be.adjudicator if arm == "full" else None
    out: list[ResolverPred] = []
    for case in cases:
        decision = resolver.classify(
            _new_fact(case), _prior_facts(case), be.embedder, llm_typer=adjudicator
        )
        out.append(
            ResolverPred(
                case_id=case.case_id,
                gold_relation=case.gold_relation,
                pred_relation=decision.label,
                gold_route=case.gold_route,
                pred_route=decision.route,
            )
        )
    out.sort(key=lambda p: p.case_id)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Scoring — exact-match macro-F1 over SUPPORTED classes only (BET-5), + variance.
# ══════════════════════════════════════════════════════════════════════════════
def macro_f1(gold: list[str], pred: list[str]) -> float:
    """Macro-F1 averaged over classes with NON-ZERO gold support only.

    The retired ``_macro_f1`` averaged over the union of gold∪pred labels, including
    zero-support classes, which deflates the mean unpredictably. We average only over
    classes actually present in the gold (the honest denominator).
    """
    classes = sorted(set(gold))
    if not classes:
        return 0.0
    f1s = []
    for lb in classes:
        tp = sum(1 for g, p in zip(gold, pred) if g == lb and p == lb)
        fp = sum(1 for g, p in zip(gold, pred) if g != lb and p == lb)
        fn = sum(1 for g, p in zip(gold, pred) if g == lb and p != lb)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if (prec + rec) else 0.0)
    return sum(f1s) / len(f1s)


def bootstrap_f1_ci(gold: list[str], pred: list[str], *, n: int, seed: int) -> tuple[float, float, float]:
    """(point, lo95, hi95) percentile bootstrap CI on macro-F1 over case resamples."""
    point = macro_f1(gold, pred)
    if len(gold) < 2:
        return point, point, point
    rng = random.Random(seed)
    idx = range(len(gold))
    samples = []
    for _ in range(n):
        pick = [rng.choice(idx) for _ in idx]
        samples.append(macro_f1([gold[i] for i in pick], [pred[i] for i in pick]))
    samples.sort()
    lo = samples[int(0.025 * len(samples))]
    hi = samples[min(len(samples) - 1, int(0.975 * len(samples)))]
    return point, lo, hi


def paired_bootstrap_delta(direct_full: list[bool], direct_hybrid: list[bool],
                           gold: list[str], pred_full: list[str], pred_hybrid: list[str],
                           *, n: int, seed: int) -> tuple[float, float, float]:
    """PAIRED bootstrap of the macro-F1 delta (full − hybrid) over the SAME cases.

    Resamples per-case indices ONCE per replicate and recomputes BOTH arms' F1 on that
    same resample (the paired structure that makes the CI correct for "within 2-3pp").
    Returns (point_delta_pp, lo95_pp, hi95_pp) — the CI the gate reads.
    """
    point = (macro_f1(gold, pred_full) - macro_f1(gold, pred_hybrid)) * 100.0
    if len(gold) < 2:
        return point, point, point
    rng = random.Random(seed)
    idx = range(len(gold))
    deltas = []
    for _ in range(n):
        pick = [rng.choice(idx) for _ in idx]
        g = [gold[i] for i in pick]
        pf = [pred_full[i] for i in pick]
        ph = [pred_hybrid[i] for i in pick]
        deltas.append((macro_f1(g, pf) - macro_f1(g, ph)) * 100.0)
    deltas.sort()
    lo = deltas[int(0.025 * len(deltas))]
    hi = deltas[min(len(deltas) - 1, int(0.975 * len(deltas)))]
    return point, lo, hi


def wilson_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson score 95% CI for a binomial rate — correct for small-n (escalation rate)."""
    if total == 0:
        return 0.0, 0.0, 0.0
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = (z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def confusion(gold: list[str], pred: list[str], labels: list[str]) -> dict:
    m = {g: Counter() for g in labels}
    for g, p in zip(gold, pred):
        m[g][p] += 1
    return m


# ══════════════════════════════════════════════════════════════════════════════
# Reporting
# ══════════════════════════════════════════════════════════════════════════════
def _support(gold: list[str]) -> str:
    c = Counter(gold)
    return ", ".join(f"{k}={c[k]}" for k in sorted(c))


def _print_confusion(title: str, gold: list[str], pred: list[str], labels: list[str]) -> None:
    m = confusion(gold, pred, labels)
    print(f"  {title} (rows=gold, cols=pred):")
    header = " " * 14 + "".join(f"{lb:>12}" for lb in labels)
    print(header)
    for g in labels:
        row = "".join(f"{m[g][p]:>12}" for p in labels)
        print(f"    {g:<10}{row}")


@dataclass
class MetricAResult:
    arm: str
    preds: list[TyperPred]
    router_stats: dict


def report(case_set: tuple[GoldCase, ...], be: Backends, *, bootstrap: int,
           decodes: int) -> bool:
    """Run both arms, print the full report, return PASS (True) / FAIL (False).

    Offline, returns True only for the plumbing/determinism checks and prints an
    explicit NO-VERDICT banner — it never claims a BET-2 pass without --real.
    """
    typers = typer_cases(case_set)
    resolvers = resolver_cases(case_set)

    print("=" * 78)
    print("BET-2 ABLATION — hybrid extraction vs 100%-LLM")
    print("=" * 78)
    print(f"gold cases: {len(case_set)}  (typer={len(typers)}, resolver={len(resolvers)})")
    print(f"goldset sha256: {goldset_hash(case_set)[:16]}")
    print(f"backends: {'REAL (gliner2/ollama/sentence-transformer)' if be.real else 'OFFLINE STUBS'}"
          f"   thresholds: high_sim={be.high_sim} low_sim={be.low_sim} "
          f"conf={be.conf_threshold}")
    for w in lint_goldset(case_set):
        print(f"  LINT WARNING: {w}")
    print()

    # ── METRIC A: both arms over the SAME typer cases, paired ────────────────
    router_h = RecallBiasedRouter(conf_threshold=be.conf_threshold)
    router_f = RecallBiasedRouter(conf_threshold=be.conf_threshold)
    a_decodes_full: list[list[TyperPred]] = []
    a_decodes_hybrid: list[list[TyperPred]] = []
    n_decodes = decodes if be.real else 1
    last_router_stats = {}
    for _ in range(n_decodes):
        ph, last_router_stats = run_metric_a(typers, be, arm="hybrid", router=router_h)
        pf, _ = run_metric_a(typers, be, arm="full", router=router_f)
        a_decodes_hybrid.append(ph)
        a_decodes_full.append(pf)

    # assert both arms scored the IDENTICAL case_ids in the IDENTICAL order (paired)
    ids_h = [p.case_id for p in a_decodes_hybrid[0]]
    ids_f = [p.case_id for p in a_decodes_full[0]]
    assert ids_h == ids_f, "Metric A arms are not paired — case order differs"

    hybrid = a_decodes_hybrid[0]
    full = a_decodes_full[0]

    _report_metric_a(hybrid, full, last_router_stats, bootstrap=bootstrap)

    # decode variance (real only; stubs are deterministic → 1 decode)
    if be.real and n_decodes > 1:
        _report_decode_variance(a_decodes_full, a_decodes_hybrid)

    # ── METRIC B: separate audit ─────────────────────────────────────────────
    b_hybrid = run_metric_b(resolvers, be, arm="hybrid")
    b_full = run_metric_b(resolvers, be, arm="full")
    _report_metric_b(b_hybrid, b_full, be, bootstrap=bootstrap)

    # ── gate evaluation ──────────────────────────────────────────────────────
    return _verdict(hybrid, full, last_router_stats, be, bootstrap=bootstrap)


def _report_metric_a(hybrid: list[TyperPred], full: list[TyperPred],
                     router_stats: dict, *, bootstrap: int) -> None:
    print("-" * 78)
    print("METRIC A — Typer (derives-detection + coref)  [PRIMARY GATE]")
    print("-" * 78)
    gold = [p.gold_relation for p in hybrid]
    print(f"  n={len(gold)}  support: {_support(gold)}")

    for name, preds in (("100%-LLM", full), ("hybrid", hybrid)):
        pr = [p.pred_relation for p in preds]
        f1, lo, hi = bootstrap_f1_ci([p.gold_relation for p in preds], pr,
                                     n=bootstrap, seed=BOOTSTRAP_SEED)
        derives_recall = _binary_recall([p.gold_relation for p in preds], pr, "derives")
        coref_acc = (sum(1 for p in preds if p.coref_correct) / len(preds)) if preds else 0.0
        print(f"  {name:<9} macro-F1={f1:.3f} [{lo:.3f},{hi:.3f}]  "
              f"derives-recall={derives_recall:.3f}  coref-acc={coref_acc:.3f}")

    # honest gate: paired delta on the DIRECT bucket only
    direct_ids = [p.case_id for p in hybrid if p.bucket == "direct"]
    _delta_block("DIRECT bucket (the honest gate)", hybrid, full, direct_ids, bootstrap)
    all_ids = [p.case_id for p in hybrid]
    _delta_block("ALL candidates (context only — dilutes toward 0)", hybrid, full, all_ids, bootstrap)

    # per-bucket error attribution: gold the router stranded in 'direct' it should
    # have escalated (unwinnable for StubTyper) is a ROUTING bug, not a typing result.
    stranded = [p for p in hybrid if p.bucket == "direct" and p.gold_relation == "derives"]
    if stranded:
        print(f"  ROUTING ATTRIBUTION: {len(stranded)} derives gold landed in 'direct' "
              f"(StubTyper can emit derives via cue, but a missed escalation is a router bug):")
        for p in stranded:
            print(f"      {p.case_id}: gold=derives pred={p.pred_relation}")
    print(f"  router by_reason: {router_stats.get('by_reason', {})}")
    print()


def _delta_block(title: str, hybrid: list[TyperPred], full: list[TyperPred],
                 subset_ids: list[str], bootstrap: int) -> None:
    hmap = {p.case_id: p for p in hybrid}
    fmap = {p.case_id: p for p in full}
    ids = [cid for cid in subset_ids if cid in hmap and cid in fmap]
    if not ids:
        print(f"  {title}: (empty subset)")
        return
    gold = [hmap[c].gold_relation for c in ids]
    pred_h = [hmap[c].pred_relation for c in ids]
    pred_f = [fmap[c].pred_relation for c in ids]
    dh = [hmap[c].correct for c in ids]
    df = [fmap[c].correct for c in ids]
    point, lo, hi = paired_bootstrap_delta(df, dh, gold, pred_f, pred_h,
                                           n=bootstrap, seed=BOOTSTRAP_SEED)
    print(f"  {title}: n={len(ids)}  support: {_support(gold)}")
    print(f"      paired F1 delta (100%-LLM − hybrid) = {point:+.1f}pp  "
          f"95%CI [{lo:+.1f},{hi:+.1f}]pp")


def _report_decode_variance(full_decodes: list[list[TyperPred]],
                            hybrid_decodes: list[list[TyperPred]]) -> None:
    print("  DECODE VARIANCE (--real, temp 0 is NOT batch-deterministic on qwen2.5:3b):")
    for name, decodes in (("100%-LLM", full_decodes), ("hybrid", hybrid_decodes)):
        f1s = [macro_f1([p.gold_relation for p in d], [p.pred_relation for p in d])
               for d in decodes]
        mean = statistics.mean(f1s)
        std = statistics.pstdev(f1s) if len(f1s) > 1 else 0.0
        flag = "  <<< EXCEEDS ~1pp §9 determinism budget — FINDING" if std > 0.01 else ""
        print(f"    {name:<9} macro-F1 {mean:.3f} ± {std:.3f} (between {len(f1s)} decodes){flag}")
    print()


def _report_metric_b(hybrid: list[ResolverPred], full: list[ResolverPred],
                     be: Backends, *, bootstrap: int) -> None:
    print("-" * 78)
    print("METRIC B — Resolver (asserts/extends/supersedes)  [SEPARATE AUDIT]")
    print("-" * 78)
    labels = ["asserts", "extends", "supersedes"]
    gold = [p.gold_relation for p in hybrid]
    print(f"  n={len(gold)}  support: {_support(gold)}")
    for name, preds in (("100%-LLM", full), ("hybrid", hybrid)):
        pr = [p.pred_relation for p in preds]
        f1, lo, hi = bootstrap_f1_ci([p.gold_relation for p in preds], pr,
                                     n=bootstrap, seed=BOOTSTRAP_SEED)
        print(f"  {name:<9} macro-F1={f1:.3f} [{lo:.3f},{hi:.3f}]")
    # confusion on the 100%-LLM arm (the more-escalated one; surfaces extends<->supersedes)
    _print_confusion("100%-LLM confusion",
                     [p.gold_relation for p in full],
                     [p.pred_relation for p in full], labels)
    # route audit: gold_route vs Decision.route (only meaningful on --real)
    if be.real:
        mism = [p for p in full if not p.route_match]
        if mism:
            print(f"  ROUTE MISMATCHES ({len(mism)}) — gold/engine semantic disagreement:")
            for p in mism:
                print(f"      {p.case_id}: gold_route={p.gold_route} pred_route={p.pred_route}")
        else:
            print("  route audit: all cases hit their gold_route ✓")
    else:
        print("  route audit: skipped offline (FakeEmbedder collapses the bands)")
    print()


def _binary_recall(gold: list[str], pred: list[str], target: str) -> float:
    tp = sum(1 for g, p in zip(gold, pred) if g == target and p == target)
    fn = sum(1 for g, p in zip(gold, pred) if g == target and p != target)
    return tp / (tp + fn) if (tp + fn) else 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Verdict — both gates jointly; refuse offline; refuse if CI straddles the gate.
# ══════════════════════════════════════════════════════════════════════════════
def _verdict(hybrid: list[TyperPred], full: list[TyperPred], router_stats: dict,
             be: Backends, *, bootstrap: int) -> bool:
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)

    # gate 1 — paired delta on the direct bucket
    direct_ids = [p.case_id for p in hybrid if p.bucket == "direct"]
    hmap = {p.case_id: p for p in hybrid}
    fmap = {p.case_id: p for p in full}
    gold = [hmap[c].gold_relation for c in direct_ids]
    pred_h = [hmap[c].pred_relation for c in direct_ids]
    pred_f = [fmap[c].pred_relation for c in direct_ids]
    dh = [hmap[c].correct for c in direct_ids]
    df = [fmap[c].correct for c in direct_ids]
    delta, dlo, dhi = paired_bootstrap_delta(df, dh, gold, pred_f, pred_h,
                                             n=bootstrap, seed=BOOTSTRAP_SEED)
    half_width = (dhi - dlo) / 2.0

    # gate 2 — escalation rate
    seen = router_stats.get("seen", 0)
    esc = router_stats.get("escalated", 0)
    rate, rlo, rhi = wilson_ci(esc, seen)

    print(f"  [gate 1] direct-bucket paired F1 delta = {delta:+.1f}pp  "
          f"95%CI [{dlo:+.1f},{dhi:+.1f}]pp  (gate: upper ≤ {DELTA_GATE_PP}pp)")
    print(f"  [gate 2] escalation rate = {rate:.1%}  Wilson95% [{rlo:.1%},{rhi:.1%}]  "
          f"(gate: < {ESCALATION_GATE:.0%})")

    if not be.real:
        print()
        print("  *** PLUMBING CHECK ONLY — NO BET-2 VERDICT OFFLINE ***")
        print("  FakeEmbedder's near-orthogonal hash collapses every non-empty slot to")
        print("  low_supersedes (the extends/ambiguous bands never fire, escalation=0%),")
        print("  and both arms share StubTyper, so the arms are IDENTICAL by construction.")
        print("  Run with --real for the actual verdict.")
        # offline success == the plumbing/determinism/unbiasedness checks passed
        ok = _offline_invariants(hybrid, full)
        print(f"  offline invariants (determinism + unbiasedness): "
              f"{'PASS' if ok else 'FAIL'}")
        return ok

    # small-n power bar (BET-5): refuse a verdict when the CI is too wide to read.
    if half_width > DELTA_GATE_PP:
        print()
        print(f"  *** UNDERPOWERED — CI half-width {half_width:.1f}pp > {DELTA_GATE_PP}pp gate. "
              f"Widen the gold set; verdict is INCONCLUSIVE. ***")
        return False

    gate1 = dhi <= DELTA_GATE_PP  # upper bound of the delta within the gate
    gate2 = rhi < ESCALATION_GATE  # escalation upper bound under the cap
    # derives recall must not collapse on hybrid (else it is a ROUTING bug)
    hr = _binary_recall([p.gold_relation for p in hybrid],
                        [p.pred_relation for p in hybrid], "derives")
    fr = _binary_recall([p.gold_relation for p in full],
                        [p.pred_relation for p in full], "derives")
    gate3 = hr >= fr - 0.10  # tolerate small noise; large drop = routing failure

    print()
    print(f"  gate 1 (delta upper ≤ {DELTA_GATE_PP}pp): {'PASS' if gate1 else 'FAIL'}")
    print(f"  gate 2 (escalation upper < {ESCALATION_GATE:.0%}): {'PASS' if gate2 else 'FAIL'}")
    print(f"  gate 3 (hybrid derives-recall {hr:.2f} ≥ 100%-LLM {fr:.2f} − 0.10): "
          f"{'PASS' if gate3 else 'FAIL (routing bug)'}")
    overall = gate1 and gate2 and gate3
    print()
    print(f"  BET-2: {'PASS' if overall else 'FAIL'}  "
          f"(both gates required jointly; never read either alone)")
    return overall


def _offline_invariants(hybrid: list[TyperPred], full: list[TyperPred]) -> bool:
    """Offline §9 checks: arms identical by construction + scorer unbiased on permutation."""
    # determinism: stub arms must agree case-for-case on Metric A
    hmap = {p.case_id: p.pred_relation for p in hybrid}
    fmap = {p.case_id: p.pred_relation for p in full}
    identical = hmap == fmap
    # unbiasedness: a label permutation should drive macro-F1 toward chance, not stay high
    gold = [p.gold_relation for p in hybrid]
    rng = random.Random(BOOTSTRAP_SEED)
    permuted = gold[:]
    rng.shuffle(permuted)
    perm_f1 = macro_f1(gold, permuted)
    true_f1 = macro_f1(gold, [p.pred_relation for p in hybrid])
    unbiased = perm_f1 <= true_f1 + 1e-9  # permutation never scores ABOVE the real preds
    if not identical:
        print("    FAIL: offline arms differ (should be byte-identical — both share StubTyper)")
    if not unbiased:
        print(f"    FAIL: permuted-label F1 {perm_f1:.3f} > true F1 {true_f1:.3f} (scorer biased)")
    return identical and unbiased


# ══════════════════════════════════════════════════════════════════════════════
# Threshold sweep — escalation-vs-F1 cost/quality knob (so one point can't hide it)
# ══════════════════════════════════════════════════════════════════════════════
def run_sweep(case_set: tuple[GoldCase, ...], *, real: bool, embedder_model: str,
              ollama_model: str, bootstrap: int) -> None:
    print("=" * 78)
    print("THRESHOLD SWEEP — escalation-vs-F1 (cost/quality tradeoff)")
    print("=" * 78)
    typers = typer_cases(case_set)
    print(f"{'conf_thr':>9} {'escalation':>11} {'hybrid-F1':>10} {'full-F1':>10}")
    for conf in (0.3, 0.4, 0.5, 0.6, 0.7, 0.85):
        be = build_backends(real, embedder_model=embedder_model, ollama_model=ollama_model,
                            high_sim=FROZEN_HIGH_SIM, low_sim=FROZEN_LOW_SIM,
                            conf_threshold=conf)
        router_h = RecallBiasedRouter(conf_threshold=conf)
        router_f = RecallBiasedRouter(conf_threshold=conf)
        ph, stats = run_metric_a(typers, be, arm="hybrid", router=router_h)
        pf, _ = run_metric_a(typers, be, arm="full", router=router_f)
        f1h = macro_f1([p.gold_relation for p in ph], [p.pred_relation for p in ph])
        f1f = macro_f1([p.gold_relation for p in pf], [p.pred_relation for p in pf])
        print(f"{conf:>9.2f} {stats.get('rate', 0.0):>10.1%} {f1h:>10.3f} {f1f:>10.3f}")
    print()


# ══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--real", action="store_true",
                    help="use Gliner2/OllamaTyper/SentenceTransformer + OllamaAdjudicator")
    ap.add_argument("--embedder", default="Qwen/Qwen3-Embedding-0.6B",
                    help="--real embedder model (graded cosine fires the resolver bands)")
    ap.add_argument("--ollama-model", default="qwen2.5:3b")
    ap.add_argument("--bootstrap", type=int, default=DEFAULT_BOOTSTRAP,
                    help="bootstrap resamples for every F1 CI (>=1000 recommended)")
    ap.add_argument("--decodes", type=int, default=DEFAULT_DECODES,
                    help="--real decode repeats for between-run std (temp 0 not deterministic)")
    ap.add_argument("--sweep", action="store_true",
                    help="run the escalation-vs-F1 threshold sweep instead of the gate")
    args = ap.parse_args()

    # BET-5: validate + lint the frozen set at LOAD time. Abort loudly, never mis-score.
    try:
        validate_goldset(GOLD_CASES)
    except Exception as exc:  # GoldsetError or anything structural
        print(f"GOLDSET INVALID — refusing to run: {exc}", file=sys.stderr)
        return 2

    if args.sweep:
        run_sweep(GOLD_CASES, real=args.real, embedder_model=args.embedder,
                  ollama_model=args.ollama_model, bootstrap=args.bootstrap)
        return 0

    be = build_backends(args.real, embedder_model=args.embedder,
                        ollama_model=args.ollama_model, high_sim=FROZEN_HIGH_SIM,
                        low_sim=FROZEN_LOW_SIM, conf_threshold=FROZEN_CONF_THRESHOLD)
    try:
        passed = report(GOLD_CASES, be, bootstrap=args.bootstrap, decodes=args.decodes)
    except BackendUnavailable as exc:
        print(f"\nBACKEND UNAVAILABLE — no BET-2 verdict (environment error, not FAIL):\n  {exc}",
              file=sys.stderr)
        return 2
    # exit non-zero on a real FAIL so CI can gate on it; offline always exits 0 on
    # passing plumbing checks (it never emits a real verdict).
    return 0 if passed else (0 if not be.real else 1)


if __name__ == "__main__":
    sys.exit(main())
