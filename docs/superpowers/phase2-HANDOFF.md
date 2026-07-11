# Phase 2 Handoff — state, remaining work, and knowledge (2026-07-03)

> **STATUS CHANGE (2026-07-03 ~13:00): benchmark runs SUSPENDED by user decision.**
> Sustained ingest rate stabilized at ~10 turns/min (ETA ~60 h) because the
> engine's escalation calibration routes ~97% of conversational candidates to
> LLM typing — the benchmark surfaced the flaw; running it to completion before
> fixing the engine buys an expensive number for an engine we already know we
> will change. Workers stopped, HF Space PAUSED (billing off), partial KU corpus
> kept under `bench/.phase2_cache/lme_ku_shards/` (3 complete namespaces).
> **Next phase: fix the exposed engine flaws first (backlog below), re-freeze,
> then re-run the slices** — the harness (Tasks 1–12) is done and stands ready.

> **UPDATE (2026-07-08+):** backlog items 1–3 fixed on `launch-gate`
> (endpoint-scoped coref, granularity + escalation re-freeze, search-time `now`).
> Benchmark re-runs are DEFERRED past the MCP launch per
> `docs/superpowers/specs/2026-07-08-strategic-direction-design.md`. Secrets
> rotated. The runbook below remains valid for the eventual re-run.

## Engine-fix backlog (from benchmark findings, in priority order)

1. **Escalation calibration** — both confidence gates (generator
   `typing_threshold`, router `conf_threshold`, both 0.5) escalate ~97% of
   conversational candidates. Fix: probe escalation vs thresholds on real LME
   turns (offline StubTyper probe, fast), pick an operating point near the
   design target (<20%), validate with `bet2_ablation --sweep --real` and the
   three gates, re-freeze. This alone makes ingest ~5–8× cheaper.
2. **Extraction shape** — fact_text is often a whole utterance (~8 facts/turn);
   related to GLiNER over-generation at low threshold. Decide the intended
   granularity and calibrate `threshold` alongside (1).
3. **Recency decay anchored to wall-clock** — `exp(-λ·(now−valid_at))` ≈ 0 for
   all facts on historical corpora; the 0.2 recency weight is dead. Options:
   forward `now` through `Memory.search` (public param), or anchor decay to the
   namespace's max(valid_at). Design decision needed.
4. **`touch()` mutates reads / non-idempotent search** — known; harness works
   around it (copy-per-arm). Consider making access-tracking opt-in.
5. **Typing-call cost** — even at sane escalation rates, ~7 s per constrained
   decode on a 3B model is the floor; consider smaller candidate batches,
   explicit `num_ctx`, or a faster constrained-decode path (engine work).
6. Review-deferred minors listed in `.superpowers/sdd/progress.md` (per task).

Items 1–2 are one calibration effort. The Phase 2 re-run afterward reuses the
harness unchanged (new config hash = new freeze; that is the designed flow).

Session handoff for the Phase 2 benchmark effort. Spec:
`docs/superpowers/specs/2026-07-02-phase2-eval-harness-design.md`. Plan (15 tasks):
`docs/superpowers/plans/2026-07-02-phase2-eval-harness.md`. Incident-level detail:
`.superpowers/sdd/progress.md` (gitignored scratch — read it before resuming).

## Current state

- **Tasks 1–12 complete** on branch `phase2-eval-harness` (all code + datasets +
  real shakeout). Full test suite: 91 green.
- **Task 13 IN PROGRESS**: KU ingest running as **6 detached worker processes**
  (they survive Claude session death; started ~2026-07-03 morning, ETA ~26 h from
  09:00, i.e. ~2026-07-04 mid-morning). Workers ingest 78 namespaces into
  `bench/.phase2_cache/lme_ku_shards/shard_{0..5}/`, typing offloaded to a
  private HF Space. **Nothing monitors them once this session ends** — check
  manually (commands below).
- **HF Space `wuesteon1337/lm-typer-phase2` is RUNNING and BILLING (~$1.05/h,
  a10g-small)** until paused. Total projected GPU spend $40–50.

## What is left to do

1. **Finish Task 13 (KU slice)** — after all 6 shards exit 0
   (check `bench/.phase2_cache/lme_ku_shards/logs/exits.log` = six `exit: 0` lines):
   1. Merge shards: `.venv/bin/python bench/.phase2_cache/merge_shards.py`
   2. Read/judge/aggregate (needs `OPENROUTER_API_KEY` exported, key file below):
      ```
      .venv/bin/python bench/phase2_eval.py --benchmark longmemeval --slice ku --arms a,b,fc --real --stage read
      .venv/bin/python bench/phase2_eval.py --benchmark longmemeval --slice ku --arms a,b,fc --real --stage judge
      .venv/bin/python bench/phase2_eval.py --benchmark longmemeval --slice ku --arms a,b,fc --real --stage aggregate
      ```
   3. Copy `hypotheses_*/verdicts_*` JSONLs from the cache dir next to the result
      file in `bench/results/phase2/`, commit (plan Task 13 steps 4–5, incl. the
      sanity-review checklist: supersessions > 0, escalation telemetry, abstention
      hypotheses eyeballed).
2. **Task 14 (LoCoMo temporal)** — ingest is NOT sharded (only 10 namespaces;
   plain `--stage ingest` with the remote-typer env vars is fine, ~4–6 h), then
   the same read/judge/aggregate flow (judges run automatically as
   lenient+strict), commit results.
