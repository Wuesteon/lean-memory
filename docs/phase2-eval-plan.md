# Phase 2 — Public Benchmark Evaluation Plan (LoCoMo + LongMemEval)

Concrete engineering plan for running lean-memory against the two standard
agent-memory benchmarks under a **frozen-judge** discipline. A developer should be
able to read this and know exactly what to build, in what order, and what a result
file must contain.

Status: `Phase 2 — Public benchmarks (LongMemEval / LoCoMo + frozen judge)` is `⬜`
in [ARCHITECTURE.md](../ARCHITECTURE.md). This document is the build plan for that row.

---

## Motivation

Every quality number lean-memory can currently cite is on a small hand-built set:

- **BET-2** extraction ablation: n=97 gold cases, goldset sha256 `350b18b51a97fe57`.
- **BET-1 / `smoke_quality.py`**: a 5-fact toy corpus (Top-1 = 4/5 with real models).

These were built to find engine bugs, and they did — the BET-2 process caught four
real engine bugs (`extends` unreachable, resolver threshold miscalibration,
`OllamaTyper` label-set anchoring, router over-escalation) before any number was
trusted. That is exactly the discipline we want to carry forward: **the benchmark's
first job is to find every engine flaw before we trust a number from it.** But n=97
and a 5-fact probe are sanity checks, not a publishable claim. ARCHITECTURE.md's
first Known Limitation says so explicitly: "No benchmark-grade quality claim."

Meanwhile the numbers competitors publish are **vendor-self-reported and disputed**.
Independent re-evaluations have found gaps of ~30 points against the scores a vendor
reported for itself. The single largest driver of that gap is **unpinned eval
configuration**: which judge model graded the answers, what the judge prompt said,
and which backbone LLM generated the answers. When those three variables float, the
number is not comparable to anything.

A frozen-judge eval on LoCoMo and LongMemEval would:

1. **Give lean-memory a credible public number** — reproducible, config-pinned,
   comparable to published systems *under the same conditions*.
2. **Test the supersession machinery on real data at scale** — LoCoMo runs up to 35
   sessions per conversation; the ADD-only monotemporal spine has never been driven
   at that depth.
3. **Enable apples-to-apples comparison** — same backbone, same judge, same k, so a
   score difference reflects the *memory engine*, not the harness.

---

## What to Build

Five pieces. Build 1→2→3 to get an end-to-end score; 4 upgrades quality; 5 chooses
where to point it first.

### 1. Ingest adapter

lean-memory's public surface is exactly two calls (see `Memory` in
`src/lean_memory/memory.py`, README quickstart):

```python
mem.add(namespace, text)                 # extract + supersede + index one utterance
mem.search(namespace, query, k=..., is_latest_only=..., as_of=...)  # → list[RetrievedFact]
```

The adapter maps each benchmark's conversation structure onto this surface:

- **One namespace per conversation / chat history.** Each namespace is its own
  SQLite file (per-tenant isolation — see ARCHITECTURE "Per-namespace SQLite files"),
  so conversations never cross-contaminate and the corpus can be wiped per-conversation
  by deleting one file.
- **Iterate turns in chronological order**, calling `mem.add(namespace, utterance)`
  once per turn. Ingest order matters: supersession is temporal, so a later-session
  fact must be added *after* the earlier fact it supersedes.
- **Multi-session handling.** LoCoMo conversations span up to **35 sessions**. Flatten
  sessions in order, preserve each session's timestamp, and pass it through so the
  monotemporal spine (`valid_at`) reflects real world-time — this is what makes
  `as_of` point-in-time queries meaningful (see the Key Experiment and the temporal
  slice below). Set `t_ref` per turn from the session timestamp rather than wall-clock
  ingest time; `smoke_quality.py::_store_raw_fact` shows the `t_ref` plumbing.
- **LongMemEval** uses the identical pattern: one namespace per chat history, add each
  history turn in order.

Data sources:
- LoCoMo: <https://github.com/snap-research/locomo>
- LongMemEval: <https://github.com/xiaowu0162/longmemeval>

Deliverable: `bench/phase2_ingest.py` — takes a benchmark JSON path, yields
`(namespace, ordered_turns, questions)` and drives `mem.add` per turn. It must NOT
call any private path except the documented `t_ref` plumbing; the whole point is to
score the real public API.

