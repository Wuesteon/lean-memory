# CLAUDE.md

## ⚠️ START HERE: strategy is quality-gate → MCP launch (2026-07-08)

Approved direction: **close a small, hard quality gate on the first-run
experience, then launch across MCP channels** (Pure A). Read before any work:

1. **`docs/superpowers/specs/2026-07-08-strategic-direction-design.md`** — the
   approved strategy (positioning, the six-item quality gate, launch plan, the
   six-week post-launch demand read). Non-negotiable context; do not re-derive.
2. **`docs/superpowers/plans/2026-07-08-launch-quality-gate.md`** — the ordered
   implementation plan that executes the gate.

State: the former "Phase 2 suspended, fix engine then re-run benchmarks" framing
is **superseded**. Engine-fix backlog items 1–3 are **FIXED** on `launch-gate`
(numbers are the source of truth in `bench/results/calibration/README.md`):

- **Escalation recalibration** — endpoint-scoped coref + `prior_entity` trigger
  dropped (two user-approved amendments); real-turn escalation **95.9% → 14.6%**
  at the frozen `(typing=0.4, conf=0.4)` operating point; goldset **10.1% → 7.6%**.
  BET-2 revalidation PASSES all three gates.
- **Extraction granularity** — GLiNER `DEFAULT_THRESHOLD` 0.1 → 0.4;
  **8.43 → 3.67 facts/turn**.
- **Recency anchoring** — `Memory.search(now=...)` forwards search-time now
  (wall-clock default unchanged).

**Benchmark runs (LongMemEval/LoCoMo) are DEFERRED past the MCP launch** per the
spec — they are a post-launch credibility layer, not the critical path. The
harness (`bench/phase2_*.py`) is complete and needs no changes for the eventual
re-run.

Gate item 5 is CLOSED (secrets rotated 2026-07-11 — revoked in dashboards,
local copies deleted; harness branch merged and labels removed). A 2026-07-12
publish-readiness review board found three launch blockers on the v0.1.2 MCP
first-run path plus packaging majors — all fixed as **v0.1.3** (tagged
2026-07-12; see CHANGELOG.md). Post-gate, **WP10a sleep-time maintenance**
(PR #3) and **WP10b review UI** (PR #4) merged 2026-07-16/17 — offline
dedupe/summarize/evict with a human review queue over MCP and the console;
default-off, first-run path pinned byte-identical; design + verification
record in `docs/superpowers/specs/2026-07-16-sleep-time-maintenance-design.md`.
**WP1 launch execution shipped (2026-07-21):** v0.2.0 (2026-07-17, feature
release) and v0.2.1 (2026-07-21, metadata-only, PR #5) are tagged and
released; **MCP Registry and PyPI (`lean-memory` + `lean-memory-console`) are
LIVE at 0.2.1**; **v0.2.2** (2026-07-29, patch) pinned `mcp>=1.2,<2` (mcp
2.0.0 removed `mcp.server.fastmcp`, breaking fresh installs) and shipped the
WP11 write-time restatement dedupe; **v0.2.3** (2026-07-29, patch) adds dual-path mcp 1.x/2.x support (pin
`>=1.2,<3`, WP12), fixes the 2.0 worker-thread SQLite crash, and ships the
WP13 tool metadata (Glama re-scan) and WP2 update-integrity benchmark;
**v0.2.4** (2026-08-06, patch) ships the WP14 console MCP tool metadata;
**v0.3.0** (2026-08-07, minor — the current release) ships WP15 entity name
collation (`entity.name_key`, schema v3 in-place migration, one-way
downgrade — hence minor not patch), the lowercase-first-person extractor
fix, and the WP2 mem0 comparison arm with a published fully-local run
(issues #13–#15, #17 all closed 2026-08-06/07; only #16 signal-watch and
#18 six-week checkpoint remain open). **Channel-posts decision (user,
2026-07-29): the remaining posts are OPTIONAL, not launch-required** — the
Claude Code plugin marketplace form (platform.claude.com/plugins/submit) and
the Show HN / r/ClaudeAI / r/LocalLLaMA posts may go out whenever (or never);
final copy stays ready in `docs/launch/launch-copy.md`, runbook in
`docs/launch/launch-checklist.md`. **`awesome-mcp-servers` PR #9890 MERGED
2026-07-22** (found 2026-08-06 — earlier "in review" framing was stale), so
that channel has been live since one week before window start. The **six-week
demand read (spec §4) runs on the live channels (MCP Registry + PyPI +
awesome-mcp-servers), window start 2026-07-29 (v0.2.2)**; if further posts go
out later, note the date so the read can be segmented. Demand-read caveat
(2026-08-06): the repo's 47 stars / 44 forks are a synthetic bot burst on
2026-07-22 (Faker-style accounts, all created 2026-06-27) — exclude from the
read; organic GitHub social signal is ~3 stars.

Historical context (dated, do not re-derive): `docs/phase2-learnings.md`
(assumptions vs. reality postmortem) and `docs/superpowers/phase2-HANDOFF.md`
(operational runbook + the now-fixed engine-fix backlog).

## Project

lean-memory: embedded, local-first agent-memory engine (SQLite vec0 + FTS5,
hybrid retrieval + rerank, ADD-only supersession with a monotemporal spine).
See `ARCHITECTURE.md` for the phase roadmap and BET results; `README.md` for
the user-facing quickstart.

- Python ≥3.10; dev venv at `.venv` (3.13). Run tests:
  `.venv/bin/python -m pytest tests/ -q` (offline by default — all model
  backends have deterministic stubs).
- Real model extras are opt-in: `[models]` (embedder+reranker), `[extract]`
  (GLiNER2), `[llm]` (Ollama typer), `[bench]` (OpenRouter client).
- Benchmarks live in `bench/` (BET-2: `bet2_*.py`; Phase 2: `phase2_*.py`).
  Frozen-config discipline: any number without a pinned config hash, judge
  model, judge prompt, and backbone is not publishable.
- Roadmap work is clustered into worktree-sized packets with dependencies,
  gates, and file-conflict lanes: `docs/superpowers/workpackets.md`. Claim a
  packet there before starting it; one worktree per packet.
