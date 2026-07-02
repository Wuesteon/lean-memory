# Phase 2 Eval Harness — Design (approved 2026-07-02)

Implements `docs/phase2-eval-plan.md`: run lean-memory against LongMemEval and
LoCoMo under a frozen-judge discipline. This spec records the design as approved
in brainstorming; the implementation plan derives from it.

## Scope and decisions (agreed with user)

- **Scope now:** build the full harness (both benchmarks), verify offline, then
  run **two real slices**: LongMemEval Knowledge-Updates (KU, 78 questions) and
  LoCoMo temporal reasoning (category 2, 321 questions). Full benchmarks later.
- **LLM access:** OpenRouter (OpenAI-compatible endpoint), pinned upstream model
  IDs. `OPENROUTER_API_KEY` must be set before `--real` runs; provider recorded
  in the result file. Backbone: `openai/gpt-4.1-mini`.
- **Judges:** benchmark-standard judges verbatim, plus a strict second judge on
  LoCoMo (both LoCoMo numbers reported side by side, never blended):
  1. `lme-official` — LongMemEval `evaluate_qa.py::get_anscheck_prompt`
     templates verbatim (6 question types + `_abs` abstention branch), judge
     `openai/gpt-4o-2024-08-06`, temperature 0, max_tokens 10, label =
     `"yes" in output.lower()`.
  2. `locomo-lenient` — Mem0 `memory-benchmarks` judge prompt verbatim, judge
     `openai/gpt-4o-mini`, JSON `{reasoning, label}`, category-3 gold truncated
     at first `;`. The comparability number.
  3. `locomo-strict` — our enumerated rubric: correct value or unambiguous
     paraphrase only; temporal answers must be point-in-time correct — dates
     within ±1 day, durations within ±1 of the stated unit (vs the lenient
     14-day / 50% tolerances); no single-item partial credit on list answers;
     "I don't know" correct only when gold is unanswerable. Judge
     `openai/gpt-4o`. The honesty number.

## Why (from research, 2026-07)

- LongMemEval has an official frozen judge every comparable number uses
  (Zep 71.2, Supermemory 81.6, full-context gpt-4o 60.2 — all judge gpt-4o).
- LoCoMo has no standard judge; Mem0's lenient prompt is de facto and is the
  main driver of the 25–54-point vendor disputes (Zep 58→75→84 by config alone;
  EverMemOS 92.32 self-reported vs 38.38 reproduced). ~6.4% of LoCoMo gold
  answers are wrong (audited ceiling ≈93.6%). A full-context baseline on the
  same backbone is the honest ceiling reference.
- The three disputed variables — judge model, judge prompt, backbone — must be
  in the result file verbatim or the number is not publishable.

## Architecture

```
bench/
  phase2_ingest.py    # dataset download + sha256 verify, adapters → mem.add(ns, text, t_ref=..., source=...)
  phase2_reader.py    # frozen reader: EchoReader (offline) | OpenRouterReader (--real, gpt-4.1-mini)
  phase2_judge.py     # 3 judges + EvalConfig dataclass (sha256-pinned)
  phase2_eval.py      # runner CLI: ingest → retrieve+read → judge → aggregate
  results/phase2/     # result JSON + per-question JSONL (committed)
  .phase2_cache/      # datasets + ingested SQLite files (gitignored)
```

Discipline inherited from BET-2, not reinvented: `paired_bootstrap_delta` /
`wilson_ci` imported from `bet2_ablation.py` (plus one accuracy-flavored paired
bootstrap sibling using the same `BOOTSTRAP_SEED` pattern), `BackendUnavailable`
abort-with-guidance (exit 2, never a silent 0), offline mode that runs on stubs
but refuses a publishable verdict, config hash printed first like the goldset
hash.

### Data flow (staged, resumable)

1. **Ingest** each conversation/haystack once into `.phase2_cache/<dataset>/
   <ingest-config-hash>/` as per-namespace SQLite files + manifest (dataset
   sha256, engine config, git commit). The only expensive local stage.
2. **Arm setup** copies the cached `.db` files per arm. Required: `search()`
   mutates `last_access` via `touch()`, so arms sharing files would contaminate
   each other.
3. **Retrieve + read**: public `mem.search(...)` with the arm's knobs → top-k
   facts → reader → append `{question_id, hypothesis, hits}` to JSONL.
   Resumable: rerun skips already-answered ids.
4. **Judge**: reads that JSONL, grades, appends verdicts to a second JSONL
   (also resumable).
5. **Aggregate**: per-type scores + paired-bootstrap deltas → pinned result
   file. Only written when every question in the slice has a verdict.

## Ingest adapter (`phase2_ingest.py`)

