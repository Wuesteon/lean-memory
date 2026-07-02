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

Last updated: 2026-07-02
