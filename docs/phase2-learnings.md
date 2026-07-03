# Phase 2 Learnings — a postmortem for the next researcher

Date: 2026-07-03. Status: benchmark runs suspended by choice; harness complete;
engine flaws found and partially fixed. Companion docs:
`docs/phase2-eval-plan.md` (the original plan),
`docs/superpowers/specs/2026-07-02-phase2-eval-harness-design.md` (approved design),
`docs/superpowers/phase2-HANDOFF.md` (operational runbook + fix backlog).
Everything below happened in roughly 24 hours on branch `phase2-eval-harness`.

## The goal

Produce lean-memory's first credible public benchmark numbers on LongMemEval
(Knowledge-Updates slice, 78 questions) and LoCoMo (temporal slice, 321
questions) under a frozen-judge discipline: sha256-pinned configs, verbatim
transcriptions of each benchmark's standard judge, pinned backbone
(`gpt-4.1-mini` via OpenRouter), and the "Key Experiment" — an A/B on
`is_latest_only` measuring whether the immutable ADD-only supersession model
pollutes top-k retrieval or de-ranks stale facts cleanly.

A stated premise of the plan: *"the benchmark's first job is to find every
engine flaw before we trust a number from it."* That premise turned out to be
the whole story. We stopped before producing headline numbers because the
benchmark kept finding engine flaws whose fixes would invalidate any number
produced along the way — scoring an engine you already know you will change is
paying twice.

## What was built (and stands, reusable)

- `bench/phase2_ingest.py` — adapters for both benchmarks (both LongMemEval
  on-disk shapes; LoCoMo speaker/photo handling; count-anchor validation),
  cached crash-safe resumable ingest with telemetry, dataset download with
  sha256 pinning. Public-API-only ingest (`mem.add(ns, text, t_ref=…)`).
- `bench/phase2_reader.py` — frozen reader prompts, offline echo stub,
  OpenRouter reader with transport-only retries.
- `bench/phase2_judge.py` — three judges: LongMemEval official templates and
  Mem0's LoCoMo judge transcribed **character-for-character** (golden-tested
  against upstream), plus a strict rubric; hashed `EvalConfig` carrying the
  three disputed variables (judge model, judge prompt, backbone) verbatim.
- `bench/phase2_eval.py` — staged resumable runner (ingest → copy-per-arm →
  read → judge → aggregate), refuses to aggregate incomplete runs, offline mode
  refuses verdicts, paired-bootstrap key-experiment stats.
- 91 passing tests; a 5-question end-to-end real shakeout passed (retrieval →
  reader → official judge → result file).
- Datasets downloaded and pinned; our LoCoMo file's sha256 matches the
  independent audit's hash exactly (`79fa87e9…ea698ff4`).

## What we assumed vs. what was true

