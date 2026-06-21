# lean-memory

An embedded, local-first agent-memory engine. No server, no daemon, no mandatory
cloud key. `pip install` and go.

This is **Phase 0 (spine) + Phase 1 (hybrid extraction)** from
[`lean-memory-design-spec.md`](../lean-memory-design-spec.md). It is runnable and
tested end-to-end (17 tests, all offline), with the real models behind opt-in
extras (see [Status](#status-what-is-real-vs-stubbed)). It exists to prove the
architecture, not to win benchmarks yet.

> **📊 How are we performing? → [`PERFORMANCE.md`](PERFORMANCE.md)** — the full honest
> scorecard: retrieval 1/5→4/5 with real models, the BET-2 verdict (accuracy holds,
> cost gate fails), the 3 engine bugs the benchmark caught, and what's next.

## Quickstart

```python
from lean_memory import Memory

mem = Memory(root="./data")          # each namespace → its own SQLite file (per-tenant isolation)

mem.add("user-42", "I work at Acme Corp.", t_ref=1_700_000_000_000)
mem.add("user-42", "I work at Globex now.", t_ref=1_700_000_100_000)  # supersedes Acme (ADD-only)

# current state (default: is_latest only)
mem.search("user-42", "where does the user work?")        # → "I work at Globex now."

# point-in-time: what held at world-time T (interval predicate over valid_at/valid_to)
mem.search("user-42", "employer", as_of=1_700_000_060_000, is_latest_only=False)  # → "...Acme..."
```

## What's implemented (Phase 0)

| Component | Status | Notes |
|---|---|---|
| `Store` interface | ✅ | single abstraction; `SqliteStore` is the only impl in Phase 0 |
| `SqliteStore` (vec0 + FTS5) | ✅ | one file per namespace (BET 4 write-isolation) |
| Monotemporal spine | ✅ | `valid_at`/`valid_to` + `is_latest` + ADD-only `superseded_by`; nothing is ever deleted |
| Two-stage Matryoshka dense | ✅ | coarse 256-dim KNN → exact 768-dim re-score (256 = verified retrieval knee, BET 1) |
| BM25 sparse arm | ✅ | FTS5 `bm25()` |
| RRF fusion (k=10) | ✅ | `Σ 1/(10 + rank)` |
| Mandatory rerank | ✅ *(interface)* | `IdentityReranker` (offline default) / `CrossEncoderReranker` (Ettin-32M) |
| Salience-decay re-score | ✅ | `0.6·rel + 0.2·recency + 0.2·importance`, `recency = exp(-λ·age)` |
| as-of temporal query | ✅ | world-time interval predicate |
| Embedder interface | ✅ | `FakeEmbedder` (offline) / `SentenceTransformerEmbedder` (EmbeddingGemma, Qwen3-0.6B) |

### Phase 1 (hybrid extraction)

| Component | Status | Notes |
|---|---|---|
| Relation taxonomy | ✅ | `asserts`/`supersedes`/`extends`/`derives`; single shared `Candidate` contract (`taxonomy.py`) |
| Pass 2 — candidate generation | ✅ | `StubCandidateGenerator` (offline) / `Gliner2Generator` (GLiNER2, `[extract]` extra) |
| Pass 3 — recall-biased router | ✅ | escalates low-conf / coref / cross-turn / possible-`derives`; **logs escalation rate** (BET-2 metric) |
| Pass 4 — LLM constrained typing | ✅ | `StubTyper` (offline) / `OllamaTyper` (local model, `[llm]` extra) |
| Contradiction → supersession | ✅ | cheap-then-escalate: slot → cosine → subsumption → (LLM); `SUPERSEDES` retires, `EXTENDS` co-valid |
| Salience at write | ✅ | deterministic heuristic, rated once + cached on the `Fact` |
| BET-2 ablation harness | ✅ | `bench/bet2_ablation.py` — hybrid vs 100%-LLM, reports Relation-F1 + escalation rate vs the ≤3pp / ≤20% gates |

## Status: what is real vs stubbed

**The plumbing is real and tested; ranking *quality* needs the real models.**

- The **default backends are offline stubs** so the engine runs with zero downloads:
  - `FakeEmbedder` — deterministic hash→vector. Reproducible, but **semantically meaningless**.
  - `IdentityReranker` — a no-op that preserves fusion order.
  - With these, the offline quality bench scores ~1/5 — that is *expected*: it only
    proves facts route end-to-end, not that they rank well.
- The **rules extractor** (`RulesExtractor`) only fires on a handful of hard-coded
  predicates (`works_at`, `lives_in`, `likes`, …). GLiNER2 + the LLM-typing residual
  are Phase 1.
- **Vectors are stored float32, not int8.** The spec targets int8 (size win, ~0.2pt
  quality cost per BET 1), but `sqlite-vec` 0.1.9's int8 *insert* path is broken; flip
  to int8 once upstream fixes it. Does not affect correctness.

### Turning on real quality

```bash
pip install 'lean-memory[models]'      # sentence-transformers + torch
python bench/smoke_quality.py --real   # EmbeddingGemma + Ettin-32M; Top-1 should jump from 1/5
python bench/smoke_quality.py --real --embedder Qwen/Qwen3-Embedding-0.6B   # the BET 1 head-to-head
```

The `--real` jump is the first concrete evidence for **BET 1** ("the reranker is the
accuracy lever that neutralizes the local-embedding deficit"). Picking the default
embedder/reranker is a **harness decision**, not an assumption — run both.

### Measured results (2026-06, real models on this machine)

Ran all tiers with real models (Qwen3-Embedding-0.6B + Ettin-32M + GLiNER2 + Ollama
qwen2.5:3b). **Note:** `google/embeddinggemma-300m` is a **gated** HF repo (needs a
license-accept + login); Qwen3-Embedding-0.6B is ungated *and* the verified-stronger
retrieval model (MTEB-R 64.65 vs 62.49), so it's the friction-free default to test.

| Test | Stub (offline) | Real models | Read |
|---|---|---|---|
| Retrieval (`smoke_quality.py`) | 1/5 | **4/5** | ✅ plumbing was correct; real models lift quality with **zero code change** |
| Retrieval (clean 3-fact corpus) | — | **3/3** @ ~0.69 | ✅ the one miss in 4/5 was a toy-corpus "employed≠works" artifact |
| BET-2 ablation (`bet2_ablation.py --real`) | F1≈0.2 both arms | F1 0.056 (LLM) / 0.163 (hybrid) | ⚠️ **negative finding — the gold set is not yet a valid instrument** (see below) |

**The BET-2 result was a real, useful negative — and rebuilding the instrument found
two engine bugs.** The original 8-example harness measured near-noise (isolated
sentences, context-dependent labels). An elite-panel rebuild (`bench/bet2_ablation.py`
+ `bench/bet2_goldset.py`: two-mechanism, slot-context, paired-bootstrap CIs, refuses a
verdict when arms are identical) produced a *methodologically valid* instrument — and
**validating it surfaced two real `ContradictionResolver` bugs**, now fixed:

1. **`extends` was unreachable** — every non-identical object mapped to `supersedes`, so
   the engine could not represent a multi-valued slot (you couldn't "use Python *and*
   Rust"). Fixed: a deterministic additive signal (additive cue like "also", or an
   inherently multi-valued predicate) routes a distinct co-valid object to `extends`;
   functional slots (works_at, lives_in) still supersede. (`test_contradiction_extends.py`)
2. **Thresholds miscalibrated to real embeddings** — `HIGH=0.82`/`LOW=0.55` assumed
   distinct objects embed far apart, but Qwen3 puts same-slot objects at 0.6–0.95.
   Recalibrated to `HIGH=0.80`/`LOW=0.45`; verified 4/4 on real Qwen3 (refinement→extends,
   replacement→supersedes, additive→extends).

Net: the benchmark did its job — it found correctness bugs *before* producing a number.
A clean BET-2 score on an engine that couldn't represent `extends` would have measured
the wrong thing.

### The BET-2 verdict (real models, valid instrument, 2026-06)

After the dataset iteration (hard-direct cases that make the arms diverge; coref as a
real resolution task) and a **third engine bug fix it surfaced** — `OllamaTyper` offered
the LLM all four relations including `supersedes`/`extends`, which the typer has no
prior-slot context to judge, so qwen2.5:3b anchored on `extends` for *everything* (0/19);
constraining the typer schema to its true `{asserts, derives}` surface fixed it (→ 10/19)
— the harness produces a real verdict:

| Gate | Result | Meaning |
|---|---|---|
| 1 — F1 delta ≤3pp (hybrid vs 100%-LLM, direct bucket) | ✅ **PASS** (0.0pp) | the cheap typer matches the LLM on de-escalated facts — **the core hybrid claim holds** |
| 2 — escalation <20% | ❌ **FAIL** (73.7%) | the router escalates far too much |
| 3 — hybrid derives-recall not worse | ✅ PASS | hybrid loses no derives vs the LLM |

**BET-2 = FAIL — but on COST (gate 2), not accuracy.** The actionable finding: the
router's `prior_entity` trigger escalates almost everything (13/19 cases) because in a
real conversation the subject is always a "known" entity. The hybrid *accuracy* story is
sound; the *escalation policy* needs retuning (the `prior_entity` heuristic is too broad).
Metric B (resolver) scores macro-F1 **0.897** with real embeddings — `extends` fully
reachable (5/5), confirming the earlier fix. The instrument now measures real things and
points at a real next task: retune the router, not the typer.

## Develop

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q          # 17 tests, all offline, <1s
```

## Layout

```
src/lean_memory/
  types.py              Episode / Entity / Fact / RetrievedFact
  memory.py             Memory facade (ingest + search; per-tenant store cache)
  store/
    base.py             Store interface
    schema.py           SQLite + vec0 + FTS5 DDL
    sqlite_store.py     default store (two-stage Matryoshka dense + BM25)
  embed/
    base.py             Embedder + matryoshka_truncate()
    fake.py             FakeEmbedder (offline default)
    sentence_transformer.py   EmbeddingGemma / Qwen3-0.6B (lazy, [models] extra)
  extract/
    rules.py            Phase 0 rules-only extractor (regex + dateparser)
    taxonomy.py         relation taxonomy + the shared Candidate contract (Pass 2-4 currency)
    gliner_extractor.py Pass 2 — Stub / GLiNER2 candidate generation
    router.py           Pass 3 — recall-biased router (logs escalation rate)
    llm_typer.py        Pass 4 — Stub / Ollama constrained typing
    contradiction.py    cheap-then-escalate contradiction → supersession resolver
    salience.py         deterministic salience-at-write scorer
  retrieve/
    rerank.py           Reranker interface; Identity + Ettin-32M cross-encoder
    retriever.py        the pipeline: dense+sparse → RRF → rerank → salience-decay
tests/
  test_spine.py         Phase 0 end-to-end spine tests
  test_phase1_extraction.py  Phase 1 hybrid-extraction tests
bench/
  smoke_quality.py      tiny retrieval quality harness (seed of the Phase 2 eval)
  bet2_ablation.py      BET-2 ablation: hybrid vs 100%-LLM (Relation-F1 + escalation)
```

## Next (Phase 2+, per the spec roadmap)

1. ~~**Phase 1 — hybrid extraction**~~ ✅ done (offline). **Run the BET-2 ablation with
   real backends:** `pip install '.[extract,llm]'`, start ollama, then
   `python bench/bet2_ablation.py --real` on a labelled LongMemEval MR/TR/KU slice —
   the verdict (does hybrid stay within ≤3pp of 100%-LLM? is escalation <20%?) needs
   real models; the offline run only proves the metric plumbing.
2. **Phase 2 — eval + de-risk:** pinned LongMemEval + LoCoMo harness, frozen judge,
   gemini-embedding-001 control. Run the BET 1 gate and the BET 3 temporal ablation.
3. Flip vectors to int8 once sqlite-vec fixes the insert path.
4. `LanceStore` (scale tier) behind the same `Store` interface.

See [`../lean-memory-design-spec.md`](../lean-memory-design-spec.md) for the full
rationale, every load-bearing decision, and the stress-test verdicts behind them.
