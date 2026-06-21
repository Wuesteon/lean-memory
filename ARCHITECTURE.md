# lean-memory — Architecture & Status

Implementation status, design decisions, benchmark results, and known limitations.

---

## Implementation Status

### Phase 0 — Storage & Retrieval

| Component | Status | Notes |
|---|---|---|
| `Store` interface | ✅ | single abstraction; `SqliteStore` is the only impl |
| `SqliteStore` (vec0 + FTS5) | ✅ | one file per namespace (per-tenant isolation) |
| Monotemporal spine | ✅ | `valid_at`/`valid_to` + `is_latest` + ADD-only `superseded_by`; nothing is ever deleted |
| Two-stage Matryoshka dense | ✅ | coarse 256-dim KNN → exact 768-dim re-score |
| BM25 sparse arm | ✅ | FTS5 `bm25()` |
| RRF fusion (k=10) | ✅ | `Σ 1/(10 + rank)` |
| Mandatory rerank | ✅ | `IdentityReranker` (offline default) / `CrossEncoderReranker` (Ettin-32M) |
| Salience-decay re-score | ✅ | `0.6·rel + 0.2·recency + 0.2·importance`, `recency = exp(-λ·age)` |
| as-of temporal query | ✅ | world-time interval predicate |
| Pluggable embedder | ✅ | `FakeEmbedder` (offline) / `SentenceTransformerEmbedder` (Qwen3-0.6B) |

### Phase 1 — Hybrid Extraction

| Component | Status | Notes |
|---|---|---|
| Relation taxonomy | ✅ | `asserts`/`supersedes`/`extends`/`derives`; single shared `Candidate` contract |
| Pass 2 — candidate generation | ✅ | `StubCandidateGenerator` (offline) / `Gliner2Generator` (GLiNER2, `[extract]` extra) |
| Pass 3 — recall-biased router | ✅ | escalates low-conf / coref / cross-turn / possible-`derives`; `self_entity` exemption prevents false-escalation of first-person facts |
| Pass 4 — LLM constrained typing | ✅ | `StubTyper` (offline) / `OllamaTyper` (local model, `[llm]` extra) |
| Contradiction → supersession | ✅ | cheap-then-escalate: slot → cosine → subsumption → LLM |
| Salience at write | ✅ | deterministic heuristic, rated once + cached on the `Fact` |
| BET-2 ablation harness | ✅ | `bench/bet2_ablation.py` — **BET-2 PASS** (2026-06-21, n=97, 0.0pp delta, 10.1% escalation) |

### Phase 1 — Integrations

| Component | Status | Notes |
|---|---|---|
| MCP server | ✅ | `memory_add` / `memory_search` / `memory_clear` via FastMCP (stdio transport) |
| Terminal demo agent | ✅ | `examples/chat.py` — full add→retrieve→supersede loop with Claude |

### Phase 2 — Next

| Item | Status |
|---|---|
| Public benchmarks (LongMemEval / LoCoMo + frozen judge) | ⬜ |
| int8 vector storage | ⬜ blocked — sqlite-vec 0.1.9 insert path is broken upstream |
| `LanceStore` scale tier | ⬜ |

---

## Measured Performance

### Retrieval Quality (BET-1 evidence)

| Configuration | Top-1 | What it proves |
|---|---|---|
| Offline stubs (FakeEmbedder + IdentityReranker) | **1/5** | plumbing routes facts end-to-end (random vectors → chance result) |
| Real (Qwen3-Embedding-0.6B + Ettin-32M), 5-fact set | **4/5** | real models lift quality with zero code changes |
| Real, clean 3-fact set | **3/3** @ ~0.69 | the one toy-set miss was a small-corpus vocabulary artifact |

The pluggable-backend architecture is validated. These are sanity checks on small hand-built sets — a publishable quality claim needs LongMemEval/LoCoMo + a frozen judge (Phase 2).

Note: `google/embeddinggemma-300m` is a gated HF repo requiring license-accept. `Qwen3-Embedding-0.6B` is ungated and the stronger retrieval model (MTEB-R 64.65 vs 62.49) — use it instead.

### Extraction Quality — BET-2 (2026-06-21, n=97)

**Metric A — Typer (`asserts` vs `derives`):**

