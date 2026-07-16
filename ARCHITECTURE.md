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
| Pass 3 — recall-biased router | ✅ | escalates low-conf / endpoint-scoped coref / possible-`derives` (2026-07 re-freeze: `prior_entity` trigger dropped — see "Escalation recalibration") |
| Pass 4 — LLM constrained typing | ✅ | `StubTyper` (offline) / `OllamaTyper` (local model, `[llm]` extra) |
| Contradiction → supersession | ✅ | cheap-then-escalate: slot → cosine → subsumption → LLM |
| Salience at write | ✅ | deterministic heuristic, rated once + cached on the `Fact` |
| BET-2 ablation harness | ✅ | `bench/bet2_ablation.py` — **BET-2 PASS**; re-frozen 2026-07 at `(typing=0.4, conf=0.4)` (−0.4pp delta, 7.6% escalation), superseding the 2026-06-21 freeze — see "Escalation recalibration (2026-07)" below |

### Phase 1 — Integrations

| Component | Status | Notes |
|---|---|---|
| MCP server | ✅ | `memory_add` / `memory_search` / `memory_clear` via FastMCP (stdio transport) |
| Terminal demo agent | ✅ | `examples/chat.py` — full add→retrieve→supersede loop with Claude |

### Phase 3 — Sleep-Time Maintenance

Offline "sleep-time" job that cleans up stored memory between sessions, with a
staged human-review queue. Preserves the ADD-only spine and as-of semantics
(§3.1 visibility theorem incl. ingest commutation) — nothing is ever deleted.
Default-off, post-launch; no change to the first-run path. Design:
`docs/superpowers/specs/2026-07-16-sleep-time-maintenance-design.md`.

| Component | Status | Notes |
|---|---|---|
| Store verbs + `batch()` + `busy_timeout` | ✅ | `retire_duplicate`/`set_tier`/`iter_*` + unit-of-work; engine-wide busy_timeout (dedicated maintenance store at 5000 ms) |
| Schema v2 (user_version-gated 1→2 migration) | ✅ | `record_kind`, `fact_derivation`, `maintenance_run`/`maintenance_proposal` ledger; v1 fixture + upgrade round-trip test |
| Two ingest hooks (commutation) | ✅ | duplicate-cascade + summary-staleness cascade; exact no-ops until maintenance has ever run |
| DEDUP-EXACT / EVICT auto-band | ✅ | the only auto-applied transforms — as-of-safe in isolation and under later ingest |
| DEDUP-NEAR / SUMMARIZE / EVICT proposals | ✅ | judgment calls staged for human review; extractive stub default, `[llm]` Ollama opt-in; unreviewed proposals expire (30d) |
| Runner + lease + `MaintenanceConfig` | ✅ | atomic lease, heartbeat, work thresholds, cursor, crash-resume; frozen-dataclass config hashed per run |
| Proposal lifecycle | ✅ | CAS decide, one-transaction apply with target re-validation, stale-target + timeout expiry, explicit-only promotion |
| Tier-filtered retrieval | ✅ | default latest-mode hides cold; `as_of` never filters tier; `include_cold=True` opts out |
| `lean-memory-maintain` CLI + cron recipe | ✅ | dry-run default; `--apply`/`--auto-only`/`--json`; core package, no console dependency |
| MCP tools ×4 + prompt + plugin command | ✅ | `memory_maintenance_run/_status`, `memory_review_queue/_decide` on all three MCP surfaces; `review-memory-maintenance` prompt + `/review-memory` command; opt-in auto-spawn (`LM_MAINT_AUTO=1`) |
| Console Review UI (WP10b) | ⬜ | next-morning click-through page — separate packet, starts after this merges |

### Phase 2 — Next

| Item | Status |
|---|---|
| Public benchmarks (LongMemEval / LoCoMo + frozen judge) | ⬜ deferred (post-launch) — harness complete, see `docs/superpowers/specs/2026-07-08-strategic-direction-design.md` |
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

