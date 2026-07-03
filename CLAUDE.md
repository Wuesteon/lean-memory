# CLAUDE.md

<!-- ═══════════════════════════════════════════════════════════════════
     TEMPORARY NOTE — Phase 2 pickup. DELETE THIS ENTIRE BLOCK once the
     engine-fix backlog below is done and the Phase 2 slices have been
     re-run (i.e. result files exist in bench/results/phase2/).
     ═══════════════════════════════════════════════════════════════════ -->

## ⚠️ START HERE: Phase 2 is suspended mid-flight (2026-07-03)

Before any work on benchmarks, extraction, or retrieval, read:

1. **`docs/phase2-learnings.md`** — what we assumed, what broke, what was
   fixed, with measured numbers. Non-negotiable context; do not re-derive.
2. **`docs/superpowers/phase2-HANDOFF.md`** — operational runbook (exact
   commands, cache layout, HF Space handling) + the engine-fix backlog.

State: the Phase 2 benchmark harness (`bench/phase2_*.py`) is COMPLETE and
tested (91 green) on branch `phase2-eval-harness`. The benchmark runs were
deliberately stopped: ingest surfaced engine flaws (97% LLM-escalation rate on
conversational data being the big one) that make any score obsolete-on-arrival.
Fix the engine first, then re-run — the harness needs no changes.

**Next steps, in order:**
1. Recalibrate escalation: offline StubTyper probe of escalation vs.
   (`typing_threshold`, `conf_threshold`) on real LongMemEval turns → pick an
   operating point <20% → validate with `bench/bet2_ablation.py --sweep --real`
   and its three gates → re-freeze constants in `bench/bet2_goldset.py`.
2. Decide extraction granularity (fact_text ≈ full utterances today, ~8
   facts/turn) — calibrate the GLiNER threshold alongside step 1.
3. Fix recency anchoring (`Memory.search` should forward `now`, or anchor decay
   to corpus time — the recency term is dead on historical data).
4. Re-run Phase 2: KU slice then LoCoMo temporal (commands in the handoff doc;
   the paused HF Space `wuesteon1337/lm-typer-phase2` can be resumed, or use
   any CUDA box — the typer model digest is pinned in the manifests).
5. Housekeeping: rotate the OpenRouter key and HF token
   (`bench/.phase2_cache/*.key|*.token` — both passed through a chat session);
   merge or PR the `phase2-eval-harness` branch after a final whole-branch
   review (superpowers flow, ledger of deferred minors in
   `.superpowers/sdd/progress.md`).

<!-- ═══════════════ END TEMPORARY NOTE (delete to here) ═══════════════ -->

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
