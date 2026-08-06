# Agent-Memory Competitive Landscape (2026)

## Overview

This document captures what the team learned about the 2026 agent-memory
competitive landscape. It is written as durable project knowledge for developers
joining lean-memory, not as a research dump. The goal is to explain the design
space, where the notable systems sit within it, and — critically — where
lean-memory occupies a position that no competitor does.

The single most important takeaway: **the field has largely converged on two
architectural bets — write-time extraction and retrieval-time / ground-truth
preservation — and both of them mutate or delete memory when facts change.**
lean-memory is a third position that keeps history immutable. That difference is
the reason we can answer questions the others structurally cannot.

A second takeaway, equally important for anyone reading vendor material: **every
headline accuracy number in this space is self-reported by the vendor or paper
authors, and independent re-evaluations have found large discrepancies.** Treat
the benchmark numbers below as reference points, never as a leaderboard.

## Architectural Taxonomy

Memory systems for agents differ primarily along three axes:

1. **When the expensive work happens.**
   - *Write-time extraction*: run LLM extraction/reconciliation as messages
     arrive, storing distilled memories. Cheaper, faster retrieval; lossy writes.
   - *Retrieval-time*: store raw episodes with minimal write-time processing,
     do the heavy lifting when a query comes in. Preserves ground truth; more
     work per read.

2. **What happens when a new fact contradicts an old one.**
   - *DELETE-then-ADD* (mutate in place): the old memory is removed and replaced.
   - *In-place update*: the record is overwritten with the most-recent state.
   - *Immutable supersession* (ADD-only): the old fact is retained but flagged as
     no longer current, with a pointer to its replacement. Nothing is deleted.

3. **Deployment model.**
   - *Client-server / daemon*: requires a running server, often Docker, and
     external stores (graph DB, Postgres/pgvector).
   - *Embedded / offline-first*: runs in-process with a local store, no server,
     no mandatory cloud key.