> **Superseded by the 2026-07 re-freeze** at operating point
> `(typing=0.4, conf=0.4)` — see "Escalation recalibration (2026-07)" below.
> The 2026-06-21 run below stays as the original PASS record; the frozen
> constants and escalation rate quoted here (`conf 0.5`-era, 10.1%) are historical.

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

### Escalation recalibration (2026-07)

The Phase 2 ingest surfaced that BET-2's <20% escalation gate held only on clean
goldset sentences: on **real LongMemEval conversational turns** the router routed
**95.9%** of candidates to the LLM (baseline probe, `bench/results/calibration/2026-07-escalation-baseline.json`;
best point typing=conf=0.3, 971/1012). Two confidence-independent router floors
drove it — `coreference` 65.6% and `prior_entity` 54.8% — so no threshold pair
could reach the target. Both were retired at the router:

- **Endpoint-scoped coref/ellipsis** — the pronoun/demonstrative scan narrowed
  from the whole `fact_text` to the predicate endpoints. On real turns
  `coreference` collapsed **664 → 1** (`2026-07-granularity-sweep.json`).
- **`prior_entity` trigger dropped** (two user-approved amendments: subject-only,
  then removed entirely) — subject re-mention of a known entity is normal
  discourse, not a hard case, and it stayed at 52.8% of real candidates even
  scoped to the subject (`2026-07-escalation-postfix.json`). Entity linking is
  deterministic by name; ambiguous refs still escalate via `coreference`,
  inferential edges via `derives`.

Post-drop probe on real turns (8 namespaces / 192 turns / 704 candidates,
`2026-07-escalation-postdrop-p{1..4}.json`) selected the operating point:

| typing | conf | escalated/seen | rate | by_reason |
|---:|---:|---:|---:|---|
| **0.40** | **0.40** | **103/704** | **14.6%** | derives 102, coref 1 |
| 0.50 | 0.50 | 316/704 | 44.9% | pre_flagged 247, low_conf 247, derives 102, coref 1 |

Only `(0.4, 0.4)` clears the margin (raising either threshold to 0.5 pulls in the
247 candidates with model confidence in [0.4, 0.5)); the residual is
derives-dominated (the irreducible inferential edges the LLM must own).

**Granularity** (`2026-07-granularity-sweep.json`): GLiNER `DEFAULT_THRESHOLD`
0.1 → 0.4 cut over-generation **8.43 → 3.67 facts/turn** (decision rule: smallest
swept threshold meeting facts/turn ≤ 4). The `median_fact_len ≤ 160` target was
waived (user-approved) as threshold-insensitive — it sits at 171–187 chars across
the entire sweep, so no GLiNER threshold moves it.

**Re-frozen constants:** `DEFAULT_TYPING_THRESHOLD = 0.4`, router
`conf_threshold = 0.4`, `FROZEN_CONF_THRESHOLD = FROZEN_TYPING_THRESHOLD = 0.4`
(`bench/bet2_goldset.py`). **BET-2 three-gate revalidation at the frozen point
(`bench/bet2_ablation.py --real`, full output `2026-07-bet2-revalidation.txt`):**

| Gate | Target | Result | Verdict |
|---|---|---|---|
| 1 — direct-bucket paired F1 delta | upper ≤ 3.0pp | −0.4pp [−1.2, +0.0]pp | ✅ PASS |
| 2 — escalation rate (goldset, Wilson upper) | < 20% | 7.6% [3.5%, 15.6%] | ✅ PASS |
| 3 — hybrid derives-recall not worse | ≥ LLM − 10pp | 0.20 ≥ 0.10 | ✅ PASS |

**BET-2 re-freeze = PASS** (all three gates jointly). Metric B resolver macro-F1
unchanged at **0.897** (the router change touches only the typer/direct path).

---

## Bugs Found by the Benchmark

The measurement process found 4 real engine bugs before they shipped.