### 2. Reader LLM

lean-memory returns **retrieved facts, not answers**. `mem.search` yields
`RetrievedFact` objects; the fields the reader consumes are `h.fact.fact_text` and
`h.final_score` (README quickstart iterates exactly these). A reader LLM turns the
top-k facts into the final answer graded by the judge.

- **Backbone model: `gpt-4.1-mini`.** This matches the backbone a competitor
  (MemMachine) used, so a score difference reflects the memory engine and retrieval,
  not a stronger/weaker answer generator. The backbone is a pinned variable in the
  result file (see Frozen Judge).
- **Reader prompt (frozen, verbatim):**
  - system: `"Answer the question using only the provided facts. If none of the facts answer the question, say 'I don't know'."`
  - user: a facts block (the top-k `fact_text` values, one per line, in ranked order)
    followed by the question.
- **No chain-of-thought, no retries, temperature 0.** The reader is an instrument, not
  a place to squeeze points; any cleverness here contaminates the comparison.

Deliverable: `bench/phase2_reader.py` — `answer(question, hits) -> str`. Pluggable so
the backbone can be swapped (and the swap recorded), mirroring lean-memory's
offline-stub pattern: an offline echo-reader for plumbing tests, the real
`gpt-4.1-mini` reader behind a `--real` flag.

### 3. Frozen judge

Reuse the BET-2 discipline wholesale. The BET-2 harness already demonstrates
frozen-goldset + sha256-pinning + joint-gate structure
(`bench/bet2_goldset.py`, `bench/bet2_ablation.py`). Apply the same rules here:

- **Pin the judge LLM and its exact prompt.** Record both, verbatim, in the config.
- **sha256-pin the eval config.** BET-2 hashes its goldset (`goldset_hash`, currently
  `350b18b51a97fe57`) and asserts frozen thresholds (`FROZEN_HIGH_SIM=0.80`,
  `FROZEN_LOW_SIM=0.45`, `FROZEN_CONF_THRESHOLD=0.5`) *before* scoring. Do the same:
  hash `{judge_model, judge_prompt, backbone_model, k, is_latest_only, benchmark_slice}`
  and print the first 16 hex chars at the top of every run, exactly as
  `bet2_ablation.py::report` prints `goldset sha256: …`.
- **LLM-as-a-Judge with an explicit rubric — not "grade generously."** The rubric must
  enumerate what counts as correct (exact answer, acceptable paraphrase, superset with
  the right value, `I don't know` when no fact supports an answer) and what does not.
  For temporal questions the rubric must require the *point-in-time-correct* answer,
  not merely a value that was ever true.
- **Log the three disputed variables in the result file: `judge_model`, `judge_prompt`,
  `backbone_model`.** These are precisely the variables that produced the ~30-point
  gap between a vendor's self-reported score and the independent re-eval. If they are
  not in the file, the number is not comparable and must not be published.