| # | Assumption | Reality |
|---|---|---|
| 1 | Ingest of the KU slice costs ~5–10 h locally | ~57 h serial. The pipeline's LLM-typing pass consumed 77% of wall time (profiled: 9.5 s/turn serial, of which 7.3 s typing) |
| 2 | Router escalation on real data ≈ BET-2's 10.1% (<20% design gate) | **96.7%** of candidates escalate on conversational turns (512 candidates / 60 turns: pre_flagged 425, low_confidence 425, coreference 354, prior_entity 254, derives 74). GLiNER2 confidences run low on messy text, so both confidence gates (generator `typing_threshold` and router `conf_threshold`, both 0.5) fire on nearly everything. The "hybrid cheap-then-escalate" pipeline is a 100%-LLM pipeline on real data |
| 3 | Parallel local workers scale ingest | Two separate failures: (a) a single decode stream saturates M2 Max Metal — `OLLAMA_NUM_PARALLEL=6` gave only 1.1× aggregate, so 4 workers queued on one lane and ran *slower* than serial; (b) 6 workers × 8 default torch threads = 48 threads on 12 cores halved throughput until capped at 2/worker (`OMP_NUM_THREADS=2`), which doubled it back |
| 4 | A remote CUDA GPU fixes throughput | Partially. A10G: 3.8 s/call solo, **1.63 s effective at 6-way** (CUDA batches; Metal can't). But sustained corpus rate still decayed to ~10 turns/min because of #5 — infra cannot outrun a per-turn LLM tax |
| 5 | Typing-prompt cost is constant per call | The prompt embedded **all** known entities; conversational data creates ~5 entities/turn (1,896 by turn 401 of one namespace). Calls inflated 7.4 s → 28.5 s and then **silently truncated past the model's context window** — a correctness defect, not just latency. Fixed with a most-recent-100 cap; even capped, saturated calls cost ~15 s, so the real floor is the escalation rate (#2) |
| 6 | `t_ref` needs a private plumbing path (plan doc said so) | Wrong in our favor: `Memory.add` exposes `t_ref` publicly; the adapter needed zero private paths |
| 7 | `as_of` point-in-time queries work | The BM25 sparse arm ignored `as_of` entirely — out-of-window facts leaked through lexical matches. Fixed + regression-tested before any run |
| 8 | Salience/recency decay helps bury stale facts | Dead weight on historical corpora: with 2023 data read in 2026, `exp(-λ·age)` ≈ 0 for every fact, so the 0.2 recency term de-ranks nothing. (Side effect: the A/B experiment becomes a *cleaner* read of the `is_latest` filter alone) |
| 9 | Our resumable harness was crash-safe | Original resume re-ingested unfinished namespaces into their existing partial DBs, silently **duplicating facts**. Found only because a real crash forced a real resume. Fixed + tested |
| 10 | An HF Space is a stable ollama host | Three distinct failure modes in one night: container restart loses ephemerally-pulled models (fix: bake the model into the image); deploys wedge in `RUNNING_BUILDING` (fix: heal the live container instead of waiting); transient proxy 500s kill single requests (fix: bounded retry — typing is deterministic at temperature 0) |
| 11 | Published memory-benchmark numbers are comparable | Confirmed worse than assumed: vendor self-reports swing 25–54 points under independent re-evaluation (Zep 58→75→84 by config alone; EverMemOS 92.3 self-reported vs 38.4 reproduced; ~6.4% of LoCoMo gold answers are simply wrong). This *validated* the frozen-judge design — pin judge model, judge prompt, backbone, or the number is meaningless |

## Chronology of attempts (with measured rates)

| Attempt | Setup | Result |
|---|---|---|
| Serial local ingest | 1 process, M2 Max, local ollama | ~6.3 turns/min → 100 h projected. Stopped |
| 4 parallel local workers | shared local ollama | **7 turns/min aggregate** (worse per-process than serial — single Metal decode lane); 1 shard crashed on queue timeout. Stopped |
| Remote A10G typing, 6 workers | HF Space, `OLLAMA_NUM_PARALLEL=8` | 24–30 turns/min early; decayed to 15.3 (thread oversubscription) |
| + torch thread caps (2/worker) | same | back to 30 turns/min briefly |
| Reality at namespace depth | same | sustained **~10 turns/min** → 60 h ETA, because prompts saturate the 100-entity cap ~30 turns into every namespace and each turn still pays an LLM call (97% escalation). **This is where we stopped**: the bottleneck is the engine's calibration, not infrastructure |

Total spent: ~$16 HF GPU, <$1 OpenRouter, three complete namespaces of corpus
(discarded on principle: one corpus, one engine version).

## What didn't work (root causes, not symptoms)

1. **Throwing parallelism at a serialized bottleneck.** 77% of per-turn time
   funneled through one LLM decode lane. Metal doesn't batch; adding workers
   just built a queue. Measure the pipeline *before* scaling it.
2. **Trusting a gate measured on clean data.** BET-2's 10.1% escalation was
   real — on goldset-style predicate sentences. The same gates hit 96.7% on
   conversational text. Distribution shift invalidated the operating point.
3. **Ephemeral state on managed containers.** Anything a Space must have on
   restart must be in the image (or a persistent volume), not pulled at runtime.
4. **Masked exit codes.** `python … | grep` returns grep's status; a shard
   crash looked like success and cost an hour of phantom progress. `pipefail`
   or no pipes on anything long-running.
5. **Unbounded prompt construction.** Any prompt assembled from an append-only
   store grows until it breaks something silently. Cap at the source.

## What we fixed (all TDD, all committed on `phase2-eval-harness`)

| Commit | Fix |
|---|---|
| `fdea1b5` | Engine: sparse BM25 arm honors `as_of` (same interval predicate as dense arm) + regression tests |
| `39078b2` | Engine: known-entities handed to router/typer capped at 100 most recent (prompt inflation + silent truncation) |
| `e990c0b` | Harness: crash-resume wipes partial namespace DBs (fact duplication); remote typer re-pulls digest-pinned model on loss |
| `0339de0` | Harness: bounded retry on transient 5xx/connect errors (deterministic calls, safe to retry) |
| `2596d3a` | Harness: OpenRouter retries narrowed to transport errors only ("content never retried") |

Plus reproducibility bookkeeping that proved its worth repeatedly: the typer
host **and model manifest digest** (`357c53fb…`) are recorded in every ingest
manifest, so corpora cannot silently mix engines, hosts, or weights.

## What the next researcher should do (in order)

1. **Recalibrate the escalation operating point** — the single highest-leverage
   fix. Probe escalation vs. (`typing_threshold`, `conf_threshold`) on real
   LongMemEval turns (offline StubTyper probe, minutes, no LLM needed), pick a
   point near the <20% design target, validate with the existing
   `bench/bet2_ablation.py --sweep --real` gates, re-freeze. Ingest becomes
   ~5–8× cheaper and the hybrid design does what it claims.
2. **Decide extraction granularity** — fact_text is often a full utterance
   (~8 facts/turn); calibrate the GLiNER threshold alongside (1).
3. **Fix recency anchoring** — forward `now` through `Memory.search` or anchor
   decay to corpus time; today the term is dead on any historical dataset.
4. **Then re-run Phase 2 unchanged.** The harness needs nothing new: fresh
   config hash, ingest (~10–15 h with sane escalation, or any CUDA box), then
   read/judge/aggregate are ~1 h and ~$10 of API calls. All commands are in the
   handoff doc.

## Meta-lesson

The plan's discipline (frozen configs, pinned judges, refusal postures,
telemetry-first ingest) did exactly what it was designed to do — it just did it
*earlier* in the pipeline than expected. Every incident either produced a
committed fix with a regression test or a measured finding with numbers. The
run we stopped was not a failure of the benchmark; it was the benchmark
succeeding before the scoreboard turned on.
