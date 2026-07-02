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


import hashlib
import json


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "phase2"


def _run_offline(tmp_path, benchmark, fixture, slice_):
    from phase2_eval import run_pipeline

    return run_pipeline(
        benchmark=benchmark, slice=slice_, dataset_path=FIXTURES / fixture,
        dataset_sha256="fixture", arms=["a", "b"], k=3, real=False,
        cache_root=tmp_path / "cache", results_dir=tmp_path / "results", limit=None,
    )


def test_offline_e2e_longmemeval(tmp_path):
    summary = _run_offline(tmp_path, "longmemeval", "lme_s_mini.json", "ku")
    assert summary["plumbing_only"] is True
    assert summary["arms"]["a"]["judges"]["stub"]["n"] == 1
    assert "key_experiment" in summary
    # offline: no result file is written
    assert not list((tmp_path / "results").glob("*.json"))


def test_offline_e2e_locomo(tmp_path):
    summary = _run_offline(tmp_path, "locomo", "locomo_mini.json", "temporal")
    assert summary["arms"]["a"]["judges"]["stub"]["n"] == 1


def test_read_stage_resumes(tmp_path):
    from phase2_eval import run_pipeline

    kw = dict(benchmark="longmemeval", slice="ku", dataset_path=FIXTURES / "lme_s_mini.json",
              dataset_sha256="fixture", arms=["a"], k=3, real=False,
              cache_root=tmp_path / "cache", results_dir=tmp_path / "results", limit=None)
    run_pipeline(**kw)
    hyp = next((tmp_path / "cache").rglob("hypotheses_a_*.jsonl"))
    lines_before = hyp.read_text().splitlines()
    run_pipeline(**kw)  # second run must not duplicate work
    assert hyp.read_text().splitlines() == lines_before


def test_arm_isolation_regression(tmp_path):
    """Running arm A's read stage must not mutate arm B's databases (touch())."""
    from phase2_eval import run_pipeline

    run_pipeline(benchmark="longmemeval", slice="ku",
                 dataset_path=FIXTURES / "lme_s_mini.json", dataset_sha256="fixture",
                 arms=["a", "b"], k=3, real=False, cache_root=tmp_path / "cache",
                 results_dir=tmp_path / "results", limit=None, stop_after="arms")
    b_dbs = sorted((tmp_path / "cache").rglob("arms/b/*.db"))
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in b_dbs]
    run_pipeline(benchmark="longmemeval", slice="ku",
                 dataset_path=FIXTURES / "lme_s_mini.json", dataset_sha256="fixture",
                 arms=["a"], k=3, real=False, cache_root=tmp_path / "cache",
                 results_dir=tmp_path / "results", limit=None)
    after = [hashlib.sha256(p.read_bytes()).hexdigest() for p in b_dbs]
    assert before == after