Deliverable: `bench/phase2_judge.py` — `grade(question, gold, predicted) -> bool`
plus a config dataclass that is hashed and serialized into the result file. Follow
`bet2_ablation.py`'s "refuse to emit a verdict when the environment isn't real / the
CI straddles the gate" posture: if the judge backend is unreachable, abort with
guidance (BET-2's `BackendUnavailable` pattern), never silently score 0.

### 4. Real model extras

The offline stubs (`FakeEmbedder` / `IdentityReranker` / `StubTyper`) are
deterministic but semantically meaningless — they only prove plumbing. Real numbers
require the real backends, opt-in exactly as `smoke_quality.py --real` loads them:

```bash
pip install 'lean-memory[models]'        # Qwen3-Embedding-0.6B + Ettin-32M reranker
pip install 'lean-memory[extract]'       # GLiNER2 candidate generation (richer extraction)
pip install 'lean-memory[llm]'           # LLM typing pass
ollama pull qwen2.5:3b                    # local model for the [llm] typing pass
```

- `[models]` is the accuracy lever — BET-1 shows Top-1 jumping 1/5 → 4/5 with
  Qwen3-Embedding-0.6B + Ettin-32M and zero code changes.
- Use **`Qwen/Qwen3-Embedding-0.6B`**, not `embeddinggemma-300m` — the Gemma repo is
  gated (license-accept) and Qwen3 is the stronger retrieval model (MTEB-R 64.65 vs
  62.49).
- The harness must expose a `--real` flag following the `smoke_quality.py` /
  `bet2_ablation.py` template: offline default runs the plumbing check and refuses a
  published verdict; `--real` loads the extras and produces the actual number. Record
  the exact embedder, reranker, extractor, and typer model IDs in the result file.

### 5. Slice focus (start here, not the full benchmark)

Do **not** lead with the full benchmark. Lead with the two slices where lean-memory's
architecture predicts differentiation, so the first result tests a hypothesis rather
than producing an undifferentiated aggregate.

- **LongMemEval — Knowledge Updates (KU).** This is the dimension the ADD-only
  supersession machinery was *designed for*: a fact changes over the conversation and
  the system must answer with the current value while the stale value stays indexed
  (`is_latest=False`, `superseded_by` pointer). **Highest-signal first experiment** —
  it directly exercises the core architectural bet and feeds the Key Experiment below.
- **LoCoMo — temporal reasoning.** The dimension where GPT-4 lags humans (~73%).
  lean-memory's `as_of` point-in-time query (world-time interval predicate over the
  monotemporal spine) is directly relevant: the reader can be asked "what was true at
  time T?" and the engine can filter to facts valid at T rather than only the latest.
- **Full benchmark second.** Once the slices are clean and the harness has shaken out
  its bugs, run the complete LongMemEval ability breakdown and full LoCoMo task set to
  produce the headline aggregate.

---

## Slice Strategy

| Order | Slice | Why first | Engine feature exercised |
|---|---|---|---|
| 1 | LongMemEval **Knowledge Updates** | The supersession machinery's reason to exist | ADD-only `superseded_by`, `is_latest` de-ranking |
| 2 | LoCoMo **temporal reasoning** | Known hard dimension (GPT-4 ≈73% vs humans) | `as_of` world-time interval query |
| 3 | Full LongMemEval (all abilities) | Headline per-ability number | full pipeline |
| 4 | Full LoCoMo (all tasks) | Headline per-task number | full pipeline, 35-session depth |

Ship slices 1–2 with a written result file before touching 3–4. If a slice surfaces
an engine bug (the expected BET-2-style outcome), fix the engine and re-freeze the
config before re-running.

---

## The Key Experiment

**Open empirical question:** does lean-memory's immutable ADD-only model *hurt* top-k
retrieval accuracy by polluting the result set with stale-but-superseded facts (which
remain indexed with `is_latest=False`)? Or does the structured de-ranking
(`is_latest` filter + salience-decay `0.6·rel + 0.2·recency + 0.2·importance`) cleanly
surface the correct current fact?

This is directly measurable. On the **KU slice**, run the identical eval twice,
changing exactly one knob on `mem.search`:

| Arm | `search(...)` call | Meaning |
|---|---|---|
| A (default) | `is_latest_only=True` | superseded facts filtered out before ranking |
| B | `is_latest_only=False` | superseded facts compete in the top-k like any other |

Everything else held constant (same namespaces, same k, same reader, same frozen
judge, same backbone). **The score difference A − B is the cost/benefit of the
immutable model:**

- If A ≫ B, the `is_latest` filter is doing real work and the immutable index is safe
  because de-ranking cleanly hides stale facts.
- If A ≈ B, superseded facts are not polluting top-k even when eligible — the salience
  decay already buries them.
- If A < B (unexpected), the filter is discarding facts the reader actually needed
  (e.g. a question that wants the *former* value) — a finding about when to expose
  history to the reader.

Report A, B, and the delta with the same paired structure BET-2 uses for its ablation
(`paired_bootstrap_delta` over the same questions), so the delta CI is honest at
small n. This is the single most important number Phase 2 produces about lean-memory's
architecture.

---

## Existing Infrastructure to Reuse

Do not reinvent the discipline — it already exists and passed:

- **`bench/smoke_quality.py`** — the template for the `--real` flag, model loading
  (`SentenceTransformerEmbedder` + `CrossEncoderReranker`), the eval loop, and the
  `t_ref` write plumbing (`_store_raw_fact`). Copy this structure for the ingest
  adapter and reader.
