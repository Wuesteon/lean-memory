# Changelog

All notable changes to lean-memory are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
