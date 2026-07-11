"""Offline escalation-threshold probe on REAL LongMemEval turns (engine-fix backlog #1).

`bench/bet2_ablation.py --sweep` sweeps `conf_threshold` only against the 97-case
frozen GOLDSET — clean, hand-built predicate sentences. Phase 2 ingest found that
gate's <20% escalation rate does not survive contact with real conversational data:
96.7% of candidates escalated (measured on the KU shards, see
`docs/phase2-learnings.md` and the `lme_ku_shards/*/manifest.json` telemetry).

This script re-measures escalation on real LongMemEval turn text directly, sweeping
BOTH knobs that gate escalation:
  - `Gliner2Generator.typing_threshold` — Pass-2 confidence below this pre-flags
    `needs_typing` (router reason `pre_flagged`).
  - `RecallBiasedRouter.conf_threshold` — Pass-3's own low-confidence gate
    (router reason `low_confidence`).

Entirely offline: GLiNER2 weights are already HF-cached locally (confirmed via
`~/.cache/huggingface/hub/models--fastino--gliner2-base-v1`), so `generate()` runs
with zero network access and no LLM calls — this only measures what the router
WOULD escalate, matching the handoff's "offline StubTyper probe" ask (we skip the
typer entirely since escalation is decided before Pass 4 ever runs).

`known_entities` approximates `Memory._known_entity_names` (real entities already
persisted in the store) by accumulating candidate subject/object names from turns
already seen in the same namespace, capped at the same `_KNOWN_ENTITIES_CAP=100`
most-recent (memory.py) — the probe never persists, so there is no entity table to
read from.

Usage:
    .venv/bin/python bench/phase2_escalation_probe.py --slice ku --namespaces 5
    .venv/bin/python bench/phase2_escalation_probe.py --slice ku --namespaces 5 --turns-per-ns 60
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, deque
from pathlib import Path

_BENCH = Path(__file__).resolve().parent
_ROOT = _BENCH.parent
for _p in (str(_BENCH), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from phase2_ingest import load_longmemeval  # noqa: E402

from lean_memory.extract.gliner_extractor import Gliner2Generator  # noqa: E402
from lean_memory.extract.router import RecallBiasedRouter  # noqa: E402
from lean_memory.types import Episode  # noqa: E402

_KNOWN_ENTITIES_CAP = 100  # mirrors memory.py's cap (fix for prompt-inflation bug)
_DATA = _BENCH / ".phase2_cache" / "data" / "longmemeval_oracle.json"

CONF_THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.85)
TYPING_THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.85)


def _candidate_name(cand) -> str:
    return cand.object_name or cand.subject_name


def _collect_real_turns(slice_: str, n_namespaces: int, turns_per_ns: int) -> list[list[str]]:
    """Real LongMemEval turn text, grouped by namespace (session order preserved)."""
    units = load_longmemeval(_DATA, slice=slice_)
    out: list[list[str]] = []
    for unit in units[:n_namespaces]:
        texts = [t.text for t in unit.turns if t.text.strip()]
        if turns_per_ns:
            texts = texts[:turns_per_ns]
        if texts:
            out.append(texts)
    return out


def run_probe(namespaces: list[list[str]], *, typing_threshold: float, conf_threshold: float,
              generator: Gliner2Generator) -> dict:
    """One (typing_threshold, conf_threshold) point: regenerate candidates fresh per
    namespace (known_entities resets per namespace, matching real per-tenant isolation),
    route them, and aggregate escalation stats across all namespaces."""
    generator.typing_threshold = typing_threshold
    router = RecallBiasedRouter(conf_threshold=conf_threshold)

    seen = 0
    escalated = 0
    turns = 0                     # NEW: total turns processed (denominator for facts/turn)
    fact_lengths: list[int] = []  # NEW: len(fact_text) per candidate, for the median
    by_reason: Counter = Counter()

    for turns_list in namespaces:
        known: deque[str] = deque(maxlen=_KNOWN_ENTITIES_CAP)
        for turn_text in turns_list:
            turns += 1            # NEW
            episode = Episode(namespace="probe", raw=turn_text, t_ref=0, source="user")
            candidates = generator.generate(episode)
            if not candidates:
                continue
            fact_lengths.extend(len(c.fact_text or "") for c in candidates)  # NEW
            router.route(candidates, known_entities=set(known))
            stats = router.last_stats
            seen += stats["seen"]
            escalated += stats["escalated"]
            for reason, count in stats["by_reason"].items():
                by_reason[reason] += count
            for cand in candidates:
                known.append(_candidate_name(cand))

    return {
        "typing_threshold": typing_threshold,
        "conf_threshold": conf_threshold,
        "gliner_threshold": generator.threshold,          # NEW
        "seen": seen,
        "escalated": escalated,
        "rate": (escalated / seen) if seen else 0.0,
        "by_reason": dict(by_reason),
        "turns": turns,                                    # NEW
        "facts_per_turn": (seen / turns) if turns else 0.0,  # NEW
        "median_fact_len": int(statistics.median(fact_lengths)) if fact_lengths else 0,  # NEW
    }


def write_json(path: Path, *, slice_: str, n_namespaces: int, total_turns: int,
               results: list[dict]) -> None:
    """Machine-readable sweep output (consumed by the calibration report)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "slice": slice_,
        "namespaces": n_namespaces,
        "turns": total_turns,
        "results": results,
    }, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slice", default="ku", choices=("ku", "all"))
    ap.add_argument("--namespaces", type=int, default=5, help="number of LongMemEval question-units to sample")
    ap.add_argument("--turns-per-ns", type=int, default=0, help="cap turns per namespace (0 = no cap)")
    ap.add_argument("--conf", type=float, default=None, help="run a single conf_threshold point (else full sweep)")
    ap.add_argument("--typing", type=float, default=None, help="single typing_threshold (else full sweep)")
    ap.add_argument("--gliner-threshold", type=float, nargs="+", default=None,
                    help="also sweep the GLiNER candidate threshold (default: model default only)")
    ap.add_argument("--json", type=Path, default=None, help="write sweep results to this JSON file")
    args = ap.parse_args()

    if not _DATA.exists():
        print(f"Dataset not found: {_DATA}", file=sys.stderr)
        return 2

    print(f"Loading {args.slice} slice, sampling {args.namespaces} namespace(s)...")
    namespaces = _collect_real_turns(args.slice, args.namespaces, args.turns_per_ns)
    total_turns = sum(len(t) for t in namespaces)
    print(f"{len(namespaces)} namespaces, {total_turns} real turns loaded.\n")

    generator = Gliner2Generator()  # typing_threshold overwritten per sweep point below
    conf_points = (args.conf,) if args.conf is not None else CONF_THRESHOLDS
    typing_points = (args.typing,) if args.typing is not None else TYPING_THRESHOLDS

    # When the flag is absent, iterate over the single model-default threshold so
    # behavior is unchanged; otherwise sweep each requested GLiNER threshold.
    gliner_points = tuple(args.gliner_threshold) if args.gliner_threshold is not None else (generator.threshold,)

    print("=" * 88)
    print("ESCALATION SWEEP — real LongMemEval turns (offline, no LLM)")
    print("=" * 88)
    header = (f"{'gliner_thr':>10} {'typing_thr':>10} {'conf_thr':>9} {'seen':>7} "
              f"{'escalated':>10} {'rate':>8} {'facts/turn':>11} {'med_len':>8}")
    print(header)

    results = []
    t0 = time.time()
    for gliner_thr in gliner_points:
        generator.threshold = gliner_thr
        for typing_thr in typing_points:
            for conf_thr in conf_points:
                r = run_probe(namespaces, typing_threshold=typing_thr, conf_threshold=conf_thr,
                              generator=generator)
                results.append(r)
                print(f"{gliner_thr:>10.2f} {typing_thr:>10.2f} {conf_thr:>9.2f} {r['seen']:>7} "
                      f"{r['escalated']:>10} {r['rate']:>8.1%} {r['facts_per_turn']:>11.2f} "
                      f"{r['median_fact_len']:>8}")
    print(f"\n({time.time() - t0:.1f}s wall)\n")

    best = min(results, key=lambda r: r["rate"])
    print("Lowest-escalation point in this sweep:")
    print(f"  typing_threshold={best['typing_threshold']}, conf_threshold={best['conf_threshold']}"
          f" -> {best['rate']:.1%} ({best['escalated']}/{best['seen']})")
    print(f"  by_reason: {best['by_reason']}")
    if best["rate"] >= 0.20:
        print("\n  Still >= 20% design gate at the sweep's lowest point on this sample.")
    else:
        print("\n  Under the 20% design gate — validate with a larger namespace sample"
              " and bet2_ablation.py --sweep --real before re-freezing.")
    if args.json:
        write_json(args.json, slice_=args.slice, n_namespaces=len(namespaces),
                   total_turns=total_turns, results=results)
        print(f"JSON written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
