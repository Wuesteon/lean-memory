# Agent-Memory Benchmarks: LoCoMo & LongMemEval

Team reference for the two canonical agent-memory benchmarks. Written for a
developer who needs to understand what each benchmark actually tests and what
running lean-memory against them would entail.

## Overview

Two benchmarks dominate the agent-memory literature. Both probe whether a system
can answer questions grounded in long, multi-session conversation histories —
the exact workload lean-memory targets.

- **LoCoMo** (arXiv 2402.17753, ACL 2024) — long-term *social* conversations
  between personas; tests QA, event summarization, and multimodal dialogue
  generation.
- **LongMemEval** (arXiv 2410.10813, ICLR 2025) — 500 manually curated questions
  over multi-session chat histories, decomposed into five distinct memory
  abilities. Its **Knowledge Updates (KU)** dimension is the single most
  relevant test for lean-memory's design.

A recurring theme in both papers: capable base LLMs collapse when asked to reason
over long histories from raw context, and the gap versus oracle/human performance
is what motivates dedicated memory systems in the first place.

**Cross-system score comparison is hazardous.** Published numbers use different
judges, different backbone LLMs, different subtask selections, and different
variants. Treat every score below as a directional signal, not a leaderboard
position.

## LoCoMo

**Paper:** arXiv 2402.17753 (ACL 2024) · **Code:** <https://github.com/snap-research/locomo>

LoCoMo (Long Conversation Memory) is a dataset of very long-term social
conversations between fixed personas.

- **Scale:** ~300 turns / ~9K tokens on average per conversation, spanning up to
  **35 sessions**.
- **Construction:** a machine-human pipeline built on personas plus temporal
  event graphs. Roughly **15% of turns were human-edited** to fix coherence and
  factual drift, so it is not purely synthetic.
- **Tasks:** three, not one —
  1. **QA** — question answering over the conversation.
  2. **Event summarization** — summarizing what happened across sessions.
  3. **Multimodal dialogue generation** — generating grounded multimodal turns.

**Watch out:** most published systems evaluate on the **QA task only**. When you
see a "LoCoMo score," assume it is QA-only unless stated otherwise, and do not
compare it against a summarization or multimodal number.

**Key finding (the motivation for dedicated memory):** GPT-4 scored an F1 of
~**32.1** on QA versus human performance of **87.9** — roughly a **73% lag**,
concentrated in temporal reasoning. Long-context prompting alone does not solve
this; the gap is what memory systems exist to close.

**Tooling:** LanceDB publishes a ready-to-use evaluation harness at
<https://github.com/lancedb/locomo-eval>, which is the most direct path to
running a new memory backend against LoCoMo QA.

> **Refuted claim (adversarial verification).** Vendor blog posts describe LoCoMo
> as "1,540 questions / 10 conversations / 26K tokens." Those numbers did not
> survive fact-checking. The correct figures are the ones above (~300 turns /
> ~9K tokens avg, up to 35 sessions). If you see the 1,540 / 26K framing, treat
> the source as unreliable.

## LongMemEval

**Paper:** arXiv 2410.10813 (ICLR 2025) · **Code:** <https://github.com/xiaowu0162/longmemeval>

LongMemEval is **500 manually curated questions** posed over multi-session chat
histories. Unlike LoCoMo, the questions are hand-authored to isolate specific
memory abilities rather than sampled from a generation pipeline.

**Three variants** (differing in how much history the system must ingest):

| Variant | Description |
|---|---|
| **S** | Shorter sessions — the standard reporting variant. |
| **M** | Longer sessions — a harder, longer-context setting. |
| **Oracle** | Gold retrieval provided — isolates reasoning from retrieval quality. |

**Five abilities tested:**

1. **Information extraction (IE)** — single-fact recall from one session.
2. **Multi-session reasoning (MR)** — combining facts across sessions.
3. **Temporal reasoning (TR)** — reasoning about time-anchored facts.
4. **Knowledge updates (KU)** — recognizing that the user's information *changed*
   over time and returning the current value. **← most relevant to lean-memory.**
5. **Abstention (ABS)** — knowing when to say "I don't know" rather than
   hallucinate.

**Key finding:** chat assistants reading full histories drop **30–60% accuracy**
versus oracle (gold) retrieval. Under a GPT-4o backbone, ChatGPT fell **-37%**
and Coze fell **-64%** relative to oracle. The lesson: retrieval quality, not
raw context length, dominates end-to-end accuracy.

**Tooling:** the official harness lives at the repo above. Mem0 additionally
maintains a benchmark harness at
<https://github.com/mem0ai/memory-benchmarks> that wraps both LongMemEval and
LoCoMo runs.

