# Launch fix list (2026-07-12)

Gap analysis after the launch quality gate closed (spec:
`docs/superpowers/specs/2026-07-08-strategic-direction-design.md`, all 11 plan
tasks done, merged as `b4acb29`). Two P0 items were found by direct checks:
PyPI returns 404 for `lean-memory`, and there is no `.github/workflows/`.

## P0 — launch-blocking (the README's first command must work)

### P0-1 · Publish to PyPI as 0.1.0
- **Why:** `pip install lean-memory` — the first command in the README and in
  every channel listing — fails for everyone (package not published; name is
  free).
- **Steps:** bump `pyproject.toml` version `0.0.1` → `0.1.0`; `hatchling`
  build sdist+wheel; install the wheel into a fresh venv and run the import +
  quickstart smoke; publish; tag `v0.1.0`; start `CHANGELOG.md`.
- **Human-only step:** create the PyPI project (account + token, or better:
  enable Trusted Publishing for `Wuesteon/lean-memory` so the CI workflow in
  P0-2 publishes on tag without a stored secret).
- **Done when:** `pip install lean-memory` works on a machine that has never
  seen this repo.

### P0-2 · CI (GitHub Actions)
- **Why:** no test gate on PRs, no badge, and — bigger — the suite has only
  ever run on one macOS/ARM machine; Linux (what CI users and servers run) is
  unverified, including the sqlite-vec extension load path.
- **Steps:** `test.yml`: offline suite on `ubuntu-latest` + `macos-latest`,
  Python 3.10 and 3.13 matrix (suite is ~8 s — cost is nil); README badge;
  `release.yml`: build + publish to PyPI on `v*` tag via Trusted Publishing.
- **Done when:** badge green on main; a PR cannot merge red; tagging publishes.

### P0-3 · Real extraction in the canonical MCP install
- **Why:** `[mcp,models]` upgrades retrieval (Qwen3 + Ettin) but extraction
  still runs the ~9-pattern stub verb lexicon — "I adopted a cat" extracts
  nothing. Worse: the frozen escalation/granularity constants were calibrated
  ON the GLiNER path, so today's canonical install ships a pipeline the
  calibration never measured. Adding GLiNER aligns shipped reality with the
  calibrated engine.
- **Steps:** canonical install string becomes
  `pip install 'lean-memory[mcp,models,extract]'` (README + listings copy);
  `mcp_server._build_memory` opportunistically constructs `Gliner2Generator()`
  when `gliner2` is importable (same graceful-fallback pattern as the
  embedder); update the README size note (+ GLiNER2-base weights); re-run the
  Task-8-style fresh-venv smoke including a non-lexicon sentence
  ("I adopted a cat in Berlin." must extract and be retrievable).
- **Done when:** fresh-venv smoke passes with GLiNER extraction active.

## P1 — should land before the launch push (small, user-facing)

### P1-4 · Lazy model load in the MCP server
- **Why:** `MEM = _build_memory(...)` runs at import; a cold-cache first spawn
  can block the MCP client through a ~1.2 GB download with no feedback.
- **Steps:** defer model construction to first tool call (or a background
  thread at startup) and log one line per model load; keep the documented
  warm-up command as the recommended path.
- **Done when:** server responds to MCP handshake immediately on a cold cache.

### P1-5 · Document the typing-tier limitation
- **Why:** without `[llm]` + Ollama, the ~15% escalated candidates (mostly
  inferential `derives` facts) are typed by the stub — inference is
  second-class on the default path. True and fine, but must be stated, not
  discovered.
- **Steps:** one bullet in README ("what the optional `[llm]` extra buys")
  and one in ARCHITECTURE.md Known Limitations.

### P1-6 · Pin the reranker default as ungated (test)
- **Why:** `test_embedder_default.py` guards the embedder against a gated
  default regression, but `CrossEncoderReranker`'s default
  (`cross-encoder/ettin-reranker-32m-v1`) sits on the same first-run path
  unguarded (Task 8 review minor).
- **Steps:** mirror the embedder test: assert the default model name; offline.

## P2 — public roadmap, post-launch (order by demand signal)

- **`search()` side effects:** `touch()` mutates `last_access` on reads —
  non-idempotent search. Make access-tracking opt-in (Phase-2 backlog #4).
- **int8 vectors:** blocked on the upstream sqlite-vec 0.1.9 insert bug;
  flip when fixed (~0.2 pt quality cost per BET-1).
- **Scale tier:** brute-force per-namespace search is fine for the wedge;
  `LanceStore` (or IVF) for 100K+ fact namespaces.
- **Multi-process writes:** two processes on one namespace file rely on
  untested WAL semantics — document and test, or lock.
- **Typing-call cost floor:** ~7 s per constrained decode on a 3B model for
  `[llm]` users; smaller batches / explicit `num_ctx` / faster constrained
  decode (Phase-2 backlog #5).
- **Deferred cosmetics** (ledger, all triaged defer at the whole-branch
  review): mid-file `import json` in `tests/test_escalation_probe.py`; probe
  table width; `_cand_introduced_here` dead helper removal; order-coupled
  assert in `test_first_person_facts_route_direct`; "664→1" attribution
  wording in the calibration README; gated-repo example line in
  `bench/smoke_quality.py` docstring; GIF prompt-overlap frame.

## Explicitly not on this list

Benchmark runs (LongMemEval/LoCoMo) — deferred past launch by the strategy
spec; the harness is ready and the engine is now the calibrated one, so the
eventual run needs no engine work.
