# Launch copy — drafts for review

> DRAFTS ONLY. Nothing here is posted. Every claim is checked against the repo
> (README.md, CHANGELOG.md, ARCHITECTURE.md, bench/results/calibration/README.md,
> docs/superpowers/specs/2026-07-08-strategic-direction-design.md,
> docs/superpowers/specs/2026-07-16-sleep-time-maintenance-design.md). Edit freely
> before posting from your own account.
>
> GitHub: https://github.com/Wuesteon/lean-memory

---

## 1. Show HN

**Title:**

Show HN: Lean-memory – agent memory that's just a SQLite file

**First comment (author intro):**

I built lean-memory because I wanted my coding agent's memory to live on my
machine, next to my code, instead of behind a cloud API. It's an embedded
Python library: `pip install lean-memory`, and each namespace is one SQLite
file (vec0 for vectors, FTS5 for text). No server, no daemon, no mandatory
cloud key. You can open the file in any SQLite browser and read what's in it.

I didn't try to build another hosted memory layer — mem0 already does the
universal, framework-everywhere version well, and I have no interest in
competing there. This is the thing you install that never phones home.

The design choice I care about most is that writes are ADD-only. Facts are
never deleted or updated in place — a contradicting fact supersedes the old
one, which stays around with an `is_latest=False` flag and a `superseded_by`
pointer. That gives you point-in-time queries: you can ask "employer" with an
`as_of` timestamp and get what was true then, not just now. Retrieval is
hybrid — Qwen3-0.6B dense embeddings + BM25, fused with RRF, reranked by a
cross-encoder. Extraction is a 4-pass pipeline (rules → GLiNER2 → a calibrated
router → an optional local LLM typing pass).

The v0.2.0 feature I'm most curious for feedback on: sleep-time maintenance
with a human review queue. An offline job (run it off-hours or on cron) dedupes,
summarizes, and demotes low-value records between sessions, but nothing is
deleted — the judgment calls get staged as *proposals* you approve the next
morning, either in a web console or conversationally in Claude Code via a
`/review-memory` command. Only exact-duplicate retirement and a strict eviction
band auto-apply; everything else waits for you, and an unreviewed proposal
expires rather than silently applying. As far as I can tell, no other shipping
memory product stages agent-proposed changes for human approval — they all
apply-then-curate — so this is the one place I'm genuinely out ahead, and I'd
love to know if it holds up. The as-of history stays bit-identical across
maintenance, pinned by tests.

What's missing: I have no public benchmark scores yet, on purpose. Numbers in
this space swing 25+ points under independent re-evaluation, so I'd rather ship
"no number" than a bad one; the eventual one will be frozen-judge reproducible.
It also brute-forces per namespace, so it won't scale to millions of facts in
one tenant.

Feedback I'm after: is the ADD-only / time-travel model actually useful to you,
or overkill? Does the review queue — think `git add -p` for your agent's memory
— feel worth the ceremony, or would you rather it just applied? And what would
you want the MCP integration to capture automatically? Apache-2.0, tests run
fully offline. Repo: https://github.com/Wuesteon/lean-memory

---

## 2. r/ClaudeAI

**Title:**

Give Claude Code / Desktop persistent local memory via MCP in about 2 minutes

**Body:**

I made a small MCP server that gives Claude Code and Claude Desktop memory that
persists across sessions — and it all stays on your machine, one SQLite file
per namespace, nothing leaves the box.

Install and wire it into Claude Code:

```
pip install 'lean-memory[mcp,models,extract]'
claude mcp add lean-memory -- lean-memory-mcp
```