The contradiction-handling axis is the one with the deepest consequences. Only
immutable supersession can reconstruct prior belief state ("what did we believe
about X at time T?"). DELETE-then-ADD and in-place update both destroy that
history.

## System Profiles

### Mem0 — write-time extraction reference design

*Reference: arXiv 2504.19413.*

Mem0 is the canonical write-time extraction system and the most-cited design in
this category. Its pipeline is two-phase:

1. **Extraction**: for each new message pair, extract the salient memories.
2. **Reconciliation**: an LLM compares each candidate against the top-10
   vector-similar existing memories and chooses one of ADD / UPDATE / DELETE /
   NOOP.

Contradictions are resolved by **DELETE-then-ADD** — the conflicting memory is
mutated in place. There is no retained history of the prior fact.

Self-reported headline numbers: **92.5 on LoCoMo** and **94.4 on LongMemEval** at
roughly **7k tokens per retrieval**. The original paper reports **66.88% LoCoMo**
under an LLM-as-a-Judge protocol, a **26% relative gain over OpenAI's memory**,
**91% lower p95 latency** (1.440s vs 17.117s), and a token reduction to **~7k vs
~26k**.

These numbers are disputed. Zep's independent evaluation put Mem0 at **62.47%**
against Mem0's self-reported 92.5% — a roughly **30-point gap**. The discrepancy
is attributed to (a) LLM-judge methodology (a "grade generously" instruction
inflates scores) and (b) the fact that LoCoMo's conversations (~16–26k tokens)
now fit comfortably inside modern context windows, which weakens the benchmark's
ability to discriminate between memory systems at all.

### MemMachine — retrieval-time / ground-truth-preserving

*Reference: arXiv 2604.04853, April 2026.*

MemMachine takes the opposite architectural bet. It is a **client-server**
system with a **two-tier** memory model:

- **Episodic memory** (STM + LTM, graph-based): stores raw episodes.
- **Profile memory** (semantic, SQL-backed): distilled, updatable facts.

It **minimizes write-time LLM extraction** and shifts the work to retrieval time,
preserving raw ground truth in the episodic tier. Contradictions are handled in
the profile tier by **updating in place to the most-recent state** — the profile
is *not* immutable, so prior states are not recoverable.

Self-reported numbers: **0.9169 on LoCoMo** (gpt-4.1-mini backbone) and **93.0%
on LongMemEval-S** (reported as a best-of-six-dimension ablation, which is worth
noting — it is not a single unified run).

Two claims that circulated about MemMachine did **not survive adversarial
fact-checking** (0 of 3 verification votes) and should be treated as **refuted**:
"+9.7 over Memobase" and "~80% fewer tokens than Mem0." Do not repeat these.

Operationally, MemMachine requires infrastructure: **Neo4j** (episodic) and
**PostgreSQL/pgvector** (profile), plus a **running server and Docker**.
Interfaces include a **REST v2 API, Python SDK, TypeScript SDK, and MCP**.
Adoption footprint at time of research: roughly **1k PyPI downloads/month** and
**13k Docker pulls**.

### Other players (less deeply profiled)

- **Zep**: publicly contested Mem0's benchmark claims. In Zep's own independent
  evaluation, Mem0 scored **62.47%** vs **Zep at 79.09%**. (Note: Zep is an
  interested party here, so this is not a neutral source either.)
- **Memori Labs**: another independent evaluation placed Mem0 at **62.47%** vs
  **Memori at 81.95%**.
- **Letta, Memobase, Cognee**: active competitors whose architectures we have not
  yet profiled in depth. Flagged here so a future reader knows they exist and are
  not yet covered.

## Comparison Table

| Dimension | Mem0 | MemMachine | lean-memory |
|---|---|---|---|
| Primary bet | Write-time extraction | Retrieval-time / ground-truth preserving | Structured write-time extraction |
| Extraction pipeline | 2-phase: extract → ADD/UPDATE/DELETE/NOOP | Minimal write-time; work at retrieval | 4-pass hybrid pipeline |
| Contradiction handling | DELETE-then-ADD (mutate in place) | Profile updated in place to latest state | Immutable ADD-only supersession (`is_latest=False` + `superseded_by`) |
| History retained? | No | No (profile tier) | Yes — nothing deleted |
| Point-in-time query (`as_of`) | No | No | Yes |
| Deployment | Server / cloud | Client-server, requires Docker | Embedded, offline-first |
| Storage | Vector store | Neo4j (episodic) + Postgres/pgvector (profile) | Per-namespace SQLite |
| Server/daemon required | Yes | Yes | No |
| Mandatory cloud key | Typically yes | No (but server required) | No |
| Self-reported LoCoMo | 92.5 (paper: 66.88% LLM-judge) | 0.9169 (gpt-4.1-mini) | — |
| Self-reported LongMemEval | 94.4 | 93.0% on LongMemEval-S (best-of-6 ablation) | — |

## Where lean-memory sits

lean-memory occupies a **third architectural position that none of the profiled
competitors occupy**. It combines:

- **Embedded, offline-first deployment.** Per-namespace **SQLite**, with no
  server, no daemon, and no mandatory cloud key. This alone separates it from
  Mem0 and MemMachine, both of which require running infrastructure.
- **Structured write-time extraction** via a **4-pass hybrid pipeline.** We do
  pay extraction cost at write time (like Mem0), but the output is structured.
- **Immutable ADD-only supersession.** When a new fact contradicts an existing
  one, the old fact's `is_latest` flag flips to `False` and a `superseded_by`
  pointer links it to its replacement. **Nothing is ever deleted.**

That last property is the differentiator. Contrast the three contradiction
models:

- Mem0: **DELETE-then-ADD** — old fact gone.
- MemMachine: **in-place profile mutation** — old state overwritten.
- lean-memory: **immutable supersession** — old fact retained and linked.

Because history is never destroyed, lean-memory is the **only** one of the three
that can answer **"what did we believe about X at time T?"** via **`as_of`
point-in-time queries.** For auditability, debugging agent behavior, and
reasoning about how beliefs evolved, this is a capability the mutate/delete
designs cannot retrofit without changing their storage model.

## Benchmark Credibility

Read this section before citing any accuracy number from this document or from
vendor material.

**All headline accuracy scores in this space are vendor- or author-self-reported.**
Cross-system comparison is hazardous because the evaluations differ on nearly
every axis that matters:

- **Different judges.** LLM-as-a-Judge scoring is sensitive to the grading
  prompt. A "grade generously" instruction is credited with much of Mem0's
  ~30-point gap between self-reported (92.5) and independently measured (62.47)
  LoCoMo scores.
- **Different backbone LLMs.** Numbers are quoted against different models
  (e.g., gpt-4.1-mini vs GPT-4o), which changes both accuracy and cost.
- **Different LoCoMo subtasks.** Most systems report only the QA subtask, not the
  full benchmark, so "LoCoMo score" does not mean the same thing across papers.
- **Different LongMemEval variants.** S / M / Oracle variants are not comparable,
  and some reported figures are best-of-N-dimension ablations rather than single
  unified runs (see MemMachine's LongMemEval-S number).
- **Benchmark saturation.** LoCoMo conversations (~16–26k tokens) now fit inside
  modern context windows, which limits how much the benchmark can distinguish
  memory systems from raw long-context.

Treat these figures as **reference points, not a leaderboard.** When lean-memory
publishes its own numbers, we should state the judge, the backbone model, the
exact subtask/variant, and whether the run is unified or an ablation — precisely
because the incumbents mostly do not.

Note also that several of the "independent" evaluations (Zep's, Memori's) come
from competitors, who are themselves interested parties. There is currently no
fully neutral, reproducible cross-system benchmark for this category.

## Sources

- **Mem0**: arXiv 2504.19413.
- **MemMachine**: arXiv 2604.04853 (April 2026).
- Independent re-evaluations of Mem0's LoCoMo score: Zep (Mem0 62.47% vs Zep
  79.09%) and Memori Labs (Mem0 62.47% vs Memori 81.95%) — both from competing
  vendors.
- MemMachine operational and adoption details (Neo4j + Postgres/pgvector, REST
  v2 / Python SDK / TypeScript SDK / MCP, ~1k PyPI downloads/month, 13k Docker
  pulls) from project materials.
- All facts above were verified via adversarial multi-agent research. Two
  MemMachine claims ("+9.7 over Memobase", "~80% fewer tokens than Mem0") were
  **refuted** (0/3 verification votes) and are excluded from the factual record.

---

Last updated: 2026-07-29

## Appendix: update-integrity results (WP2)

*When a fact changes, does the engine return the current truth and keep the
old one queryable?* Ten scripted scenarios through the public API only
(`Memory.add` → `Memory.search`), asserting per scenario: top-1 is the new
fact; the superseded fact has `is_latest=False` and `superseded_by` set; and
`as_of=<t before the update>` returns the old fact (point-in-time reads pass
`is_latest_only=False` — the as_of interval predicate governs visibility).
Offline deterministic backends by default; the identical scenarios run as
regression tests in CI (`tests/test_update_integrity_scenarios.py`).

Reproduce:

```bash
.venv/bin/python bench/update_integrity.py --markdown
```

Results (2026-07-29):

# Update-integrity results — lean-memory 0.2.2 (offline stub backends, Python 3.13.7)

| Scenario | Assertion | Result | Detail |
|---|---|---|---|
| employer_change | top1-is-current | PASS |  |
| employer_change | old-fact-retired | PASS |  |
| employer_change | as-of-returns-old-truth | PASS |  |
| name_identity_change | top1-is-current | PASS |  |
| name_identity_change | old-fact-retired | PASS |  |
| name_identity_change | as-of-returns-old-truth | PASS |  |
| city_move | top1-is-current | PASS |  |
| city_move | old-fact-retired | PASS |  |
| city_move | as-of-returns-old-truth | PASS |  |
| preference_flip | top1-is-current | PASS |  |
| preference_flip | old-fact-retired | PASS |  |
| preference_flip | as-of-returns-old-truth | PASS |  |
| additive_extends | top1-is-current | PASS |  |
| additive_extends | latest-set-exact | PASS |  |
| replacement_after_additive | top1-is-current | PASS |  |
| replacement_after_additive | old-fact-retired | PASS |  |
| replacement_after_additive | latest-set-exact | PASS |  |
| multivalued_preserved | top1-is-current | PASS |  |
| multivalued_preserved | latest-set-exact | PASS |  |
| as_of_before_everything | top1-is-current | PASS |  |
| as_of_before_everything | as-of-returns-old-truth | PASS |  |
| restart_persistence | top1-is-current | PASS |  |
| restart_persistence | old-fact-retired | PASS |  |
| restart_persistence | as-of-returns-old-truth | PASS |  |
| restatement_no_duplicate | top1-is-current | PASS |  |
| restatement_no_duplicate | latest-set-exact | PASS |  |

**ALL PASS** — 26/26 assertions.

### mem0 comparison arm (2026-08-07)

The same ten scenarios, the same assertion names, run against mem0 OSS instead
of lean-memory (`bench/update_integrity.py --arm mem0`, adapter class
`Mem0Arm`). The arm is opt-in and never skips silently: without `mem0ai`
installed the tool exits **2** with the hint `pip install mem0ai`.

Reproduce (a local LLM + embedder are required — this run used Ollama, no
cloud calls, `MEM0_TELEMETRY=false`):

```bash
pip install mem0ai ollama
ollama pull qwen2.5:3b && ollama pull nomic-embed-text
MEM0_TELEMETRY=false PYTHONPATH=src python bench/update_integrity.py --markdown            # arm A
MEM0_TELEMETRY=false PYTHONPATH=src python bench/update_integrity.py --arm mem0 --markdown # arm B
```

**Fairness rule.** The adapter maps each scenario onto mem0's own public API as
faithfully as that API allows and runs the identical assertions under the
identical names; no assertion is relaxed for one arm or tightened for the
other. Substring matching is case-insensitive in both arms (mem0 rewrites a
turn into third person, lean-memory keeps the source sentence; casing must not
decide a comparison). Where mem0's API has no equivalent of the concept under
test, the adapter probes the installed library at runtime and renders
`n/a (unsupported)` with mem0's own refusal quoted in the Detail cell; those
rows are excluded from the PASS tally rather than counted against either arm.

| Scenario concept | lean-memory 0.2.4 | mem0 2.0.17 (OSS) |
|---|---|---|
| ingest one turn | `Memory.add(ns, text, t_ref=t)` | `Memory.add(text, user_id=ns)` |
| turn timestamp | `t_ref=` | not available — `add(timestamp=…)` raises `ValueError: The timestamp parameter is not supported by the OSS Memory SDK.`; turns are ingested in wall-clock order |
| query | `Memory.search(ns, q, k=10)` | `Memory.search(q, filters={"user_id": ns}, top_k=10)` |
| current set | `search(...)` (latest only) | `get_all(filters={"user_id": ns})` |
| retired-but-queryable | `search(is_latest_only=False)` → `is_latest=0` + `superseded_by` | no equivalent field; the adapter accepts mem0's own `Memory.history(id)` UPDATE/DELETE record as the analogue |
| point-in-time read | `search(as_of=…)` | not available — `search(reference_date=…)` raises `ValueError: The reference_date parameter is not supported by the OSS Memory SDK.` |

**Arm A — lean-memory** (same machine, same interpreter, same day as arm B):

# Update-integrity results — lean-memory 0.2.4 (offline stub backends, Python 3.14.6)

| Scenario | Assertion | Result | Detail |
|---|---|---|---|
| employer_change | top1-is-current | PASS |  |
| employer_change | old-fact-retired | PASS |  |
| employer_change | as-of-returns-old-truth | PASS |  |
| name_identity_change | top1-is-current | PASS |  |
| name_identity_change | old-fact-retired | PASS |  |
| name_identity_change | as-of-returns-old-truth | PASS |  |
| city_move | top1-is-current | PASS |  |
| city_move | old-fact-retired | PASS |  |
| city_move | as-of-returns-old-truth | PASS |  |
| preference_flip | top1-is-current | PASS |  |
| preference_flip | old-fact-retired | PASS |  |
| preference_flip | as-of-returns-old-truth | PASS |  |
| additive_extends | top1-is-current | PASS |  |
| additive_extends | latest-set-exact | PASS |  |
| replacement_after_additive | top1-is-current | PASS |  |
| replacement_after_additive | old-fact-retired | PASS |  |
| replacement_after_additive | latest-set-exact | PASS |  |
| multivalued_preserved | top1-is-current | PASS |  |
| multivalued_preserved | latest-set-exact | PASS |  |
| as_of_before_everything | top1-is-current | PASS |  |
| as_of_before_everything | as-of-returns-old-truth | PASS |  |
| restart_persistence | top1-is-current | PASS |  |
| restart_persistence | old-fact-retired | PASS |  |
| restart_persistence | as-of-returns-old-truth | PASS |  |
| restatement_no_duplicate | top1-is-current | PASS |  |
| restatement_no_duplicate | latest-set-exact | PASS |  |

**ALL PASS** — 26/26 assertions.

**Arm B — mem0** (`mem0ai` 2.0.17, `ollama` client 0.6.2, `qdrant-client`
1.19.0, macOS 15, run 2026-08-07; verdict-identical across two consecutive
runs; ~23 s of scenario wall-clock in total):

# Update-integrity results — mem0 2.0.17 (llm=ollama/qwen2.5:3b, embedder=ollama/nomic-embed-text (768d), vector_store=qdrant (local, on-disk), ollama_base_url=http://localhost:11434, Python 3.14.6)

*Same scenarios and same assertion names as the lean-memory arm. `n/a (unsupported)` marks an assertion with no equivalent in this library's public API (probed at runtime — the library's own refusal is quoted in Detail); those rows are excluded from the PASS tally.*