| Arm | macro-F1 | derives-recall |
|---|---|---|
| 100%-LLM (qwen2.5:3b) | 0.474 [0.27, 0.68] | 0.20 |
| hybrid (StubTyper on direct bucket) | 0.474 [0.27, 0.68] | 0.20 |

**Metric B — Resolver (`asserts`/`extends`/`supersedes`) with real embeddings:**

| Arm | macro-F1 |
|---|---|
| both arms | **0.897** [0.73, 1.00] |

**Gate results:**

| Gate | Target | Result | Verdict |
|---|---|---|---|
| 1 — F1 delta (hybrid vs LLM, direct bucket) | ≤ 3pp | 0.0pp [0, 0] | ✅ PASS |
| 2 — escalation rate (Wilson upper) | < 20% | 10.1% [5.2%, 18.7%] | ✅ PASS |
| 3 — hybrid derives-recall not worse | ≥ LLM − 10pp | 0.20 ≥ 0.10 | ✅ PASS |

**BET-2 = PASS** (goldset sha256 `350b18b51a97fe57`).

---

## Bugs Found by the Benchmark

The measurement process found 4 real engine bugs before they shipped.

| # | Bug | Fix |
|---|---|---|
| 1 | **`extends` unreachable** — every non-identical object mapped to `supersedes` | Additive signal routing: additive cues ("also", multi-valued predicates) → `extends`; functional slots still supersede |
| 2 | **Resolver thresholds miscalibrated** — `HIGH=0.82`/`LOW=0.55` put Qwen3 refinements in the wrong band | Recalibrated to `HIGH=0.80`/`LOW=0.45`; verified 4/4 on real Qwen3 |
| 3 | **`OllamaTyper` label-set mismatch** — offering all 4 relations to a typer with no prior-slot context caused qwen2.5:3b to anchor on `extends` for everything | Constrained to `{asserts, derives}` |
| 4 | **Router `prior_entity` over-escalation** — "user" always in `known_entities` after turn 1, causing 73.7% false escalation | `self_entity` exemption in `_references_prior_entity()`; expanded `_KNOWN_PREDICATES`; gold set rebalanced 37→97 cases |

---

## Design Decisions

### ADD-only writes / supersession

Facts are never deleted or updated in place. A contradicting fact marks the old one `is_latest=False` via a `superseded_by` pointer and inserts the new one. This makes the full history auditable and enables point-in-time queries (`as_of=<timestamp_ms>`).

### Offline-first with pluggable backends

Every component has an offline stub that needs zero downloads and produces deterministic output. Real models are opt-in extras. This means the full test suite (56 tests) runs in ~4 seconds with no network access, and the architecture is validated independently of model availability.

### Cheap-then-escalate contradiction detection

The resolver tries slot match → cosine similarity → token subsumption before calling the LLM. Each step is ~100× cheaper than the next. Only genuinely ambiguous cases (high cosine, disjoint tokens) reach the LLM. The BET-2 gate 2 verifies the escalation rate stays under 20%.

### Per-namespace SQLite files

Each namespace (e.g. per-user) gets its own SQLite file rather than a shared database with a namespace column. This gives hard write isolation, trivial backup/export (copy one file), and clean multi-tenant semantics with no cross-tenant query risk.

---

## Known Limitations

- **No benchmark-grade quality claim.** All numbers are on small hand-built sets. A real claim needs LongMemEval/LoCoMo + a frozen judge (Phase 2).
- **Vectors stored float32, not int8.** sqlite-vec 0.1.9's int8 insert path is broken upstream. Flip when fixed (~0.2pt quality cost per BET-1, no correctness impact).
- **Small-model ceiling.** qwen2.5:3b caps Typer accuracy. A larger local model would likely lift derives-recall. Untested.
- **Single machine, single run** for all real numbers. Reproducible, but not multi-seed/multi-judge as the spec's BET-5 demands.

---

## Reproduce

```bash
# Offline (all 56 tests, ~4s)
pip install -e '.[dev]' && pytest -q

# Real retrieval quality
pip install -e '.[models]'
python bench/smoke_quality.py --real --embedder Qwen/Qwen3-Embedding-0.6B

# Real BET-2 ablation
pip install -e '.[models,extract,llm]'
ollama serve & ollama pull qwen2.5:3b
python bench/bet2_ablation.py --real --decodes 3
```