> **Refuted claim.** The description "LongMemEval-S = ~115K tokens / 50 sessions"
> did not survive fact-checking. Use the variant semantics above (S = shorter
> sessions, M = longer sessions) rather than a fixed token/session count.

## The KU Dimension — lean-memory's key test

Knowledge Updates is the LongMemEval ability that directly exercises
lean-memory's core design decision. The paper defines it as:

> *recognizing changes in the user's personal information and updating the
> knowledge of the user dynamically over time.*

**Canonical example.** The user says "I work at Acme" in session 1, then "I moved
to Globex" in session 5. A correct system must return **Globex**, not Acme, when
asked where the user works now.

This is the head-to-head discriminator between the leading approaches:

| System | Update mechanism | Old value after update |
|---|---|---|
| **lean-memory** | `superseded_by` pointer + `is_latest=False`; old fact stays indexed but de-ranked; `as_of` query returns the correct value at any past time | **Retained & queryable** |
| **MemMachine** | Profile SQL row updated in place | **Gone** |
| **Mem0** | DELETE old memory + ADD new memory | **Gone** |

Because lean-memory supersedes rather than deletes, it is the **only** system of
the three that can correctly answer *both*:

- "Where does the user work **now**?" → Globex
- "Where did the user work **before** Globex?" → Acme

MemMachine and Mem0 can answer the first question but not the second, because the
prior value no longer exists after the update. Standard LongMemEval KU scoring
only asks the "now" question, so this bitemporal advantage is not fully captured
by the headline KU number — it is a capability the benchmark's current scoring
under-credits, and worth flagging when we report results.

## Published Scores

Reference points only. These come from heterogeneous evaluations (different
judges, backbones, subtask selections, and S/M/Oracle variants) and are **not**
directly comparable to one another.

| System | LoCoMo | LongMemEval | Notes |
|---|---|---|---|
| Mem0 (self-reported, 2026) | 92.5 | 94.4 | ~6,956 tokens/retrieval; disputed |
| Mem0 (Zep independent eval) | 62.47% | — | Same benchmark, different judge |
| MemMachine (self-reported, Apr 2026) | 0.9169 (gpt-4.1-mini) | 93.0% (LME-S, best-of-ablation) | Author-reported only |
| Zep (self-reported) | 79.09% | — | Zep's own eval |
| Memori Labs (independent) | — | — | Put Mem0 at 62.47%, Memori at 81.95% |
| **lean-memory** | **not yet run** | **not yet run** | **Phase 2 goal** |

Note the ~30-point spread on Mem0's LoCoMo number (92.5 self-reported vs 62.47%
under Zep's judge) — a concrete illustration of why these are directional
signals, not a ranked leaderboard.

## Running These Benchmarks

lean-memory has **not yet been run against either benchmark**; a full
LoCoMo/LongMemEval evaluation is a **Phase 2** goal. What exists today in
`bench/` is the seed infrastructure, not the benchmark runners:

- **`bench/smoke_quality.py`** — a ~10-line ranking sanity check. Offline (default
  `FakeEmbedder`/`IdentityReranker`) it verifies plumbing (does the relevant fact
  come back at all); with `--real` it loads the real embedder + Ettin-32M reranker
  and checks whether ranking puts the gold fact on top. Explicitly *not*
  LongMemEval/LoCoMo — it exists so that plugging in real models is a one-command
  verification rather than a leap of faith.
- **`bench/bet2_goldset.py`** / **`bench/bet2_ablation.py`** — the frozen, hashed
  gold set and ablation harness for the BET-2 extraction gate (hybrid extraction
  vs 100%-LLM). This measures the internal
  assert/extend/supersede/derive relation routing that underpins KU behavior; it
  is not an end-to-end QA benchmark.

**What a real run would entail.** Adapting an existing harness to drive
`lean_memory.Memory` — ingest each conversation's sessions via `Memory.add`, then
answer each benchmark question via `Memory.search`:

- **LoCoMo:** the LanceDB harness (<https://github.com/lancedb/locomo-eval>) is
  the most direct starting point; expect to implement QA only first.
- **LongMemEval:** the official harness (<https://github.com/xiaowu0162/longmemeval>)
  or Mem0's wrapper (<https://github.com/mem0ai/memory-benchmarks>). Prioritize
  the **KU** subset first — it is where lean-memory's superseding/bitemporal design
  should differentiate, and where we should additionally probe the
  "before Globex" query the standard scoring omits.

Both require an LLM judge and a chosen backbone; pin both and record them
alongside any score we publish, so our numbers stay comparable across our own
runs even if they are not comparable to other vendors'.

Last updated: 2026-07-02