| Scenario | Assertion | Result | Detail |
|---|---|---|---|
| employer_change | top1-is-current | PASS |  |
| employer_change | old-fact-retired | FAIL | no retirement record for 'Acme': it is absent from the live set and no UPDATE/DELETE row in mem0's history carries it (mem0's LLM emitted 1 memory event(s) [ADD] across 2 add() call(s)) |
| employer_change | as-of-returns-old-truth | n/a (unsupported) | mem0 2.0.17 has no point-in-time read: search(reference_date=…) → ValueError: The reference_date parameter is not supported by the OSS Memory SDK.; add(timestamp=…) → ValueError: The timestamp parameter is not supported by the OSS Memory SDK. |
| name_identity_change | top1-is-current | PASS |  |
| name_identity_change | old-fact-retired | FAIL | no retirement record for 'engineer': it is absent from the live set and no UPDATE/DELETE row in mem0's history carries it (mem0's LLM emitted 1 memory event(s) [ADD] across 2 add() call(s)) |
| name_identity_change | as-of-returns-old-truth | n/a (unsupported) | mem0 2.0.17 has no point-in-time read: search(reference_date=…) → ValueError: The reference_date parameter is not supported by the OSS Memory SDK.; add(timestamp=…) → ValueError: The timestamp parameter is not supported by the OSS Memory SDK. |
| city_move | top1-is-current | PASS |  |
| city_move | old-fact-retired | FAIL | no retirement record for 'Berlin': it is absent from the live set and no UPDATE/DELETE row in mem0's history carries it (mem0's LLM emitted 1 memory event(s) [ADD] across 2 add() call(s)) |
| city_move | as-of-returns-old-truth | n/a (unsupported) | mem0 2.0.17 has no point-in-time read: search(reference_date=…) → ValueError: The reference_date parameter is not supported by the OSS Memory SDK.; add(timestamp=…) → ValueError: The timestamp parameter is not supported by the OSS Memory SDK. |
| preference_flip | top1-is-current | PASS |  |
| preference_flip | old-fact-retired | FAIL | no retirement record for 'vim': it is absent from the live set and no UPDATE/DELETE row in mem0's history carries it (mem0's LLM emitted 1 memory event(s) [ADD] across 2 add() call(s)) |
| preference_flip | as-of-returns-old-truth | n/a (unsupported) | mem0 2.0.17 has no point-in-time read: search(reference_date=…) → ValueError: The reference_date parameter is not supported by the OSS Memory SDK.; add(timestamp=…) → ValueError: The timestamp parameter is not supported by the OSS Memory SDK. |
| additive_extends | top1-is-current | PASS |  |
| additive_extends | latest-set-exact | FAIL | latest=['User works at Acme and Globex.'] expected-substrings=('Acme', 'Globex') (mem0's LLM emitted 1 memory event(s) [ADD] across 2 add() call(s)) |
| replacement_after_additive | top1-is-current | FAIL | expected 'Zorbex' in top-1, got 'User works at Acme and Globex.' (mem0's LLM emitted 2 memory event(s) [ADD, ADD] across 3 add() call(s)) |
| replacement_after_additive | old-fact-retired | FAIL | old value is still a current memory in mem0: ['User was previously employed at Acme and Globex, but now works at Zorbex.'] |
| replacement_after_additive | latest-set-exact | FAIL | latest=['User works at Acme and Globex.', 'User was previously employed at Acme and Globex, but now works at Zorbex.'] expected-substrings=('Zorbex',) (mem0's LLM emitted 2 memory event(s) [ADD, ADD] across 3 add() call(s)) |
| multivalued_preserved | top1-is-current | PASS |  |
| multivalued_preserved | latest-set-exact | PASS |  |
| as_of_before_everything | top1-is-current | FAIL | expected 'Berlin' in top-1, got '<no results>' (mem0's LLM emitted 0 memory event(s) [none] across 1 add() call(s)) |
| as_of_before_everything | as-of-returns-old-truth | n/a (unsupported) | mem0 2.0.17 has no point-in-time read: search(reference_date=…) → ValueError: The reference_date parameter is not supported by the OSS Memory SDK.; add(timestamp=…) → ValueError: The timestamp parameter is not supported by the OSS Memory SDK. |
| restart_persistence | top1-is-current | PASS |  |
| restart_persistence | old-fact-retired | FAIL | no retirement record for 'Acme': it is absent from the live set and no UPDATE/DELETE row in mem0's history carries it (mem0's LLM emitted 1 memory event(s) [ADD] across 2 add() call(s)) |
| restart_persistence | as-of-returns-old-truth | n/a (unsupported) | mem0 2.0.17 has no point-in-time read: search(reference_date=…) → ValueError: The reference_date parameter is not supported by the OSS Memory SDK.; add(timestamp=…) → ValueError: The timestamp parameter is not supported by the OSS Memory SDK. |
| restatement_no_duplicate | top1-is-current | FAIL | expected 'Berlin' in top-1, got '<no results>' (mem0's LLM emitted 0 memory event(s) [none] across 2 add() call(s)) |
| restatement_no_duplicate | latest-set-exact | FAIL | latest=[] expected-substrings=('Berlin',) (mem0's LLM emitted 0 memory event(s) [none] across 2 add() call(s)) |

**FAILURES PRESENT** — 8/20 assertions. 6 further assertion(s) rendered `n/a (unsupported)` — no equivalent in this arm's API, excluded from the tally.

**Caveats — read before quoting any of this.**

1. **The backbone is small, and it dominates several rows.** mem0 forms
   memories with a single LLM call that must return JSON. Replaying mem0's own
   `ADDITIVE_EXTRACTION_PROMPT` directly against qwen2.5:3b returns well-formed
   but empty JSON (`{"memory": []}`) for an isolated single-sentence turn — so
   in every scenario mem0 stored nothing from turn 1 and only formed a memory
   once a second, differing turn supplied conversational context. In
   `as_of_before_everything` and `restatement_no_duplicate` no such turn exists,
   so nothing was stored at all; those three FAILs are backbone artefacts and
   must not be quoted as mem0 results. A larger backbone would plausibly extract
   on turn 1.
2. **`old-fact-retired` mixes an architectural fact with a backbone artefact.**
   Architectural: mem0 2.0.17 OSS has no `is_latest` / `superseded_by` and no
   way to query a retired value through search — the adapter therefore accepts
   mem0's own `history()` UPDATE/DELETE record as the analogue. Backbone: since
   turn 1 stored nothing, there was no separate old memory to retire, and the
   history log held ADD rows only. The five `no retirement record …` FAILs
   therefore record the absence of a retired-but-queryable record, not the
   observed destruction of one. The sixth (`replacement_after_additive`) is a
   different mode: there the superseded value was still a current memory.
3. **Granularity differs.** mem0 merges values into one memory string (e.g.
   `User works at Acme and Globex.`). `latest-set-exact` counts memories, so a
   merged memory fails the count even though both values appear in its text —
   the Detail cell shows exactly what was returned.
4. **The six `n/a (unsupported)` rows are architectural and version-pinned**,
   not a backbone effect: mem0 2.0.17 OSS rejects both temporal parameters at
   the API boundary. mem0's hosted Platform advertises them; this arm exercises
   the OSS package only.
5. **`replacement_after_additive`** shows mem0's LLM choosing ADD twice rather
   than UPDATE/DELETE, leaving the stale memory co-resident with — and ranked
   above — the newer one. Both the event choice and the ranking depend on the
   backbone and the embedder.
6. **Scope.** One machine, one backbone (qwen2.5:3b), one embedder
   (nomic-embed-text), two runs, ten scripted scenarios. This is reproduced,
   versioned behaviour under the pinned header and nothing more: no accuracy
   claim on LoCoMo/LongMemEval-style benchmarks, and no claim about mem0's
   hosted Platform, follows from it.

`--arm mem0` writes a per-turn ingest trace (`add … -> ADD "…"` /
`no memory extracted`) to stderr, so every row above can be traced back to what
mem0's LLM actually emitted.
