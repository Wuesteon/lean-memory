# Calibration runs — escalation & granularity (launch gate)

## post-Task-4 goldset check (endpoint-scoped coref/ellipsis)

- Command: `bench/bet2_ablation.py` (offline arm — no Ollama; refuses a BET-2
  verdict by design, plumbing/invariants only).
- Escalation rate on the frozen goldset: **10.1%** Wilson95% [5.2%, 18.7%]
  (gate: < 20% — PASS). router by_reason: `{'prior_entity': 2, 'derives': 6,
  'coreference': 1}`.
- Unchanged from the pre-fix goldset rate (10.1%): the goldset's predicate
  sentences are already grounded with non-pronoun endpoints, so narrowing the
  whole-text pronoun scan to endpoint-level signals cannot escalate fewer of
  them. The heuristic change targets the *conversational* 65.6% coref floor
  (see 2026-07 baseline below), which the goldset does not exercise.

## 2026-07 baseline (pre-fix)

- Probe: `phase2_escalation_probe.py --slice ku --namespaces 5 --turns-per-ns 40`
  (turns=120 in the JSON — some namespaces have fewer than 40 turns)
- File: `2026-07-escalation-baseline.json`
- Best point: typing=0.3, conf=0.3 → 95.9% (971/1012 candidates escalated)
- by_reason at best point: `{"pre_flagged": 681, "low_confidence": 681, "derives": 110, "prior_entity": 555, "coreference": 664}`
- Conclusion: thresholds alone do NOT reach <20%. Every point in the 6×6 sweep
  escalates ≥95.9%; raising either threshold only pushes the rate higher
  (up to 99.4% at typing=conf=0.85). Coreference fires on 65.6% (664/1012) of
  candidates independent of confidence → router heuristic change required
  (see plan Task 4).

### Confidence-independent reasons (constant across the whole sweep)

Three reason counts do not move anywhere in the sweep — they are properties of
the candidate, not of either threshold, so no threshold pair can retire them
(`pre_flagged` is invariant to `conf_threshold` but tracks `typing_threshold`,
so it is excluded here):

- `coreference`: 664/1012 = 65.6% — the dominant confidence-independent floor.
  The current router escalates on ANY pronoun/demonstrative anywhere in the
  fact text, which fires on conversational filler. Fixing this is plan Task 4.
- `prior_entity`: 555/1012 = 54.8% — the second-largest confidence-independent
  reason. Even with coreference fully fixed, this alone keeps the rate above the
  <20% target, so the router heuristic change in Task 4 must also address (or the
  operating-point work in Task 6 must account for) `prior_entity` scoping.
- `derives`: 110/1012 = 10.9% — constant across the sweep.

Only `pre_flagged` and `low_confidence` respond to the thresholds, and they move
the wrong way (both rise as the thresholds rise), so the sweep has no minimum
below the top-left corner.

### Granularity corroboration (plan Task 5)

- candidates/turn ≈ seen/turns = 1012/120 ≈ 8.4 — the extractor emits ~8 facts
  per turn on real conversational data, corroborating the over-generation
  finding that motivates the GLiNER candidate-threshold calibration in Task 5.
