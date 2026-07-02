import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from phase2_eval import aggregate_scores, paired_bootstrap_acc_delta


def test_paired_bootstrap_acc_delta_identical_arms_is_zero():
    a = [True, False, True, True, False] * 10
    point, lo, hi = paired_bootstrap_acc_delta(a, a)
    assert point == 0.0 and lo == 0.0 and hi == 0.0


def test_paired_bootstrap_acc_delta_deterministic_and_signed():
    a = [True] * 30 + [False] * 10
    b = [True] * 20 + [False] * 20
    p1 = paired_bootstrap_acc_delta(a, b)
    p2 = paired_bootstrap_acc_delta(a, b)
    assert p1 == p2                       # seeded
    assert p1[0] == 25.0                  # (0.75-0.50)*100
    assert p1[1] <= p1[0] <= p1[2]


def test_aggregate_scores():
    verdicts = [
        {"question_id": "a", "label": True},
        {"question_id": "b", "label": False},
        {"question_id": "c", "label": True},
    ]
    qtypes = {"a": "knowledge-update", "b": "knowledge-update", "c": "multi-session"}
    got = aggregate_scores(verdicts, qtypes)
    assert got["n"] == 3
    assert abs(got["overall"] - 2 / 3) < 1e-9
    assert got["by_type"]["knowledge-update"] == {"acc": 0.5, "n": 2}
    assert 0.0 <= got["wilson_ci"][0] <= got["overall"] <= got["wilson_ci"][1] <= 1.0
