"""Phase 2 runner — LongMemEval / LoCoMo slices against lean-memory's public API.

Stages: ingest → arms (copy) → read → judge → aggregate. Every stage resumable.
Offline default is a plumbing check and REFUSES a verdict; --real produces the
pinned result file. See docs/superpowers/specs/2026-07-02-phase2-eval-harness-design.md.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

_BENCH = Path(__file__).resolve().parent
_ROOT = _BENCH.parent
for _p in (str(_BENCH), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bet2_ablation import BOOTSTRAP_SEED, wilson_ci  # noqa: E402


def paired_bootstrap_acc_delta(
    arm_a: list[bool], arm_b: list[bool], *, n: int = 1000, seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float, float]:
    """95% CI on (acc_A − acc_B) in percentage points, paired over questions."""
    assert len(arm_a) == len(arm_b) and arm_a, "arms must be same-length, non-empty"
    m = len(arm_a)
    point = (sum(arm_a) / m - sum(arm_b) / m) * 100.0
    rng = random.Random(seed)
    deltas = []
    for _ in range(n):
        idx = [rng.randrange(m) for _ in range(m)]
        da = sum(arm_a[i] for i in idx) / m
        db = sum(arm_b[i] for i in idx) / m
        deltas.append((da - db) * 100.0)
    deltas.sort()
    lo = deltas[int(0.025 * n)]
    hi = deltas[min(n - 1, int(0.975 * n))]
    return point, lo, hi


def aggregate_scores(verdicts: list[dict], qtypes: dict[str, str]) -> dict:
    n = len(verdicts)
    if n == 0:
        return {"overall": 0.0, "wilson_ci": [0.0, 0.0], "n": 0, "by_type": {}}
    wins = sum(1 for v in verdicts if v["label"])
    _, lo, hi = wilson_ci(wins, n)
    by_type: dict[str, dict] = {}
    for v in verdicts:
        t = qtypes[v["question_id"]]
        b = by_type.setdefault(t, {"wins": 0, "n": 0})
        b["n"] += 1
        b["wins"] += 1 if v["label"] else 0
    return {
        "overall": wins / n if n else 0.0,
        "wilson_ci": [lo, hi],
        "n": n,
        "by_type": {t: {"acc": b["wins"] / b["n"], "n": b["n"]} for t, b in by_type.items()},
    }