- **`bench/bet2_ablation.py`** — frozen operating point asserted before scoring, joint
  gates never read alone, paired bootstrap CI (`paired_bootstrap_delta`), Wilson CI
  for rates (`wilson_ci`), `BackendUnavailable` abort-with-guidance (never a silent
  FAIL), and the "refuse a verdict when the CI straddles the gate / offline" posture.
  The Key Experiment's A-vs-B delta reuses `paired_bootstrap_delta` directly.
- **`bench/bet2_goldset.py`** — frozen, hashed goldset pattern; `goldset_hash`
  (current sha256 `350b18b51a97fe57`); `validate_goldset` / `lint_goldset` run at load
  time to abort loudly on a mis-built case rather than mis-scoring. Mirror this: hash
  the Phase 2 eval config and validate it at load.
- **The operating philosophy:** "the benchmark did its job — found every engine flaw
  before we trusted a number from it." Expect the first LoCoMo/LongMemEval runs to
  surface engine bugs. That is the benchmark working, not failing.

New files to add (keep them siblings under `bench/`, matching the existing layout):
`phase2_ingest.py`, `phase2_reader.py`, `phase2_judge.py`, and a top-level
`phase2_eval.py` runner that ties them together and writes the result file.

---

## Success Criteria

A run is a success when it produces a **result file** containing, at minimum:

- **sha256-pinned judge config** — the first-16-hex hash of
  `{judge_model, judge_prompt, backbone_model, k, is_latest_only, slice}`, printed at
  the top of the run exactly like BET-2 prints its goldset hash.
- **The three disputed variables, verbatim:** `judge_model`, `judge_prompt`,
  `backbone_model` (`gpt-4.1-mini`).
- **All engine settings:** embedder / reranker / extractor / typer model IDs,
  `k` value, `is_latest_only` setting, frozen retrieval thresholds.
- **Per-ability scores on LongMemEval** and **per-task scores on LoCoMo** (not just an
  aggregate) — the slice breakdown is where differentiation shows.
- **The Key Experiment delta** (A: `is_latest_only=True` vs B: `False`) on the KU
  slice, with a paired bootstrap CI.

Beyond the file existing:

- **Comparable numbers.** The scores can be placed next to published numbers *under
  the same backbone and the same judge conditions* — otherwise they are not published.
- **Architectural win (the hypothesis):** ideally a **KU score that beats Mem0's
  mutate-in-place approach**, demonstrating that ADD-only supersession + `is_latest`
  de-ranking retrieves the current fact at least as well as destructive update — while
  additionally preserving auditable history and point-in-time query, which
  mutate-in-place cannot offer.

---

## Open Questions

1. **Immutable-model retrieval cost** — the Key Experiment's central unknown: does
   `is_latest=False` clutter pollute top-k, or does structured de-ranking hide it
   cleanly? (Measured directly by the A-vs-B delta.)
2. **When should history be exposed to the reader?** If arm B ever beats arm A on
   questions about *former* values, the harness needs a policy for surfacing
   superseded facts to the reader (e.g. `as_of` on temporal questions) rather than a
   single global `is_latest_only`.
3. **Ingest fidelity vs `mem.add` extraction** — LoCoMo/LongMemEval utterances are
   conversational, not the clean predicate-shaped sentences the rules extractor fires
   on. How much recall does the 4-pass pipeline lose on messy real turns, and does the
   GLiNER2 (`[extract]`) pass close the gap? (BET-2's escalation-rate gate is the
   template for measuring this.)
4. **Backbone sensitivity** — `gpt-4.1-mini` matches MemMachine, but how much does the
   score move across backbones? Worth a one-off sensitivity run once slices 1–2 are
   clean, recorded as a separate config hash.
5. **35-session scale** — the monotemporal spine has never run at LoCoMo depth. Does
   supersession stay correct (and search stay fast) as a namespace accumulates dozens
   of sessions of superseded facts? Watch for latency and for stale facts leaking into
   top-k as the `is_latest=False` set grows.
6. **`k` selection** — what k maximizes the KU score without drowning the reader in
   irrelevant facts? Sweep k as a recorded config variable, not a silently tuned one
   (mirror BET-2's threshold sweep discipline, `--sweep`).

---

Last updated: 2026-07-02