**LongMemEval.** Canonical dataset `longmemeval_s_cleaned.json` (264 MB) from
HF `xiaowu0162/longmemeval-cleaned` via wget (the HF datasets-library loader is
broken on this repo); `longmemeval_oracle.json` (15 MB) supported as the
shakeout dataset. sha256 of the downloaded file recorded in the manifest and
result file. One **namespace per question_id** (each question has its own
haystack). Sessions ingested in file order (pre-sorted in `_s`); session
timestamp format `"2023/04/10 (Mon) 23:07"` → epoch-ms `t_ref`, **+1 s per turn**
within a session so supersession order is well-defined. Both roles ingested:
`source="user"` / `source="assistant"` (engine already down-weights non-user
salience). Handle both on-disk haystack shapes (oracle `{session_id, turns}`
objects; `_s` turn-arrays + parallel `haystack_session_ids` / `haystack_dates`)
— shapes confirmed in the user's prior gbrain_check adapter. Slice `ku` =
`question_type == "knowledge-update"` (78 questions, 6 of them `_abs`
abstention — kept in, as official QA eval does).

**LoCoMo.** `data/locomo10.json` raw from GitHub snap-research/locomo
(2,805,274 bytes; sha256-pinned; license CC BY-NC 4.0 — eval use, nothing
redistributed). One **namespace per sample_id** (10 conversations, 19–32
sessions each). Session timestamp `"1:56 pm on 8 May, 2023"` parsed via
`dateutil` → `t_ref` + the same +1 s per-turn increment. Turn text =
`f"{speaker}: {text}"`;
image turns append `f"{speaker} shared a photo: {blip_caption}"`. Slice
`temporal` = `category == 2` (321 questions). Category mapping (validated by
count anchors): 1=multi-hop(282), 2=temporal(321), 3=open-domain(96),
4=single-hop(841), 5=adversarial(446, excluded).

**Load-time validation** (validate_goldset-style, abort loudly): 500 LME
questions; LoCoMo counts above; timestamp parse failures are errors, not
warnings.

**Strictly public API**: only `mem.add(namespace, text, t_ref=..., source=...)`
— `t_ref` is public, so no private paths at all (correction to the plan doc,
which assumed the smoke_quality private plumbing was needed).

**Telemetry per namespace**: facts/turn, supersession count, router escalation
stats, add-latency p50/p95, DB size → result file (answers plan open questions
3 and 5). `--limit N` ingests a subset first to calibrate runtime; cache
persists so calibration work is kept.

## Reader (`phase2_reader.py`)

- Interface: `answer(question: str, hits: list[RetrievedFact]) -> str` (FC
  baseline variant takes the transcript instead of hits).
- `EchoReader` (offline): returns top-1 `fact_text`. Plumbing only.
- `OpenRouterReader` (`--real`): `openai/gpt-4.1-mini`, temperature 0,
  max_tokens 256, single attempt; transport errors retry with backoff, content
  never retried. No chain-of-thought.
- System prompt, verbatim (frozen): `"Answer the question using only the
  provided facts. If none of the facts answer the question, say 'I don't
  know'."`
- Facts block: one line per fact in ranked order, **each line carrying the
  fact's valid_at date**: `- [2023-05-08] <fact_text>`. Flagged deviation from
  the plan's bare lines: KU/temporal questions are unanswerable without dates,
  comparable harnesses feed dates, and `valid_at` is engine output under test.
  The exact rendered template is frozen into the config hash.
- **Full-context baseline arm (`fc`)**: same prompt shape, full transcript
  instead of facts. Run once per slice (~$4–8); the honest ceiling reference.

## Judges + config (`phase2_judge.py`)

- `grade(question, gold, predicted, qmeta) -> Verdict` per judge; judge calls
  cached in the stage-4 JSONL keyed by (question_id, judge_id).
- The three judges as listed under Decisions. Verbatim transcriptions carry
  their source URLs; golden tests pin the templates.
- `EvalConfig` dataclass, sha256-hashed, first 16 hex printed at the top of
  every run and embedded in the result file:
  `{benchmark, slice, dataset_file, dataset_sha256, judge_id, judge_model,
  judge_prompt (verbatim), backbone_model, provider, k, is_latest_only,
  reader_prompt (verbatim), embedder/reranker/extractor/typer model IDs,
  retrieval constants (W_REL/W_REC/W_IMP, RRF_K, OVER_RETRIEVE, DECAY_LAMBDA,
  resolver high/low sim, router conf_threshold), git_commit}`.
- Unreachable judge/reader backend → `BackendUnavailable` with guidance,
  exit 2. Never score 0 on error.

## Runner (`phase2_eval.py`)

CLI: `python bench/phase2_eval.py --benchmark {longmemeval,locomo} --slice
{ku,temporal} --arms a,b,fc --k 10 [--real] [--limit N]`. `k` defaults to 10.
Further slices (full benchmarks) are added later without CLI changes.

- **Arms**: A = `is_latest_only=True` (default), B = `False`, FC = full-context
  baseline. Identical cached ingest copied per arm; identical reader and judge.
