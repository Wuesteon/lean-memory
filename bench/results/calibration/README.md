# Calibration runs — escalation & granularity (launch gate)

## Task 5 decision — GLiNER candidate threshold (granularity)

- Probe: `phase2_escalation_probe.py --slice ku --namespaces 5 --turns-per-ns 40
  --typing 0.5 --conf 0.5 --gliner-threshold 0.1 0.2 0.3 0.4 0.5` (turns=120).
- File: `2026-07-granularity-sweep.json`.

| gliner_threshold | facts/turn | median_fact_len | escalation rate |
|-----------------:|-----------:|----------------:|----------------:|
| 0.10             |       8.43 |             184 |           94.8% |
| 0.20             |       6.38 |             187 |           91.3% |
| 0.30             |       4.83 |             176 |           86.4% |
| **0.40**         |   **3.67** |         **173** |       **79.1%** |
| 0.50             |       2.63 |             171 |           63.3% |

- **Chosen: `DEFAULT_THRESHOLD = 0.4`.** Decision rule = smallest swept threshold
  meeting the granularity targets, recall-biased (keep as many candidates as the
  targets allow). facts/turn falls monotonically as threshold rises; 0.30 → 4.83
  (fails ≤ 4), 0.40 → 3.67 (passes), so 0.40 is the smallest qualifying point.
- **`median_fact_len ≤ 160` target waived** (controller adjudication, user-approved):
  the median is threshold-insensitive — it sits at 171–187 chars across the *entire*
  sweep, moving non-monotonically and never near 160. It measures how long a
  standalone `fact_text` sentence is, not how many candidates the extractor emits,
  so no GLiNER threshold can move it. The decision therefore reduces to the single
  granularity signal that *does* respond to the threshold: facts/turn ≤ 4.

### Load-bearing observations for Task 6 (escalation operating point)

Task 6 calibrates the escalation operating point against the candidate population
this threshold produces (at 0.40: seen=441, escalated=349, rate=79.1%). Two
router `by_reason` facts from this sweep constrain that work:

- **`coreference` collapsed 664 → 1** on real turns — the Task 4 endpoint-scoped
  coref/ellipsis fix is verified against real conversational data here (the 2026-07
  baseline below measured 664/1012 = 65.6% coref before the fix; post-fix it fires
  on exactly one candidate at every threshold in this sweep). The dominant
  confidence-independent escalation floor is retired.
- **`prior_entity` is now the dominant escalation reason**: 181/316 = 57% of
  candidates at threshold 0.50, and it is confidence-independent (it does not move
  with `typing`/`conf`). With coref gone, `prior_entity` alone keeps escalation well
  above the <20% design gate, so Task 6's operating-point work must scope or account
  for `prior_entity` — thresholds cannot retire it.

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

## post-Step-0 goldset check (subject-only prior_entity — Task 6 scope amendment)

- Change: `_references_prior_entity` narrowed to the SUBJECT endpoint only
  (`src/lean_memory/extract/router.py`). Re-mentioning a known entity as the OBJECT
  is normal discourse; only a non-self prior entity as the fact's SUBJECT is a
  cross-turn edge. This retires the confidence-independent `prior_entity` floor
  (57% of real candidates, see the Task 5 observation above) that no threshold pair
  could clear. Self-exemption and `introduced_here` logic unchanged; reason string
  `"prior_entity"` unchanged.
- Command: `bench/bet2_ablation.py` (offline arm — plumbing/invariants only).
- Escalation rate on the frozen goldset: **10.1%** Wilson95% [5.2%, 18.7%]
  (gate: < 20% — PASS). router by_reason: `{'prior_entity': 2, 'derives': 6,
  'coreference': 1}`.
- Unchanged from the post-Task-4 goldset rate (10.1%): the goldset's 2 `prior_entity`
  escalations are both SUBJECT-endpoint edges (the fact is about a prior third party),
  so narrowing away the object endpoint leaves them intact. The change targets the
  *conversational* 57% object-remention floor, which the goldset does not exercise —
  the same shape as the Task-4 coref fix.

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
