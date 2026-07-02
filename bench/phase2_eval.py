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


# ── staged runner ──

import argparse  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time as _time  # noqa: E402
from typing import Optional  # noqa: E402

from phase2_ingest import (  # noqa: E402
    DEFAULT_EMBEDDER, DEFAULT_GENERATOR_MODEL, DEFAULT_RERANKER_MODEL,
    DEFAULT_TYPER_MODEL, IngestUnit, build_memory, ensure_dataset, ingest_units,
    load_locomo, load_longmemeval,
)
from phase2_judge import (  # noqa: E402
    EvalConfig, LMEOfficialJudge, LocomoLenientJudge, LocomoStrictJudge,
    RETRIEVAL_CONSTANTS, StubJudge, config_hash,
)
from phase2_reader import (  # noqa: E402
    DEFAULT_BACKBONE, EchoReader, FC_SYSTEM_PROMPT, OpenRouterReader,
    READER_SYSTEM_PROMPT, unit_transcript,
)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, cwd=_ROOT).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _jsonl_done(path: Path, key: str) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(line)[key] for line in path.read_text().splitlines() if line}


def _append_jsonl(path: Path, obj: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def stage_arms(cache_dir: Path, arms_root: Path, arms: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for arm in arms:
        if arm == "fc":
            out[arm] = None
            continue
        arm_dir = arms_root / arm
        if not arm_dir.exists():
            arm_dir.mkdir(parents=True)
            for db in cache_dir.glob("*.db"):
                shutil.copy2(db, arm_dir / db.name)
        out[arm] = arm_dir
    return out


def stage_read(units: list[IngestUnit], arm: str, arm_dir: Optional[Path],
               out_path: Path, *, reader, k: int, real: bool, embedder_name: str) -> None:
    done = _jsonl_done(out_path, "question_id")
    mem = None if arm == "fc" else build_memory(arm_dir, real=real, embedder_name=embedder_name)
    try:
        for unit in units:
            for q in unit.questions:
                if q.question_id in done:
                    continue
                if arm == "fc":
                    hyp = reader.answer_full_context(q.question, unit_transcript(unit))
                    hits_out = []
                else:
                    hits = mem.search(unit.namespace, q.question, k=k,
                                      is_latest_only=(arm == "a"))
                    hyp = reader.answer(q.question, hits)
                    hits_out = [{"fact_id": h.fact.id, "fact_text": h.fact.fact_text,
                                 "valid_at": h.fact.valid_at,
                                 "final_score": round(h.final_score, 6)} for h in hits]
                _append_jsonl(out_path, {"question_id": q.question_id,
                                         "hypothesis": hyp, "hits": hits_out})
    finally:
        if mem is not None:
            mem.close()


def stage_judge(units: list[IngestUnit], hyp_path: Path, out_path: Path, judge) -> None:
    qs = {q.question_id: q for u in units for q in u.questions}
    done = _jsonl_done(out_path, "question_id")
    for line in hyp_path.read_text().splitlines():
        entry = json.loads(line)
        qid = entry["question_id"]
        if qid in done or qid not in qs:
            continue
        label = judge.grade(qs[qid], entry["hypothesis"])
        _append_jsonl(out_path, {"question_id": qid, "judge_id": judge.judge_id,
                                 "label": bool(label), "raw": ""})


def _load_units(benchmark: str, slice: str, dataset_path: Path, expect_counts: bool):
    if benchmark == "longmemeval":
        return load_longmemeval(dataset_path, slice=slice, expect_counts=expect_counts)
    return load_locomo(dataset_path, slice=slice, expect_counts=expect_counts)


def run_pipeline(*, benchmark: str, slice: str, dataset_path: Path, dataset_sha256: str,
                 arms: list[str], k: int, real: bool, cache_root: Path, results_dir: Path,
                 limit: Optional[int], embedder_name: str = DEFAULT_EMBEDDER,
                 stage: str = "all", stop_after: Optional[str] = None) -> dict:
    units = _load_units(benchmark, slice, dataset_path, expect_counts=real)
    if limit is not None:
        units = units[:limit]
    qtypes = {q.question_id: q.question_type for u in units for q in u.questions}
    expected_qids = set(qtypes)

    mode = "real" if real else "offline"
    run_key = f"{benchmark}_{slice}_{mode}_{dataset_path.stem}"
    cache_dir = cache_root / run_key
    arms_root = cache_dir / "arms"

    reader = OpenRouterReader() if real else EchoReader()
    if not real:
        judges = [StubJudge()]
    elif benchmark == "longmemeval":
        judges = [LMEOfficialJudge()]
    else:
        judges = [LocomoLenientJudge(), LocomoStrictJudge()]

    def cfg_for(arm: str, judge) -> EvalConfig:
        return EvalConfig(
            benchmark=benchmark, slice=slice, dataset_file=dataset_path.name,
            dataset_sha256=dataset_sha256, judge_id=judge.judge_id,
            judge_model=judge.model, judge_prompt=judge.prompt_repr(),
            backbone_model=reader.model, provider="openrouter" if real else "offline",
            k=k, is_latest_only={"a": True, "b": False, "fc": "fc"}[arm],
            reader_prompt=READER_SYSTEM_PROMPT if arm != "fc" else FC_SYSTEM_PROMPT,
            embedder_model=embedder_name if real else "FakeEmbedder",
            reranker_model=DEFAULT_RERANKER_MODEL if real else "IdentityReranker",
            generator_model=DEFAULT_GENERATOR_MODEL if real else "StubCandidateGenerator",
            typer_model=DEFAULT_TYPER_MODEL if real else "StubTyper",
            retrieval_constants=json.dumps(RETRIEVAL_CONSTANTS, sort_keys=True),
            git_commit=_git_commit(),
        )

    primary_hash = config_hash(cfg_for("a" if "a" in arms else arms[0], judges[0]))
    print(f"phase2 config sha256: {primary_hash[:16]}")
    if not real:
        print("PLUMBING CHECK ONLY — NO VERDICT (offline stubs; use --real for a number)")

    # stage 1: ingest
    if stage in ("all", "ingest"):
        manifest = ingest_units(units, cache_dir, real=real,
                                embedder_name=embedder_name, limit=None)
    else:
        manifest = json.loads((cache_dir / "manifest.json").read_text())
    if stage == "ingest":
        return {"manifest": manifest}

    # stage 2: arms
    arm_dirs = stage_arms(cache_dir, arms_root, arms)
    if stop_after == "arms":
        return {"arm_dirs": {a: str(p) for a, p in arm_dirs.items()}}

    # stage 3: read
    if stage in ("all", "read"):
        for arm in arms:
            hyp = cache_dir / f"hypotheses_{arm}_{primary_hash[:16]}.jsonl"
            stage_read(units, arm, arm_dirs[arm], hyp, reader=reader, k=k,
                       real=real, embedder_name=embedder_name)
    if stage == "read":
        return {"read": "done"}

    # stage 4: judge
    if stage in ("all", "judge"):
        for arm in arms:
            hyp = cache_dir / f"hypotheses_{arm}_{primary_hash[:16]}.jsonl"
            for judge in judges:
                out = cache_dir / f"verdicts_{arm}_{judge.judge_id}_{primary_hash[:16]}.jsonl"
                stage_judge(units, hyp, out, judge)
    if stage == "judge":
        return {"judge": "done"}

    # stage 5: aggregate (refuses on missing verdicts)
    summary: dict = {"plumbing_only": not real, "config_hash": primary_hash,
                     "benchmark": benchmark, "slice": slice, "arms": {}}
    arm_bools: dict[str, dict[str, list[bool]]] = {}
    for arm in arms:
        summary["arms"][arm] = {"config_hash": config_hash(cfg_for(arm, judges[0])),
                                "config": cfg_for(arm, judges[0]).__dict__ if real else "offline",
                                "judges": {}}
        for judge in judges:
            out = cache_dir / f"verdicts_{arm}_{judge.judge_id}_{primary_hash[:16]}.jsonl"
            verdicts = [json.loads(l) for l in out.read_text().splitlines()] if out.exists() else []
            got = {v["question_id"] for v in verdicts}
            if got != expected_qids:
                raise SystemExit(
                    f"REFUSING to aggregate: {arm}/{judge.judge_id} judged {len(got)}/"
                    f"{len(expected_qids)} questions — finish the run first.")
            verdicts.sort(key=lambda v: v["question_id"])
            summary["arms"][arm]["judges"][judge.judge_id] = aggregate_scores(verdicts, qtypes)
            arm_bools.setdefault(judge.judge_id, {})[arm] = [v["label"] for v in verdicts]

    if "a" in arms and "b" in arms:
        summary["key_experiment"] = {}
        for judge_id, byarm in arm_bools.items():
            point, lo, hi = paired_bootstrap_acc_delta(byarm["a"], byarm["b"])
            summary["key_experiment"][judge_id] = {
                "delta_pp": point, "ci95_pp": [lo, hi],
                "n_paired": len(byarm["a"]), "seed": BOOTSTRAP_SEED,
            }

    # spec: the lenient-vs-strict gap is a first-class result-file field on LoCoMo
    if benchmark == "locomo" and real:
        summary["judge_gap"] = {
            arm: round(
                summary["arms"][arm]["judges"]["locomo-lenient"]["overall"]
                - summary["arms"][arm]["judges"]["locomo-strict"]["overall"], 4)
            for arm in arms
        }

    summary["ingest_telemetry"] = manifest.get("namespaces", {})

    if real:
        results_dir.mkdir(parents=True, exist_ok=True)
        out_file = results_dir / f"{benchmark}_{slice}_{primary_hash[:16]}.json"
        summary["created_at"] = _time.strftime("%Y-%m-%dT%H:%M:%S%z")
        out_file.write_text(json.dumps(summary, indent=1, default=str))
        print(f"result file: {out_file}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 2 benchmark runner")
    ap.add_argument("--benchmark", required=True, choices=["longmemeval", "locomo"])
    ap.add_argument("--slice", default=None, help="ku|temporal|all (default per benchmark)")
    ap.add_argument("--variant", default="s", choices=["s", "oracle"],
                    help="longmemeval file variant")
    ap.add_argument("--arms", default="a,b")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--stage", default="all",
                    choices=["all", "ingest", "read", "judge", "aggregate"])
    ap.add_argument("--embedder", default=DEFAULT_EMBEDDER)
    ap.add_argument("--data-dir", default=str(_BENCH / ".phase2_cache" / "data"))
    ap.add_argument("--cache-root", default=str(_BENCH / ".phase2_cache"))
    ap.add_argument("--results-dir", default=str(_BENCH / "results" / "phase2"))
    args = ap.parse_args()

    slice_ = args.slice or ("ku" if args.benchmark == "longmemeval" else "temporal")
    ds_name = "locomo10" if args.benchmark == "locomo" else (
        "lme_oracle" if args.variant == "oracle" else "lme_s")
    dataset_path, sha = ensure_dataset(ds_name, Path(args.data_dir))
    from bet2_ablation import BackendUnavailable

    try:
        run_pipeline(benchmark=args.benchmark, slice=slice_, dataset_path=dataset_path,
                     dataset_sha256=sha, arms=args.arms.split(","), k=args.k,
                     real=args.real, cache_root=Path(args.cache_root),
                     results_dir=Path(args.results_dir), limit=args.limit,
                     embedder_name=args.embedder, stage=args.stage)
    except BackendUnavailable as exc:
        print(f"\nBACKEND UNAVAILABLE — no verdict (environment error, not FAIL):\n  {exc}",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
