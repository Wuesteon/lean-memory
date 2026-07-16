# Changelog

All notable changes to lean-memory are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.2.0] - 2026-07-17

### Added

- **Sleep-time maintenance** — an offline job that cleans up stored memory
  between sessions (dedupe, summarize-old, evict-low-value) while preserving the
  ADD-only spine and as-of query semantics. Nothing is ever deleted: maintenance
  only appends, retires via the existing `superseded_by` flip, or demotes to a
  cold tier, so full history stays queryable at any past point in time —
  bit-for-bit identical at the store visibility predicate, pinned by executable
  tests. Design:
  `docs/superpowers/specs/2026-07-16-sleep-time-maintenance-design.md`.
  - **`lean-memory-maintain` CLI** (in the core package beside `lean-memory-mcp`)
    — **dry-run by default**; `--apply` runs the auto band and stages proposals,
    `--auto-only` runs only the provably-safe band, `--json` emits a
    machine-readable report. `--root` defaults to `$LM_DATA_ROOT`; `--namespace`
    scopes to one namespace. Ships a cron/launchd recipe in the README.
  - **Two-tier autonomy** — only exact-duplicate retirement and a strict
    eviction band auto-apply. Near-duplicate merges, summaries, and softer
    evictions are staged as **proposals** a human reviews; an unreviewed proposal
    **expires** after 30 days (default) rather than auto-applying — silence is
    never consent.
  - **Console Review page** — the memory console gains a `Review` page: the
    pending-proposal queue grouped by entity with per-kind before/after
    evidence, Approve / Keep / Edit-then-approve / Promote verbs, batch-approve
    per entity, and a run-maintenance button (dry-run by default, apply behind
    a confirm). Backed by five auth-gated console routes
    (`/views/{ns}/review/*`, `/views/{ns}/maintenance/*`) that reach the engine
    only through the gateway; double-decides surface as HTTP 409 with the
    compare-and-set outcome passed through, so the console and Claude Code can
    review the same queue without double-applying. Also closes the four WP10a
    carry-in cleanups (summarizer budget-check ordering, CLI missing-namespace
    guard, `Store.path` on the ABC, tool-docstring apply/auto-spawn asymmetry).
  - **Conversational review in Claude Code** — four MCP tools
    (`memory_maintenance_run`, dry-run by default; `memory_maintenance_status`,
    model-free; `memory_review_queue`; `memory_review_decide`) on the core
    `lean-memory-mcp` server plus both console MCP surfaces, a
    `review-memory-maintenance` MCP prompt (console MCP surfaces), and a `/review-memory` plugin command
    that walks the queue grouped by entity and records only explicit user
    verdicts.
  - **Tier-filtered retrieval** — default latest-mode search hides cold-demoted
    facts; `as_of` queries never filter tier, and `search(..., include_cold=True)`
    opts out. Promotion back to hot is explicit-only — reads never durably change
    surfaces.
  - **Schema v2** — `user_version`-gated 1→2 migration (v1-format files upgrade
    in place, verified by a checked-in fixture) adding `record_kind`,
    `fact_derivation` lineage, and the `maintenance_run` / `maintenance_proposal`
    ledger tables. Two exact-no-op ingest hooks (duplicate-cascade,
    summary-staleness cascade) keep offline transforms coherent under later
    ingest; both are byte-identical no-ops until maintenance has ever run.
  - **Concurrency & crash safety** — engine-wide `PRAGMA busy_timeout`, a
    `batch()` unit-of-work, an atomic maintenance lease with heartbeat and
    crash-resume, and `memory_clear` refusing while a live maintenance lease is
    held. `LM_MAINT_AUTO=1` opts into a detached background auto-run on the first
    tool call of a stale namespace (off by default).

## [0.1.3] - 2026-07-12

Publish-readiness release: an independent multi-team review of v0.1.2 found
three launch blockers on the canonical MCP first-run path plus packaging and
correctness majors — all fixed here.

### Fixed
- **MCP Registry install crashed on startup**: `server.json` ran
  `uvx --from lean-memory lean-memory-mcp`, but `mcp` is an optional extra, so
  every registry install died with `ModuleNotFoundError`. The manifest now
  installs `lean-memory[mcp,models,extract]` (pinned by a manifest test).
- **Model banner corrupted the MCP stdio stream**: gliner2's `from_pretrained`
  prints a config banner to stdout — the JSON-RPC channel — on the first
  `memory_add` of the canonical install. All model lazy-loads (GLiNER2,
  SentenceTransformer, CrossEncoder) now route load-time chatter to stderr.
- **Embedder swap bricked existing namespaces**: reopening a DB created with a
  different embedder dimension (768-dim offline stub → 1024-dim Qwen after
  installing `[models]`) failed deep in retrieval with an opaque shape error;
  the store now refuses the mismatch at open with an actionable message.
- **Uppercase FTS5 operator words crashed search**: `'coffee AND tea'` raised
  `sqlite3.OperationalError` through `Memory.search` and the `memory_search`
  tool. Terms are now quoted FTS5 string literals; the sparse arm degrades to
  no-hits on any residual syntax error.