- **Key Experiment (KU)**: report A, B, and A−B in points with paired-bootstrap
  95% CI over per-question correctness (accuracy sibling of
  `paired_bootstrap_delta`, same seed discipline). Engine finding to record:
  for 2023-dated data queried in 2026, `exp(-λ·age)` recency ≈ 0 for every
  fact, so arm B stale facts are de-ranked by relevance alone — A−B cleanly
  measures the `is_latest` filter itself.
- **LoCoMo temporal**: same A/B/FC arms, dual-judged (lenient + strict). The
  plan's `as_of` arm is **deferred**: LoCoMo questions carry no reference date
  to plug into `as_of`; any mapping would be a hand-tuned heuristic. Recorded
  as an open question in the result file.
- Offline default prints `PLUMBING CHECK ONLY — NO VERDICT`; `--real` required
  for a publishable number, exactly the BET posture.

## Engine findings & fix policy (pre-registered)

1. **Fix before real runs (TDD, regression test first):** `sparse_search`
   ignores `as_of` — the BM25 arm leaks facts outside the temporal window
   (`sqlite_store.py`; dense arm filters, sparse doesn't). `as_of` is the
   headline differentiator; it must not ship broken under a benchmark.
2. **Handled by harness design, recorded as limitations:** `touch()` mutates
   `last_access` on every search (→ copy-per-arm); `Memory.search` does not
   forward `now` so recency uses wall-clock (→ non-deterministic recency,
   near-zero for historical datasets — recorded, not patched).
3. Any further bug a slice surfaces: fix engine → re-freeze config hash →
   re-run slice (the BET-2 posture; expected outcome, not failure).

## Result file

`bench/results/phase2/<benchmark>_<slice>_<confighash16>.json`, committed.
Each arm records its own `EvalConfig` hash inside the file (arms differ in
`is_latest_only`); the filename uses arm A's hash. Contents:

- full `EvalConfig` + hash; git commit; dataset sha256; run timestamps
- per-arm overall + per-question-type / per-category scores with n and Wilson
  CIs
- Key Experiment: A, B, delta pp with paired 95% CI
- FC baseline score
- LoCoMo: lenient vs strict judge gap
- ingest telemetry (facts/turn, supersessions, escalation rate, add-latency
  p50/p95, DB sizes)
- pointers + sha256 of the per-question JSONLs

## Testing (TDD)

- Fixture mini-datasets: hand-built 2-question LME JSON in **both** on-disk
  shapes; 1-conversation LoCoMo JSON → adapter tests (namespaces, t_ref values,
  speaker prefixes, count-anchor aborts).
- Golden-string tests: judge prompts match official templates verbatim; reader
  facts-block rendering; config-hash stability (fixed config → known hash).
- Offline end-to-end on fixtures: resumability (kill + rerun skips done ids);
  arm isolation (running arm A must not change arm B's scores — the `touch()`
  regression).
- Engine fix: failing test for sparse-arm `as_of` leak, then fix.

## Execution plan & budget

1. Offline e2e green on fixtures.
2. Engine fix (sparse `as_of`) merged; config frozen.
3. Download datasets, record sha256s.
4. Shakeout: LME oracle, ~5 questions, `--real` end-to-end (~cents).
5. Calibrate: ingest 10 KU questions from `_s_cleaned`, extrapolate.
6. Full KU ingest (~5–10 h local, background) → arms A/B/FC → result file.
7. LoCoMo ingest (~30–60 min) → temporal arms, dual judge → result file.

API budget ≈ $10–15 total (both slices, FC baselines, dual judges). Local:
M2 Max, models already installed (`[models]`, `[extract]`, `[llm]` extras +
qwen2.5:3b present). Embedder pinned to `Qwen/Qwen3-Embedding-0.6B`.

## Out of scope (this cycle)

- Full LongMemEval / full LoCoMo runs (slices first, per plan).
- `as_of` reader policy for LoCoMo temporal (deferred, see above).
- k sweep beyond recording k=10 (sweep is a recorded follow-up).
- Backbone sensitivity run.
- Fixing `touch()` read-mutation or `now` forwarding in the engine.

## Key references

- LongMemEval: github.com/xiaowu0162/LongMemEval; HF
  `xiaowu0162/longmemeval-cleaned`; judge = `src/evaluation/evaluate_qa.py`.
- LoCoMo: github.com/snap-research/locomo (`data/locomo10.json`); de-facto
  judge = github.com/mem0ai/memory-benchmarks `benchmarks/locomo/prompts.py`;
  audit = github.com/dial481/locomo-audit.
- Published anchors (per-config, never cross-compared): Zep LME_S 71.2 /
  full-context gpt-4o 60.2 (judge gpt-4o); MemMachine LoCoMo 0.9169
  (gpt-4.1-mini backbone, judge gpt-4o-mini); Mem0 LoCoMo 66.88 (gpt-4o-mini
  backbone+judge); full-context LoCoMo ≈72.9 (Mem0 paper) / 92.62 with CoT
  prompt (audit).
