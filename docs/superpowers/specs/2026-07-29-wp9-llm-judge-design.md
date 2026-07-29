# WP9 variant — optional LLM-as-judge tier in the contradiction ladder

**Date:** 2026-07-29 · **Status:** design recorded, implementation gated
(six-week read, or the original WP9 trigger — whichever fires first).
**Origin:** user-requested 2026-07-29 ("optionally add a knowledge LLM as a
judge checker, a second mechanism with an optional API key") during WP11.

## Motivation

The contradiction ladder's ambiguous middle band (`LOW < sim < HIGH`, no
additive cue) currently resolves to `ambiguous_default` → SUPERSEDES when no
adjudicator is supplied. That default is safe (recoverable, keeps
`is_latest=1` single-valued) but blind: rephrase-vs-replacement mistakes in
the band are decided by a coin weighted toward supersede. An optional
API-key LLM judge upgrades exactly this rung for users willing to pay a call.

## Seam (already exists — zero new architecture)

`ContradictionResolver.classify(..., llm_typer=...)` escalates the ambiguous
band to `llm_typer.adjudicate_contradiction(new_fact, existing_fact)
→ EXTENDS | SUPERSEDES` (route `"llm"`), and `Memory.add()` currently passes
`llm_typer=None`. The judge is a new backend implementing that one method,
wired into the `classify` call when configured.

## Design decisions

1. **Adjudicate, never skip.** The judge decides EXTENDS vs SUPERSEDES only.
   It never gates the WP11 restatement skip and never suppresses a write: a
   wrong adjudication is recoverable from the ADD-only audit chain; a wrong
   skip loses data silently.
2. **Enable by key presence.** `LEAN_MEMORY_JUDGE_API_KEY` set → judge active;
   unset → behavior byte-identical to today. Companion vars
   `LEAN_MEMORY_JUDGE_MODEL` and `LEAN_MEMORY_JUDGE_BASE_URL`
   (OpenAI-compatible chat-completions endpoint; works for OpenRouter,
   OpenAI, Ollama's compat server, etc.). Constructor injection
   (`Memory(judge=...)`) stays available for programmatic use and tests.
3. **Fail open to the current default.** Timeout, HTTP error, or an
   out-of-vocabulary response → fall through to `ambiguous_default`
   (SUPERSEDES), log once to stderr; `add()` never crashes on judge failure.
   Response is constrained to a single token (EXTENDS/SUPERSEDES); anything
   else is treated as failure, mirroring `classify`'s existing coercion.
4. **Offline-by-default invariant.** Deterministic stub judge for the test
   suite; no new mandatory dependency (stdlib HTTP or the existing
   `[bench]`-style client pattern; if an extra is needed it is `[judge]`).
5. **Frozen-config discipline.** Judge model + prompt are pinned alongside
   the resolver thresholds before any published number is attributed to the
   judge tier; BET-2 gate re-run required, same as the NLI variant.

## Relation to the original WP9 NLI plan

Same rung, two implementations: local NLI (DeBERTa-class ONNX, offline,
per-call free) vs API judge (higher quality ceiling, per-call cost, needs a
key). They compose: NLI as the always-on middle tier, judge as the final
escalation for what NLI still can't split. Implementation order is decided
when the packet opens, informed by WP3/telemetry evidence if available.

## Files (when implemented)

`src/lean_memory/extract/judge.py` (+ stub), `Memory.__init__`/`add()` wiring,
`tests/test_llm_judge.py`, env-var docs in README configuration section.

## Acceptance criteria

- Offline suite green with no key set; first-run path byte-identical.
- Judge failure degrades to `ambiguous_default` (assert via stub that raises).
- Ambiguous-band decisions route `"llm"` when configured (assert via stub).
- Zero new mandatory deps.