- **Functional-slot supersession left stale facts current**: a replacement
  retired only the single most-similar fact, so a slot extended by an additive
  cue ("I also work at Globex.") kept two conflicting current employers. A
  replacement on a functional slot now retires every co-valid latest fact;
  multi-valued slots keep single-target retirement.
- **High-similarity band ignored multi-valued slots**: with real embedders,
  distinct co-valid values on a multi-valued slot (jazz/blues) embed at cosine
  0.6–0.95 and were silently superseded; multi-valued predicates now stay
  co-valid in every band (new resolver route: `high_extends_additive`).
  Predicate-scoped on purpose: the textual cue ("and"/"also") stays a
  low/mid-band signal so a conjunction-phrased replacement still supersedes.
- **`[llm]` extra crashed every add() with Ollama stopped**: `Memory.add` now
  catches `TyperError` and stub-types the escalated batch, as the typer
  contract always documented.
- **Packaging**: Apache-2.0 `LICENSE` added (repo, wheel, and sdist — the
  license was previously declared but its text shipped nowhere); the sdist is
  scoped to user-facing files (0.1.2 shipped internal strategy docs, agent
  instructions, and the bench harness to PyPI); the README hero GIF uses an
  absolute URL so the PyPI page renders it; the demo-agent flow is clone-based
  (the script was never in the wheel).

### Added
- Schema-version stamp (`PRAGMA user_version = 1`) as the migration anchor for
  0.1.x namespace files (pre-stamp files upgrade in place; newer stamps are
  never downgraded).
- `LM_FORCE_STUBS` env var pins the offline stub backends in the MCP server
  (for tests/CI that must never load a model).
- Subprocess-level MCP stdio protocol test: handshake + real tool call, every
  stdout line must parse as JSON. CI matrix now covers Python 3.11/3.12.

## [0.1.2] - 2026-07-12

### Fixed
- MCP Registry namespace case: `io.github.Wuesteon/lean-memory` (the registry's
  PyPI ownership marker is compared case-exactly against the published README).

## [0.1.1] - 2026-07-12

### Added
- MCP Registry metadata: `server.json` (io.github.wuesteon/lean-memory) and an
  OIDC publish workflow; `mcp-name` ownership marker in the README (required by
  the registry's PyPI validation — the reason for this patch release).

## [0.1.0] - 2026-07-12

First public release. lean-memory is an embedded, local-first agent-memory
engine: one SQLite file per namespace, hybrid dense+sparse retrieval with
rerank, and ADD-only supersession queryable at any past point in time
(`as_of`). No server, no daemon, no mandatory cloud key.

### Added

- **MCP server** exposing memory as three tools (`memory_add`, `memory_search`,
  `memory_clear`) for Claude Code, Claude Desktop, and other MCP clients.
  Canonical install `pip install 'lean-memory[mcp,models,extract]'`
  opportunistically upgrades each backend whose extra is present (real embedder
  + reranker via `[models]`, GLiNER2 extraction via `[extract]`) and otherwise
  falls back to deterministic offline stubs. Two-minute quickstart with
  copy-paste Claude Code / Claude Desktop config and a demo GIF.
- **`Memory.search(now=...)`** — recency decay now anchors to a caller-supplied
  timestamp, so the 0.2 recency term is no longer dead on historical corpora.
- **Point-in-time queries** via `as_of` (epoch ms) with `is_latest_only=False`.
- **CI + release workflows** (GitHub Actions): offline test matrix on
  ubuntu/macOS × Python 3.10/3.13, plus build-and-publish to PyPI on `v*` tag
  via Trusted Publishing.
- PyPI metadata: keywords, classifiers, and project URLs.

### Changed

- **Default embedder is now the ungated Qwen3-Embedding-0.6B** (was a gated
  Gemma model that broke the `[models]` first run). Reranker default is
  Ettin-32M; both are pinned ungated and covered by regression tests.
- **Escalation engine recalibrated on real conversational turns.** Endpoint-
  scoped coreference/ellipsis detection replaces the whole-text pronoun scan
  (coreference escalations dropped from 65.6% to effectively nil on real
  turns), and the `prior_entity` trigger was retired (subject re-mention is
  normal discourse, measured at 52.8% of candidates). At the re-frozen
  `(typing_threshold=0.4, conf_threshold=0.4)` operating point, escalation on
  the real LongMemEval probe is **14.6%** (was ~96% pre-fix), with the residual
  being irreducible inferential-edge (`derives`) escalations. BET-2 three-gate
  revalidation PASSes at this operating point.
- **Extraction granularity calibrated** — GLiNER candidate threshold set to
  0.4, cutting the extractor from ~8 facts/turn to ~3.7 so `fact_text` reads as
  facts rather than whole utterances.
- MCP server loads models lazily (first tool call rather than import) so a
  cold-cache spawn answers the MCP handshake immediately instead of blocking on
  a model download; search output is deduplicated.

### Fixed

- Sparse BM25 retrieval arm now honors the `as_of` interval predicate.
- Known-entities handed to the router/typer are capped at the 100 most recent.

[0.1.0]: https://github.com/Wuesteon/lean-memory/releases/tag/v0.1.0