(First run downloads ~2 GB of open models for real retrieval + extraction, so
pre-warm once — there's a one-liner in the README.)

It exposes `memory_add`, `memory_search`, and `memory_clear`. Claude can save
facts you tell it (where you work, project conventions, preferences) and pull
the relevant ones back in a later session. Because writes are ADD-only, when
something changes the old fact is superseded rather than deleted, so it can also
answer "what did I tell you, and when."

New in v0.2.0: sleep-time maintenance with a review queue. An offline job dedupes
and summarizes stored memory between sessions, but the judgment calls are staged
as *proposals* — nothing changes until you approve. You review them right inside
Claude Code: a `/review-memory` command (plus four MCP tools —
`memory_maintenance_run`, `memory_maintenance_status`, `memory_review_queue`,
`memory_review_decide`) walks you through the queue grouped by entity, recording
only the verdicts you give. Unreviewed proposals expire rather than auto-applying,
and as-of history stays intact. As far as I can tell, no other memory product
lets you approve agent-proposed changes before they land — everything else
applies first and lets you clean up after.

It's local-only and Apache-2.0. Repo, plus the Claude Desktop config, here:
https://github.com/Wuesteon/lean-memory

---

## 3. r/LocalLLaMA

**Title:**

lean-memory: a fully local agent-memory stack — no API keys anywhere, every model runs on your machine

**Body:**

I've been building an embedded agent-memory engine where the entire stack is
local: no API keys, no cloud calls, nothing phones home. It's a `pip install
lean-memory` library, and each namespace is a single SQLite file (vec0 +
FTS5).

Everything that touches a model runs locally:

- Embeddings: Qwen3-Embedding-0.6B (dense), fused with BM25 via RRF
- Rerank: Ettin-32M cross-encoder
- Extraction: a 4-pass pipeline — rules → GLiNER2 NER → a calibrated router →
  an optional local LLM typing tier (Ollama, e.g. qwen2.5:3b) for the ~15% of
  candidates that escalate

The default `[mcp,models,extract]` install downloads about 2 GB of open,
ungated weights. Honest about the tradeoffs: the optional `[llm]` Ollama tier
is what gives you real typing on inferential facts — without it those fall
through to a deterministic stub. And I have no public benchmark scores yet
(deliberate — memory-benchmark numbers in this space are unreliable; mine will
be frozen-judge reproducible when I publish them).

v0.2.0 adds sleep-time maintenance, and it stays true to the local-only rule:
an offline job (`lean-memory-maintain`, dry-run by default, or a cron line)
dedupes, summarizes, and demotes stale records between sessions, entirely on
your machine. Nothing is deleted — the safe band (exact-duplicate retirement,
a strict eviction band) auto-applies; every judgment call is staged as a
*proposal* you approve, in a web console or conversationally in Claude Code via
`/review-memory`. Unreviewed proposals expire rather than auto-applying, and
the as-of spine stays bit-identical across a run (pinned by tests). As far as I
can tell, no other shipping memory product gates agent-proposed changes on human
approval before they land — they all apply-then-curate — so if you care about
keeping a human in the loop on what your agent remembers, that's the pitch.

What I do have is a calibration writeup with real measured numbers — the
escalation rate on real conversational turns, the granularity sweep, and the
BET-2 three-gate revalidation. I'd genuinely welcome scrutiny of that
methodology:

https://github.com/Wuesteon/lean-memory/blob/main/bench/results/calibration/README.md

The offline test suite runs with no downloads and no network at all (every
backend has a deterministic stub), CI is on Linux + macOS, Apache-2.0. Repo:
https://github.com/Wuesteon/lean-memory

---

## 4. awesome-mcp-servers PR (one-liner)

> [lean-memory](https://github.com/Wuesteon/lean-memory) - Embedded, local-first agent memory in a single SQLite file per namespace (vec0 + FTS5 hybrid retrieval). ADD-only history queryable as-of any past time; offline "sleep-time" maintenance stages dedupe/summarize/evict proposals a human reviews. No server, no cloud key.

---

## 5. MCP registry description (server.json / listing blurb)

Local-first agent memory that's just a SQLite file. Each namespace is one
file on your machine — no server, no daemon, no cloud key; runs fully offline
out of the box (real local models are an opt-in extra). Writes are ADD-only:
updated facts are superseded, never overwritten, so you can query what your
agent believed at any past point in time. An offline sleep-time maintenance
job dedupes, summarizes, and demotes low-value memory between sessions —
judgment calls become proposals you approve in Claude Code or the bundled
console; unreviewed proposals expire rather than auto-apply. Tools:
memory_add, memory_search, memory_clear, memory_maintenance_run/status,
memory_review_queue/decide.
