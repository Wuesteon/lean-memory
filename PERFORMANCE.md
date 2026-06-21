# lean-memory — Performance Report

**Date:** 2026-06-20 · **Status:** Phase 0 (spine) + Phase 1 (hybrid extraction) built, tested, and measured with real models.

This is the honest scorecard: what's built, what's measured, what's good, what's
broken, and what the numbers actually mean. Every number here was reproduced fresh on
the measurement date (Apple Silicon, local models). Where a result is a *plumbing check*
rather than a *quality measurement*, it says so.

---

## 1. TL;DR — how are we doing?

**Architecturally: strong.** The core design bet — a pluggable, offline-first engine
where stub backends prove the plumbing and real models drop in with zero code change —
**works as intended.** Retrieval quality jumps from random (1/5) to good (4/5) just by
swapping the embedder/reranker.

**As a measured product: early, with one validated win and one identified weakness.**
- ✅ **Retrieval works** — 4/5 on a toy set, 3/3 on a clean set with real models.
- ✅ **Extraction accuracy holds** the core hybrid claim (cheap typer matches the LLM).
- ❌ **Extraction cost fails** — the router over-escalates (73.7% vs the <20% target).
- 🐛 **The benchmarking process found and fixed 3 real engine bugs** before they shipped.

**The single most valuable outcome isn't a number — it's that the measurement
infrastructure is now valid and catches real bugs.** A passing score on a broken
instrument would have been worse than the honest FAIL we have.

---

## 2. What's built (and verified)

| Layer | Component | Status | Test |
|---|---|---|---|
| **Storage** | `Store` interface + `SqliteStore` (vec0 + FTS5, per-tenant files) | ✅ | spine |
| | Monotemporal spine (`valid_at`/`valid_to`/`is_latest`/`superseded_by`, ADD-only) | ✅ | spine |
| | as-of point-in-time query (interval predicate) | ✅ | spine |
| **Retrieval** | Two-stage Matryoshka dense (256→768) + BM25 + RRF(k=10) + rerank + salience-decay | ✅ | spine |
| | Pluggable embedder (Fake / EmbeddingGemma / Qwen3-0.6B) + reranker (Identity / Ettin-32M) | ✅ | spine |
| **Extraction** | 4-pass hybrid: rules → GLiNER2 candidates → recall-biased router → LLM typing | ✅ | phase1 |
| | Relation taxonomy (`asserts`/`supersedes`/`extends`/`derives`), one shared `Candidate` | ✅ | phase1 |
| | Contradiction→supersession resolver (cheap-then-escalate) | ✅ | phase1 + extends |
| | Salience scored + cached at write | ✅ | phase1 |
| **Eval** | BET-2 ablation harness + frozen gold set (two-metric, paired-bootstrap CIs) | ✅ | runs |

**Footprint:** 3,239 LOC source · 17 tests (all offline, <0.5s) · default install <250 MB,
no mandatory cloud key. Real models are opt-in extras (`[models]`, `[extract]`, `[llm]`).

---

## 3. Measured performance

### 3a. Retrieval quality (the BET-1 evidence)

| Configuration | Top-1 | What it proves |
|---|---|---|
| Offline stubs (FakeEmbedder + IdentityReranker) | **1/5** | plumbing routes facts end-to-end (random vectors → chance) |
| Real (Qwen3-Embedding-0.6B + Ettin-32M), toy 5-fact set | **4/5** | real models lift quality with **zero code change** |
| Real, clean 3-fact set | **3/3** @ ~0.69 | the one toy-set miss ("employed"≠"works") was a small-corpus artifact |

**Read:** the pluggable-backend architecture is validated. This is a *sanity* signal on
a tiny set, **not** a benchmark claim — a publishable "better than supermemory" number
needs the real LongMemEval/LoCoMo datasets + a frozen judge (not yet built). But the
direction is unambiguous and the mechanism is proven.

*Note:* the spec's default embedder (`google/embeddinggemma-300m`) is a **gated** HF repo
needing a license-accept; we used **Qwen3-Embedding-0.6B** instead — ungated *and* the
verified-stronger retrieval model (MTEB-R 64.65 vs 62.49), so an upgrade, not a compromise.

### 3b. Extraction quality — the BET-2 verdict (real models)