| # | Bug | Fix |
|---|---|---|
| 1 | **`extends` unreachable** — every non-identical object mapped to `supersedes` | Additive signal routing: additive cues ("also", multi-valued predicates) → `extends`; functional slots still supersede |
| 2 | **Resolver thresholds miscalibrated** — `HIGH=0.82`/`LOW=0.55` put Qwen3 refinements in the wrong band | Recalibrated to `HIGH=0.80`/`LOW=0.45`; verified 4/4 on real Qwen3 |
| 3 | **`OllamaTyper` label-set mismatch** — offering all 4 relations to a typer with no prior-slot context caused qwen2.5:3b to anchor on `extends` for everything | Constrained to `{asserts, derives}` |
| 4 | **Router `prior_entity` over-escalation** — "user" always in `known_entities` after turn 1, causing 73.7% false escalation | `self_entity` exemption in `_references_prior_entity()`; expanded `_KNOWN_PREDICATES`; gold set rebalanced 37→97 cases — **superseded 2026-07:** the `prior_entity` trigger was dropped entirely after real-turn data showed it still fired on 52.8% of candidates (see "Escalation recalibration (2026-07)") |

---

## Design Decisions

### ADD-only writes / supersession

Facts are never deleted or updated in place. A contradicting fact marks the old one `is_latest=False` via a `superseded_by` pointer and inserts the new one. This makes the full history auditable and enables point-in-time queries (`as_of=<timestamp_ms>`).

### Offline-first with pluggable backends

Every component has an offline stub that needs zero downloads and produces deterministic output. Real models are opt-in extras. This means the full offline test suite runs in seconds with no network access, and the architecture is validated independently of model availability.

### Cheap-then-escalate contradiction detection

The resolver tries slot match → cosine similarity → token subsumption before calling the LLM. Each step is ~100× cheaper than the next. Only genuinely ambiguous cases (high cosine, disjoint tokens) reach the LLM. The BET-2 gate 2 verifies the escalation rate stays under 20%.

### Per-namespace SQLite files

Each namespace (e.g. per-user) gets its own SQLite file rather than a shared database with a namespace column. This gives hard write isolation, trivial backup/export (copy one file), and clean multi-tenant semantics with no cross-tenant query risk.

---

## Known Limitations

- **No benchmark-grade quality claim.** All numbers are on small hand-built sets. A real claim needs LongMemEval/LoCoMo + a frozen judge (Phase 2, deferred post-launch).
- **Recency on historical corpora — addressed (2026-07).** `exp(-λ·age)` collapses to ~0 for every fact when historical data is read years later, so the 0.2 recency term was dead weight on such corpora. `Memory.search(now=...)` now forwards an explicit search-time anchor so callers evaluating historical data can restore the recency signal; the wall-clock default is unchanged for live use.
- **Vectors stored float32, not int8.** sqlite-vec 0.1.9's int8 insert path is broken upstream. Flip when fixed (~0.2pt quality cost per BET-1, no correctness impact).
- **Inference typing is second-class without `[llm]`.** On the default `[mcp,models,extract]` path there is no Ollama typer, so the ~15% of candidates that escalate — almost entirely inferential (`derives`) facts — fall through to the deterministic `StubTyper` rather than a model. Assertional facts are unaffected; inference-type facts are second-class until you add the `[llm]` extra, which routes that escalated tier through real constrained typing.
- **Small-model ceiling.** qwen2.5:3b caps Typer accuracy. A larger local model would likely lift derives-recall. Untested.
- **Single machine, single run** for all real numbers. Reproducible, but not multi-seed/multi-judge as the spec's BET-5 demands.

---

## Reproduce

```bash
# Offline (full suite, no downloads)
pip install -e '.[dev]' && pytest -q

# Real retrieval quality
pip install -e '.[models]'
python bench/smoke_quality.py --real --embedder Qwen/Qwen3-Embedding-0.6B

# Real BET-2 ablation
pip install -e '.[models,extract,llm]'
ollama serve & ollama pull qwen2.5:3b
python bench/bet2_ablation.py --real --decodes 3
```