3. **PAUSE THE SPACE** when LoCoMo ingest is done (billing stops):
   `python -c "from huggingface_hub import HfApi; HfApi(token=open('bench/.phase2_cache/hf.token').read().strip()).pause_space('wuesteon1337/lm-typer-phase2')"`
   Do this EARLY if you decide not to continue the runs.
4. **Task 15 (docs)** — ARCHITECTURE.md Phase 2 row + results section (styled like
   BET-2's), docs/benchmarks.md "measured results" section, phase2-eval-plan.md
   status. Include the engine findings below.
5. **Final whole-branch review** (superpowers flow: requesting-code-review) over
   `main..phase2-eval-harness`, address findings, then merge decision
   (finishing-a-development-branch). Deferred Minor findings are listed in the
   ledger per task.
6. **Rotate secrets** (they passed through chat): the OpenRouter key and the HF
   write token. Files: `bench/.phase2_cache/openrouter.key`,
   `bench/.phase2_cache/hf.token` (both gitignored).

## Operating the ingest (fresh session cheat-sheet)

- Progress: count `"done": true` in `bench/.phase2_cache/lme_ku_shards/shard_*/manifest.json`;
  live turn counts: `SELECT COUNT(*) FROM episode` per `shard_*/​*.db` (read-only URI).
- A crashed shard (nonzero line in `exits.log`) is safe to relaunch — resume
  wipes partial namespaces (idempotent). Relaunch command for shard `$i`:
  ```
  ( export PHASE2_OLLAMA_HOST=https://wuesteon1337-lm-typer-phase2.hf.space \
           PHASE2_OLLAMA_TOKEN=$(cat bench/.phase2_cache/hf.token) \
           OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 VECLIB_MAXIMUM_THREADS=2 \
           TOKENIZERS_PARALLELISM=false && \
    caffeinate -i .venv/bin/python bench/.phase2_cache/shard_ingest.py $i 6 \
      > bench/.phase2_cache/lme_ku_shards/logs/shard_$i.log 2>&1 ; \
    echo "shard $i exit: $?" >> bench/.phase2_cache/lme_ku_shards/logs/exits.log ) &
  ```
- Thread caps are load-bearing (without them: 48 torch threads on 12 cores → rate
  halves). The Mac must stay awake (lid open or `pmset disablesleep`).
- If the Space loses its model again the typer self-heals (re-pull + retry);
  transient proxy 5xx are retried with backoff. Only repeated hard failures kill
  a shard — and relaunching is always safe.

## Knowledge gathered (documented where?)

**Engine findings — must appear in the Task 15 docs and result-file notes:**
1. **Sparse-arm `as_of` leak** (FIXED, commit `fdea1b5` + regression test):
   BM25 arm ignored the temporal predicate; point-in-time queries leaked
   out-of-window facts via lexical matches.
2. **Router escalation 96.7% on conversational data** (MEASURED, not fixed —
   answer to plan open question 3): recall-biased routing has no cheap path on
   messy turns (by_reason: pre_flagged 425, low_confidence 425, coreference 354,
   prior_entity 254, derives 74 / 512 candidates over 60 real turns). BET-2's
   <20% gate held only on clean goldset sentences. Follow-up: recalibrate the
   GLiNER threshold / router operating point (needs BET-2 re-freeze).
3. **Unbounded known-entities prompt growth** (FIXED, commit `39078b2` + test):
   conversational data creates ~5 entities/turn; at ~1,900 names the typing
   prompt went 7.4 s → 28.5 s and silently truncated past the model context —
   latency AND correctness defect at 35-session scale (answers open question 5).
4. **Recency decay is dead weight on historical corpora** (MEASURED): with
   2023-dated data read in 2026, `exp(-λ·age)` ≈ 0 for every fact — the 0.2
   recency term cannot de-rank anything; arm B's stale facts are de-ranked by
   relevance alone. Makes the Key Experiment a clean read of `is_latest`.
5. **Harness resume duplication** (FIXED, commit `e990c0b` + test): re-ingesting
   an unfinished namespace previously duplicated episodes/facts silently.
6. **Extraction shape on real data** (MEASURED): fact_text is often a full
   utterance, not a distilled predicate (~8 facts/turn from GLiNER
   over-generation) — affects retrieval granularity; quantified in the ingest
   telemetry that lands in the result file.

**Operational knowledge (this doc + ledger):** M2 Max Metal saturates at 1 decode
stream (parallel ollama slots gain 1.1×; CUDA A10G gains 2.3× at 6-way);
HF Spaces: model must be baked into the image (ephemeral storage loses pulls on
restart), deploys can wedge in RUNNING_BUILDING (restart + heal live container),
transient proxy 500s happen; torch thread caps are mandatory for multi-worker
CPU inference; pipeline exit codes must not be masked by grep (`pipefail`).

**Frozen-config bookkeeping:** remote typing host + model digest
(`357c53fb659c…`) are recorded in each shard manifest's `engine` dict; remote
ollama is 0.31.1 vs local 0.30.10 (same blob — note in result-file notes).
Engine re-freeze happened mid-Task-13 (commit `39078b2`): the KU corpus now
ingesting was started AFTER the fix — no namespace mixes engine versions.

## Cost meter (approximate)

- HF GPU: ~13 h turbulence + ~26 h KU + ~5 h LoCoMo ≈ **$45**
- OpenRouter (reader + judges + FC baselines, both slices): **~$10–15**
- Shakeout spent so far: <$1