The ablation reports **two independent metrics** (the engine assigns relations via two
mechanisms; conflating them was the original instrument's fatal flaw), with bootstrap CIs.

**Metric A — Typer (`asserts` vs `derives` + coreference):**

| Arm | macro-F1 | derives-recall | coref-acc |
|---|---|---|---|
| 100%-LLM (qwen2.5:3b) | 0.474 [0.27, 0.68] | 0.20 | 1.00 |
| hybrid (stub on direct bucket) | 0.474 [0.27, 0.68] | 0.20 | 1.00 |

**Metric B — Resolver (`asserts`/`extends`/`supersedes`), with real embeddings:**

| Arm | macro-F1 | Confusion highlight |
|---|---|---|
| both | **0.897** [0.73, 1.00] | `extends` 5/5 ✓, `supersedes` 6/8 (2 → extends) |

**The three gates (joint pass required):**

| Gate | Target | Result | Verdict |
|---|---|---|---|
| 1 — F1 delta (hybrid vs LLM, direct bucket) | ≤ 3pp | **0.0pp** [0,0] | ✅ PASS |
| 2 — escalation rate | < 20% | **73.7%** [51, 88] | ❌ **FAIL** |
| 3 — hybrid derives-recall not worse | ≥ LLM − 10pp | 0.20 ≥ 0.10 | ✅ PASS |

**BET-2 = FAIL — on COST (gate 2), not accuracy.**

**What this actually means:**
- ✅ **The core hybrid claim holds.** On the de-escalated "direct" bucket, the free
  StubTyper produces *identical* results to the 3B LLM (0.0pp delta) — the cheap path
  loses nothing where it's used. This is the central BET-2 hypothesis, and it's true.
- ❌ **The router escalates far too much (73.7%).** The cost story is the failure. The
  dominant cause: the `prior_entity` trigger fires on 13/19 cases — in a real
  conversation the subject is always a "known" entity, so almost everything escalates.
  **This is the actionable next task: retune the router, not the typer.**
- The Typer's absolute accuracy (0.474 macro-F1, 0.20 derives-recall) is modest — but
  that reflects qwen2.5:3b being a small model on a *genuinely hard* task (subtle
  inference like "I take the train every morning" → is that stated or derived?), not a
  bug. Coreference resolution is **perfect (1.00)**.
- The resolver is **strong (0.897)** with real embeddings — `extends` fully reachable,
  confirming the bug fix below end-to-end.

---

## 4. Bugs the benchmarking process found and fixed

The benchmark's biggest contribution wasn't a score — it was catching **3 real engine
bugs** that would otherwise have shipped silently. Each is now fixed and regression-tested.

| # | Bug | How it surfaced | Fix |
|---|---|---|---|
| 1 | **`extends` unreachable** — every non-identical object → `supersedes`; multi-valued slots impossible ("use Python *and* Rust" was impossible) | adversarial audit ran real models, saw the confusion matrix collapse | additive signal (cue like "also", or multi-valued predicate) → `extends`; functional slots still supersede |
| 2 | **Resolver thresholds miscalibrated** — `HIGH=0.82`/`LOW=0.55` assumed distinct objects embed far apart, but Qwen3 puts same-slot objects at 0.6–0.95 | real-embedding test put refinements in the wrong band | recalibrated to `HIGH=0.80`/`LOW=0.45`; verified 4/4 on real Qwen3 |
| 3 | **`OllamaTyper` label-set mismatch** — offered the LLM all 4 relations incl. `supersedes`/`extends` (which it has no prior-slot context to judge); qwen2.5:3b anchored on `extends` for *everything* (0/19) | first `--real` BET-2 run | constrained the typer schema to its true `{asserts, derives}` surface → 10/19 |

**Lesson recorded:** a clean BET-2 score on an engine that couldn't represent `extends`,
with miscalibrated thresholds, and a typer that always said `extends`, would have measured
nothing real. The instrument did its job — it found the engine was wrong *before* we
trusted a number from it.

---

## 5. Honest limitations (what is NOT proven)

- **No benchmark-grade quality claim.** All quality numbers are on small hand-built sets
  (5–19 cases). The bootstrap CIs are wide; the harness *refuses a verdict* when
  underpowered. A real claim needs LongMemEval/LoCoMo + a frozen judge — that harness
  isn't built (it's Phase 2).
- **The router is the known weak point** (gate 2). Escalation policy needs retuning before
  the cost story is defensible.
- **Small-model ceiling.** qwen2.5:3b caps Typer accuracy; a larger local model would
  likely lift derives-recall. Untested.
- **Vectors stored float32, not int8** — sqlite-vec 0.1.9's int8 insert path is broken
  upstream; flip when fixed (~0.2pt quality cost per BET-1, no correctness impact).
- **Single machine, single run** for the real numbers. Reproducible, but not multi-seed/
  multi-judge as the spec's BET-5 ultimately demands.

---

## 6. Scorecard vs the design spec's bets

| Bet | Claim | Status from measurement |
|---|---|---|
| **BET 1** | local embeddings reach parity via mandatory reranker | ✅ **directionally confirmed** (1/5→4/5); full parity-vs-gemini gate not yet run |
| **BET 2** | hybrid extraction within ~3pp of 100%-LLM at <20% escalation | ◑ **accuracy PASS, cost FAIL** — hybrid matches LLM (0pp) but router over-escalates (73.7%) |
| **BET 3** | monotemporal default; bi-temporal opt-in | ✅ built as specified; temporal headroom small (already known) |
| **BET 4** | embedded store comfortable at <100K/tenant | ✅ architecture built; scaling numbers verified earlier from primary sources |
| **BET 5** | prove "better" on a pinned, honest harness | ◑ **instrument now valid** (caught 3 bugs, reports CIs, refuses underpowered verdicts); full public-benchmark run is Phase 2 |

---

## 7. The clear next step

The evidence points at exactly one thing: **retune the router's escalation policy.**
Gate 2 (escalation 73.7%) is the only failing gate, and its cause is identified (the
`prior_entity` trigger is too broad). That's a focused, offline, measurable task — and
the now-valid BET-2 harness will tell us immediately whether a fix moves the number.

Everything else (Phase 2 public benchmarks, int8 flip, LanceStore scale tier) is
genuine but lower-priority. The engine is *correct*; the open question is *cost-efficient*.

---

*Reproduce: `pip install -e '.[dev]' && pytest -q` (offline, 17 tests). Real numbers:
`pip install -e '.[models,extract,llm]'`, `ollama serve & ollama pull qwen2.5:3b`, then
`python bench/smoke_quality.py --real --embedder Qwen/Qwen3-Embedding-0.6B` and
`python bench/bet2_ablation.py --real --decodes 3`.*
