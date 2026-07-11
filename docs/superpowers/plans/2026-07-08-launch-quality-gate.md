# Launch Quality Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the six-item launch quality gate from
`docs/superpowers/specs/2026-07-08-strategic-direction-design.md` so lean-memory's
MCP first-run experience is real-model quality, the three engine flaws from
`docs/phase2-learnings.md` are fixed, secrets are rotated, and the repo is
review-clean and documented for launch.

**Architecture:** The engine work is calibration + small API surface changes, not new
subsystems: narrow the router's coref heuristic from "pronoun anywhere in text" to
"pronoun endpoint / ungrounded subject" (the measured 69% coref-floor makes threshold
sweeps alone mathematically unable to reach the <20% escalation target), calibrate the
GLiNER candidate threshold for fact granularity, pick and re-freeze the two confidence
thresholds, and forward `now` through `Memory.search` (the `Retriever` already accepts
it at `src/lean_memory/retrieve/retriever.py:43`). The MCP path already auto-upgrades
to real models when installed (`mcp_server._build_memory`); the fix is flipping the
embedder default off the gated gemma repo and documenting the canonical install.

**Tech Stack:** Python ≥3.10, pytest (offline, deterministic stubs), GLiNER2
(`fastino/gliner2-base-v1`, HF-cached locally), sentence-transformers
(Qwen3-Embedding-0.6B + Ettin-32M), Ollama qwen2.5:3b (only for the BET-2 gate
validation run), vhs (demo GIF).

## Global Constraints

- Offline test suite must stay green at every commit: `.venv/bin/python -m pytest tests/ -q` (91 passing today; grows with new tests). No test may require network or model downloads.
- Frozen-config discipline (CLAUDE.md): any recalibrated constant is re-frozen in `bench/bet2_goldset.py` and validated with `bench/bet2_ablation.py` gates before the number is trusted.
- Escalation target: <20% on the real-turn probe AND BET-2 gate 2 (Wilson upper bound < 20%) on the goldset.
- Granularity targets (spec item 3 made numeric): mean facts/turn ≤ 4 AND median `fact_text` length ≤ 160 chars on the real-turn probe sample.
- Canonical MCP install string (spec, verbatim): `pip install 'lean-memory[mcp,models]'`.
- ADD-only discipline: nothing in this plan deletes stored history or changes the supersession model.
- Work on a feature branch `launch-gate` off `main`; commit per task. All commits end with the session's `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` / `Claude-Session:` footer (harness rule; not repeated in each commit block below).
- Task 1 (secrets) is safe to run first and independently; Tasks 2→6 are ordered (each changes the candidate population the next calibrates against); Tasks 7–8 are independent of 2–6; Tasks 9–11 come last.

---

### Task 1: Rotate the leaked OpenRouter key and HF token

Both secrets passed through a chat session (phase2-learnings; handoff §"Rotate secrets") and live gitignored at `bench/.phase2_cache/openrouter.key` and `bench/.phase2_cache/hf.token`. This must precede any public attention.

**Files:**
- Modify (content only, both gitignored): `bench/.phase2_cache/openrouter.key`, `bench/.phase2_cache/hf.token`

**Interfaces:**
- Produces: valid replacement credentials at the same two file paths (Task 11's review flow and any future bench run read them from there). No code changes.

- [ ] **Step 1: Confirm both files are gitignored (never committed)**

Run: `git check-ignore bench/.phase2_cache/openrouter.key bench/.phase2_cache/hf.token`
Expected: both paths printed (exit 0). If either is NOT ignored, STOP and treat as an incident (check `git log --all -- <path>`).

- [ ] **Step 2: Rotate the OpenRouter key (browser step — the human partner or a browser-capable session)**

At https://openrouter.ai/settings/keys: create a new key, then delete/disable the old one. Overwrite the local file with the new key (single line, no trailing newline needed):

```bash
printf '%s' 'sk-or-v1-<NEW-KEY>' > bench/.phase2_cache/openrouter.key
```

- [ ] **Step 3: Verify old key dead, new key live**

```bash
OLD_STATUS=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer <OLD-KEY>" https://openrouter.ai/api/v1/models)
NEW_STATUS=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $(cat bench/.phase2_cache/openrouter.key)" https://openrouter.ai/api/v1/models)
echo "old=$OLD_STATUS new=$NEW_STATUS"
```
Expected: `old=401 new=200`.

- [ ] **Step 4: Rotate the HF token (browser step)**

At https://huggingface.co/settings/tokens: invalidate the old write token, create a new one (write scope — it manages the `wuesteon1337/lm-typer-phase2` Space). Overwrite:

```bash
printf '%s' 'hf_<NEW-TOKEN>' > bench/.phase2_cache/hf.token
```

- [ ] **Step 5: Verify new token works and Space is still PAUSED**

```bash
.venv/bin/python -c "
from huggingface_hub import HfApi
api = HfApi(token=open('bench/.phase2_cache/hf.token').read().strip())
print('user:', api.whoami()['name'])
print('space:', api.get_space_runtime('wuesteon1337/lm-typer-phase2').stage)"
```
Expected: your username, and `space: PAUSED` (if it prints RUNNING, pause it immediately: `api.pause_space('wuesteon1337/lm-typer-phase2')` — it bills ~$1.05/h).

- [ ] **Step 6: No commit** (nothing tracked changed). Note completion in the task report.

---

### Task 2: Commit the escalation probe with a `--json` flag and an offline unit test

`bench/phase2_escalation_probe.py` exists untracked (written for backlog #1). Add machine-readable output and a deterministic test, then commit it.

**Files:**
- Modify: `bench/phase2_escalation_probe.py` (add `--json`)
- Test: `tests/test_escalation_probe.py` (create)

**Interfaces:**
- Consumes: `Gliner2Generator(model=...)` injection seam (`src/lean_memory/extract/gliner_extractor.py:127`), `run_probe(namespaces, *, typing_threshold, conf_threshold, generator) -> dict` (already defined in the probe).
- Produces: `--json PATH` writes `{"slice", "namespaces", "turns", "results": [{"typing_threshold", "conf_threshold", "seen", "escalated", "rate", "by_reason"}, ...]}`. Tasks 3, 5, 6 consume this file shape.

- [ ] **Step 1: Write the failing test**

Create `tests/test_escalation_probe.py`:

```python
"""Offline test for the escalation probe: inject a duck-typed fake GLiNER2 model."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))

from phase2_escalation_probe import run_probe  # noqa: E402

from lean_memory.extract.gliner_extractor import Gliner2Generator  # noqa: E402


class FakeGliner2:
    """Duck-types gliner2's extract_entities/extract_relations return shapes."""

    def extract_entities(self, text, entity_types, **kw):
        return {"person": [{"text": "Alice", "confidence": 0.9, "start": 0, "end": 5}]}

    def extract_relations(self, text, relation_types, **kw):
        return {
            "relation_extraction": {
                "works_at": [
                    {
                        "head": {"text": "Alice", "confidence": 0.9, "start": 0, "end": 5},
                        "tail": {"text": "Acme", "confidence": 0.8, "start": 15, "end": 19},
                    }
                ]
            }
        }


def test_run_probe_offline_shape_and_determinism():
    gen = Gliner2Generator(model=FakeGliner2())
    namespaces = [["Alice works at Acme.", "Alice works at Acme."]]
    r1 = run_probe(namespaces, typing_threshold=0.5, conf_threshold=0.5, generator=gen)
    r2 = run_probe(namespaces, typing_threshold=0.5, conf_threshold=0.5, generator=gen)
    assert r1 == r2  # deterministic
    assert r1["seen"] == 2  # FakeGliner2 emits 1 candidate/turn × 2 turns
    assert 0.0 <= r1["rate"] <= 1.0
    # subset, not equality: Task 5 adds shape-metric keys to this dict
    assert {"typing_threshold", "conf_threshold", "seen", "escalated", "rate", "by_reason"}.issubset(r1)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_escalation_probe.py -v`
Expected: PASS already for `run_probe` (it exists) — if it passes, that step is the safety net confirming the fake-model seam works; the *failing* part comes next.

- [ ] **Step 3: Write the failing test for `--json`**

Append to `tests/test_escalation_probe.py`:

```python
import json


def test_json_flag_writes_results(tmp_path):
    from phase2_escalation_probe import write_json

    out = tmp_path / "probe.json"
    results = [{"typing_threshold": 0.5, "conf_threshold": 0.5, "seen": 2,
                "escalated": 1, "rate": 0.5, "by_reason": {"coreference": 1}}]
    write_json(out, slice_="ku", n_namespaces=1, total_turns=2, results=results)
    data = json.loads(out.read_text())
    assert data["slice"] == "ku"
    assert data["results"][0]["rate"] == 0.5
```

Run: `.venv/bin/python -m pytest tests/test_escalation_probe.py::test_json_flag_writes_results -v`
Expected: FAIL — `ImportError: cannot import name 'write_json'`.

- [ ] **Step 4: Implement `write_json` and the `--json` flag**

In `bench/phase2_escalation_probe.py`, add `import json` to the imports, then above `main()`:

```python
def write_json(path: Path, *, slice_: str, n_namespaces: int, total_turns: int,
               results: list[dict]) -> None:
    """Machine-readable sweep output (consumed by the calibration report)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "slice": slice_,
        "namespaces": n_namespaces,
        "turns": total_turns,
        "results": results,
    }, indent=2))
```

In `main()`, add the argument next to the existing ones:

```python
    ap.add_argument("--json", type=Path, default=None, help="write sweep results to this JSON file")
```

and after the sweep loop (after the `best = ...` block prints), before `return 0`:

```python
    if args.json:
        write_json(args.json, slice_=args.slice, n_namespaces=len(namespaces),
                   total_turns=total_turns, results=results)
        print(f"JSON written: {args.json}")
```

- [ ] **Step 5: Run the tests and the full suite**

Run: `.venv/bin/python -m pytest tests/test_escalation_probe.py -v && .venv/bin/python -m pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add bench/phase2_escalation_probe.py tests/test_escalation_probe.py
git commit -m "feat(bench): escalation-threshold probe on real LME turns, with --json output"
```

---

### Task 3: Baseline escalation sweep — measure before touching the router

Produce the numbers every later decision cites. This confirms (or refutes) the coref-floor hypothesis: `by_reason` from ingest telemetry showed coreference on 354/512 candidates (69%), which no threshold pair can push under 20%.

**Files:**
- Create: `bench/results/calibration/2026-07-escalation-baseline.json` (probe output)
- Create: `bench/results/calibration/README.md` (running analysis notes)

**Interfaces:**
- Consumes: `bench/phase2_escalation_probe.py --json` (Task 2); GLiNER2 weights (HF-cached: `~/.cache/huggingface/hub/models--fastino--gliner2-base-v1`); dataset at `bench/.phase2_cache/data/longmemeval_oracle.json` (already downloaded + sha256-pinned).
- Produces: the baseline JSON + a written analysis later tasks cite. No code.

- [ ] **Step 1: Sanity-check prerequisites**

Run: `.venv/bin/python -c "import gliner2; print('ok')" && ls bench/.phase2_cache/data/longmemeval_oracle.json`
Expected: `ok` and the file path. If gliner2 is missing: `.venv/bin/pip install -e '.[extract]'`.

- [ ] **Step 2: Run the baseline sweep (offline, no LLM; ~20–40 min CPU)**

```bash
.venv/bin/python bench/phase2_escalation_probe.py --slice ku --namespaces 5 --turns-per-ns 40 \
  --json bench/results/calibration/2026-07-escalation-baseline.json
```
Expected: the 6×6 sweep table prints; rates near the top-left (low thresholds) are the lowest; the summary states whether any point is <20%. Based on the ingest telemetry, expect NO point under ~60% — the coreference reason fires on most conversational turns regardless of confidence.

- [ ] **Step 3: Write the analysis note**

Create `bench/results/calibration/README.md`:

```markdown
# Calibration runs — escalation & granularity (launch gate)

## 2026-07 baseline (pre-fix)

- Probe: `phase2_escalation_probe.py --slice ku --namespaces 5 --turns-per-ns 40`
- File: `2026-07-escalation-baseline.json`
- Best point: typing=<X>, conf=<Y> → <Z>% (fill from the run)
- by_reason at best point: <paste>
- Conclusion: thresholds alone {do / do not} reach <20%. Coreference fires on
  <N>% of candidates independent of confidence → router heuristic change
  required (see plan Task 4).
```

Fill every `<...>` from the actual JSON — no placeholders may survive the commit.

- [ ] **Step 4: Commit**

```bash
git add bench/results/calibration/
git commit -m "bench: baseline escalation sweep on real LME turns (pre-router-fix)"
```

---

### Task 4: Narrow the router's coref/ellipsis heuristic to endpoint-level signals

The current `_has_coref_or_ellipsis` (`src/lean_memory/extract/router.py:359-369`) escalates when ANY pronoun/demonstrative (`it, this, that, there, then, ...`) appears ANYWHERE in the fact text, or the text leads with a bare verb. On goldset predicate sentences that's rare; on conversational turns it fires almost always. The principled contract: a candidate is coref-unresolvable only if **its own endpoints** aren't grounded — the subject/object IS a pronoun, or the span has no grounded subject at all. A stray "that" elsewhere in a sentence whose head and tail GLiNER already grounded to real names does not make the fact unresolvable.

**Files:**
- Modify: `src/lean_memory/extract/router.py` (replace `_has_coref_or_ellipsis`, update `_reasons`)
- Test: `tests/test_router.py` (new cases; update any existing case that asserted the pronoun-anywhere contract)

**Interfaces:**
- Consumes: `Candidate` fields via the existing accessors `_cand_subject_name`, `_cand_object_name`, `_cand_text`; `subject_span` attribute; `_norm()`; `self_key` already threaded through `_reasons(cand, known, self_key)`.
- Produces: `RecallBiasedRouter._coref_or_ellipsis(cand, text, self_key) -> bool` (replaces the static `_has_coref_or_ellipsis(text)`). Escalation reason string stays `"coreference"` — Tasks 5/6 and the ablation harness read `by_reason` unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_router.py` (match its existing import style; `Candidate` comes from `lean_memory.extract.taxonomy`):

```python
from lean_memory.extract.router import RecallBiasedRouter
from lean_memory.extract.taxonomy import Candidate


def _grounded_cand(subject="Alice", obj="Acme", predicate="works_at",
                   text=None, conf=0.9, subject_span=(0, 5), object_span=(15, 19)):
    return Candidate(
        subject_name=subject, predicate=predicate, object_literal=obj,
        fact_text=text or f"{subject} works at {obj}.", valid_at=0,
        confidence=conf, source="test",
        subject_span=subject_span, object_span=object_span, needs_typing=False,
    )


def test_grounded_endpoints_with_stray_pronoun_route_direct():
    """Conversational filler ('that', 'it', 'there') must not escalate a fully
    grounded candidate — this was the 69% coref-floor on real turns."""
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = _grounded_cand(text="Alice works at Acme now and that office is downtown.")
    to_type, direct = r.route([cand])
    assert cand in direct
    assert r.last_stats["by_reason"].get("coreference", 0) == 0


def test_pronoun_subject_escalates_as_coreference():
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = _grounded_cand(subject="She", text="She works at Acme.")
    to_type, _ = r.route([cand])
    assert cand in to_type
    assert r.last_stats["by_reason"]["coreference"] == 1


def test_pronoun_object_escalates_as_coreference():
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = _grounded_cand(obj="it", text="Alice really likes it.", predicate="likes")
    to_type, _ = r.route([cand])
    assert cand in to_type
    assert r.last_stats["by_reason"]["coreference"] == 1


def test_ungrounded_subject_with_ellipsis_lead_escalates():
    """Zero-pronoun clause: no subject span, not the self-entity, leads with a
    conjunction/bare verb — still coref (subject carried from prior turn)."""
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = Candidate(
        subject_name="Berlin", predicate="lives_in", object_literal="Berlin",
        fact_text="and moved to Berlin last spring", valid_at=0,
        confidence=0.9, source="test",
        subject_span=None, object_span=(13, 19), needs_typing=False,
    )
    to_type, _ = r.route([cand])
    assert cand in to_type
    assert r.last_stats["by_reason"]["coreference"] == 1


def test_first_person_self_entity_not_coref():
    """'I moved to Berlin' → subject resolved to the self entity → grounded."""
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = Candidate(
        subject_name="user", predicate="lives_in", object_literal="Berlin",
        fact_text="I moved to Berlin last week.", valid_at=0,
        confidence=0.9, source="test",
        subject_span=None, object_span=(11, 17), needs_typing=False,
    )
    to_type, direct = r.route([cand])
    assert cand in direct
```

- [ ] **Step 2: Run them to verify current failures**

Run: `.venv/bin/python -m pytest tests/test_router.py -v -k "coref or pronoun or ellipsis or self_entity"`
Expected: `test_grounded_endpoints_with_stray_pronoun_route_direct` and `test_first_person_self_entity_not_coref` FAIL (old contract escalates both); the pronoun-endpoint ones may already pass (pronouns also appear in text).

- [ ] **Step 3: Implement the endpoint-scoped heuristic**

In `src/lean_memory/extract/router.py`, add near the other regex constants (`_ELLIPSIS_LEAD` and `_LEADING_VERB` stay — they are still used; `_COREF_PRONOUNS` gets deleted in this step):

```python
# Endpoint-level pronouns/demonstratives: a candidate whose OWN subject or object
# is one of these is not self-contained. This replaces the old whole-text scan,
# which fired on conversational filler ("that", "it", "there") in ~69% of real
# turns (2026-07 baseline probe) and put a hard floor over the <20% target.
_ENDPOINT_PRONOUNS = frozenset({
    "he", "him", "his", "she", "her", "hers", "they", "them", "their", "theirs",
    "it", "its", "this", "that", "these", "those",
    "the former", "the latter", "the same",
})
```

Delete the `_COREF_PRONOUNS` regex (grep first: `grep -rn "_COREF_PRONOUNS" src tests bench` — remove/adjust every use). Replace the static method `_has_coref_or_ellipsis` with an instance method:

```python
    def _coref_or_ellipsis(self, cand: Candidate, text: str, self_key: str) -> bool:
        """Endpoint-scoped coref: escalate iff the candidate's OWN endpoints are
        unresolvable — a pronoun endpoint, or no grounded subject on a clause that
        leads like a subject-dropped continuation. A pronoun elsewhere in the
        sentence is conversational filler, not a resolution problem."""
        for endpoint in (_cand_subject_name(cand), _cand_object_name(cand)):
            if _norm(endpoint) in _ENDPOINT_PRONOUNS:
                return True
        subject_key = _norm(_cand_subject_name(cand))
        grounded = (
            getattr(cand, "subject_span", None) is not None
            or (bool(subject_key) and subject_key == self_key)
        )
        if not grounded and text and (_ELLIPSIS_LEAD.match(text) or _LEADING_VERB.match(text)):
            return True
        return False
```

In `_reasons` (router.py:345-347), change the call site:

```python
        # 2. Coreference / ellipsis / zero-pronoun: the candidate itself is not
        #    self-contained (endpoint-scoped — see _coref_or_ellipsis).
        if self._coref_or_ellipsis(cand, text, self_key):
            reasons.append(REASON_COREF)
```

- [ ] **Step 4: Run the router tests, fix the pre-existing cases that encoded the old contract**

Run: `.venv/bin/python -m pytest tests/test_router.py -v`
For any pre-existing failure: if the test constructs a candidate with grounded non-pronoun endpoints but expected `coreference` because of a mid-sentence pronoun, update it to the new contract (give it a pronoun endpoint, or assert it routes direct) — with a comment noting the 2026-07 recalibration. Do NOT weaken tests about pre_flagged/low_confidence/prior_entity/derives.

- [ ] **Step 5: Run the whole suite (extraction pipeline tests exercise routing end-to-end)**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all PASS (fix regressions the same way as step 4 — contract-update only).

- [ ] **Step 6: Verify against the frozen goldset (offline arm — no Ollama needed)**

Run: `.venv/bin/python bench/bet2_ablation.py 2>&1 | tail -30`
Expected: the offline run prints stats and REFUSES a verdict (by design — offline mode). Confirm the escalation-rate line still lands under 20% on the goldset (it was 10.1%; the narrowed heuristic can only lower it). Record the printed rate in `bench/results/calibration/README.md` under a "post-Task-4 goldset check" bullet.

- [ ] **Step 7: Commit**

```bash
git add src/lean_memory/extract/router.py tests/test_router.py bench/results/calibration/README.md
git commit -m "fix(router): endpoint-scoped coref/ellipsis — stop escalating on conversational filler"
```

---

### Task 5: Calibrate GLiNER candidate granularity (facts should read as facts)

`DEFAULT_THRESHOLD = 0.1` (`src/lean_memory/extract/gliner_extractor.py:73`) over-generates: ~8 facts/turn with `fact_text` ≈ whole utterances on real data. Extend the probe to measure shape vs `threshold`, pick the smallest threshold meeting the granularity targets, and raise the default. This runs BEFORE the escalation-threshold pick (Task 6) because it changes the candidate population Task 6 calibrates against.

**Files:**
- Modify: `bench/phase2_escalation_probe.py` (shape metrics + `--gliner-threshold` sweep)
- Modify: `src/lean_memory/extract/gliner_extractor.py:73` (`DEFAULT_THRESHOLD`)
- Test: `tests/test_escalation_probe.py` (shape-metric assertions)

**Interfaces:**
- Consumes: `run_probe(...)` and `write_json(...)` from Tasks 2–4.
- Produces: `run_probe` result dict gains `"turns"`, `"facts_per_turn"`, `"median_fact_len"` keys; probe gains `--gliner-threshold T [T ...]` (sweeps `generator.threshold`). Task 6 reads the enriched JSON.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_escalation_probe.py`:

```python
def test_run_probe_reports_shape_metrics():
    gen = Gliner2Generator(model=FakeGliner2())
    namespaces = [["Alice works at Acme.", "Alice works at Acme."]]
    r = run_probe(namespaces, typing_threshold=0.5, conf_threshold=0.5, generator=gen)
    assert r["turns"] == 2
    assert r["facts_per_turn"] == r["seen"] / 2
    assert r["median_fact_len"] > 0  # FakeGliner2 spans → non-empty fact_text
```

Run: `.venv/bin/python -m pytest tests/test_escalation_probe.py::test_run_probe_reports_shape_metrics -v`
Expected: FAIL — `KeyError: 'turns'`.

- [ ] **Step 2: Implement the shape metrics in `run_probe`**

In `bench/phase2_escalation_probe.py`, add `import statistics` to the imports. Inside `run_probe`, track turns and fact lengths (additions marked):

```python
    seen = 0
    escalated = 0
    turns = 0                     # NEW
    fact_lengths: list[int] = []  # NEW
    by_reason: Counter = Counter()

    for turns_list in namespaces:
        known: deque[str] = deque(maxlen=_KNOWN_ENTITIES_CAP)
        for turn_text in turns_list:
            turns += 1            # NEW
            episode = Episode(namespace="probe", raw=turn_text, t_ref=0, source="user")
            candidates = generator.generate(episode)
            if not candidates:
                continue
            fact_lengths.extend(len(c.fact_text or "") for c in candidates)  # NEW
            ...
```

and extend the return dict:

```python
    return {
        "typing_threshold": typing_threshold,
        "conf_threshold": conf_threshold,
        "gliner_threshold": generator.threshold,          # NEW
        "seen": seen,
        "escalated": escalated,
        "rate": (escalated / seen) if seen else 0.0,
        "by_reason": dict(by_reason),
        "turns": turns,                                    # NEW
        "facts_per_turn": (seen / turns) if turns else 0.0,  # NEW
        "median_fact_len": int(statistics.median(fact_lengths)) if fact_lengths else 0,  # NEW
    }
```

Add the sweep flag in `main()`:

```python
    ap.add_argument("--gliner-threshold", type=float, nargs="+", default=None,
                    help="also sweep the GLiNER candidate threshold (default: model default only)")
```

and wrap the existing sweep loops in an outer loop (set `generator.threshold = g` per point; when the flag is absent, iterate over `(generator.threshold,)` so behavior is unchanged). Print `facts/turn` and `med_len` columns in the table.

- [ ] **Step 3: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_escalation_probe.py -v && .venv/bin/python -m pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 4: Run the granularity sweep on real turns**

```bash
.venv/bin/python bench/phase2_escalation_probe.py --slice ku --namespaces 5 --turns-per-ns 40 \
  --typing 0.5 --conf 0.5 --gliner-threshold 0.1 0.2 0.3 0.4 0.5 \
  --json bench/results/calibration/2026-07-granularity-sweep.json
```
Expected: facts/turn falls as threshold rises (baseline ~8 at 0.1). Pick the SMALLEST threshold with `facts_per_turn ≤ 4` AND `median_fact_len ≤ 160` (recall bias: keep as many candidates as the targets allow).

- [ ] **Step 5: Raise the default and document**

In `src/lean_memory/extract/gliner_extractor.py:73`, change `DEFAULT_THRESHOLD = 0.1` to the chosen value, updating the comment to cite the sweep file. Append the decision (chosen value + measured facts/turn + median length) to `bench/results/calibration/README.md`.

- [ ] **Step 6: Full suite, then commit**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all PASS (stubs don't call the model; threshold default only affects the real backend).

```bash
git add bench/phase2_escalation_probe.py tests/test_escalation_probe.py \
        src/lean_memory/extract/gliner_extractor.py bench/results/calibration/
git commit -m "feat(extract): calibrate GLiNER candidate threshold for fact granularity"
```

---

### Task 6: Pick the escalation operating point, re-freeze, validate the BET-2 gates

With the coref heuristic fixed (Task 4) and granularity set (Task 5), sweep both confidence thresholds on real turns, pick the most recall-biased point that clears the target with margin, update the defaults, re-freeze, and run the full three-gate validation.

**Files:**
- Modify: `src/lean_memory/extract/gliner_extractor.py:77` (`DEFAULT_TYPING_THRESHOLD`)
- Modify: `src/lean_memory/extract/router.py:243` (`conf_threshold` default)
- Modify: `bench/bet2_goldset.py:54-56` (re-freeze `FROZEN_CONF_THRESHOLD`, add `FROZEN_TYPING_THRESHOLD`)
- Create: `bench/results/calibration/2026-07-escalation-postfix.json`

**Interfaces:**
- Consumes: enriched probe JSON (Task 5 shape).
- Produces: the frozen constants every future BET-2/Phase-2 run reads. Selection rule for later auditors: **highest** `(typing_threshold, conf_threshold)` pair (most recall-biased) with probe `rate < 0.15` (margin under the 20% gate), tie-broken by higher `conf_threshold`.
- Produces (Step 0): `_references_prior_entity` narrowed to the SUBJECT endpoint — reason string `"prior_entity"` unchanged.

- [ ] **Step 0: Narrow `prior_entity` to the subject endpoint (scope amendment, user-approved 2026-07-10)**

The Task 5 sweep measured `prior_entity` at 57% of candidates (181/316 at gliner 0.5) — confidence-independent, so no threshold pair can reach <20% without this change (same situation Task 4 fixed for coref; the calibration README documents it). Approved contract: an endpoint matching a previously-seen entity is a *hard cross-turn edge* only when it is the candidate's **subject** (a non-self third party the fact is about); re-mentioning a known entity as the **object** is normal discourse and routes on the other signals.

TDD — add to `tests/test_router.py`:

```python
def test_object_remention_of_known_entity_routes_direct():
    """Re-mentioning a known entity as the OBJECT is normal discourse, not a
    cross-turn edge — measured at 57% of real candidates (2026-07 sweep)."""
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = _grounded_cand(subject="user", obj="Acme", text="I visited Acme again today.",
                          predicate="works_at", subject_span=None)
    to_type, direct = r.route([cand], known_entities={"Acme"})
    assert cand in direct
    assert r.last_stats["by_reason"].get("prior_entity", 0) == 0


def test_prior_subject_still_escalates():
    """A non-self subject seen in a PRIOR turn is a genuine cross-turn edge."""
    r = RecallBiasedRouter(conf_threshold=0.3)
    cand = _grounded_cand(subject="Acme", obj="Berlin", predicate="located_in",
                          text="Acme is located in Berlin.")
    to_type, _ = r.route([cand], known_entities={"Acme"})
    assert cand in to_type
    assert r.last_stats["by_reason"]["prior_entity"] == 1
```

(Adjust `_grounded_cand` usage to the helper's actual signature; the first test's subject is the self entity so the pre-existing self-exemption must also keep it direct.) Run them: the object-remention test FAILS on the old contract. Implement: in `_references_prior_entity` (`src/lean_memory/extract/router.py`), check ONLY `_cand_subject_name(cand)` — drop the object endpoint from the loop; keep the `introduced_here` and self-entity exemptions unchanged; update the method docstring to cite the 57% measurement. Update any pre-existing test that encoded the object-endpoint contract (contract-update only, with a comment). Then re-run the offline goldset check (`.venv/bin/python bench/bet2_ablation.py 2>&1 | tail -30`) and record the escalation rate as a "post-Step-0 goldset check" bullet in `bench/results/calibration/README.md` — it must stay <20%.

- [ ] **Step 0b: Drop `prior_entity` as an escalation trigger (second scope amendment, user-approved 2026-07-10)**

The post-Step-0 sweep (`2026-07-escalation-postfix.json`, 8 namespaces / 192 turns / 704 candidates) measured subject-only `prior_entity` still firing on 52.8% (372/704): subject re-mention is normal discourse in real dialogs, not a rare hard case. Floor decomposition at (0.3, 0.3): prior_entity 372, derives 102, coreference 1, zero low-confidence — so <20% is unreachable with the trigger and ≈14.6% without it. Rationale for removal: entity linking is deterministic by name (`upsert_entity`); ambiguous references escalate via coref; inferential edges via derives; supersession ambiguity has its own cheap-then-escalate resolver downstream. Third strike for this signal (73.7% false-escalation bug in BET-2, then the self-exemption, then subject-only). Quality proof is Step 5's gate 1 (F1 delta on the direct bucket now includes ex-prior_entity candidates).

Implementation contract:
- Remove trigger #3 from `_reasons` and delete `_references_prior_entity`; keep `REASON_PRIOR_ENTITY` defined with a deprecation comment (historical probe JSONs reference the string).
- KEEP the `known_entities` parameter on `route()`/`should_escalate()` (API stability; `Memory.add` passes it and the typer uses the names as context) — docstring notes it no longer drives escalation.
- TDD: update the prior_entity router tests to the new contract (known-subject re-mention routes direct; keep one test asserting `by_reason` never contains `prior_entity`). Contract-update only; do not weaken coref/derives/low-conf/pre_flagged tests.
- Offline goldset check (`bench/bet2_ablation.py`, offline) — record the rate as a "post-Step-0b goldset check" bullet in the calibration README (must stay <20%).
- Commit separately: `fix(router): drop prior_entity escalation trigger (52.8% subject-remention floor on real dialogs)`.

- [ ] **Step 1: Run the post-fix sweep**

```bash
.venv/bin/python bench/phase2_escalation_probe.py --slice ku --namespaces 8 --turns-per-ns 40 \
  --json bench/results/calibration/2026-07-escalation-postfix.json
```
(8 namespaces now — the decision run deserves a bigger sample than the baseline.)
Expected: with Step 0 landed, multiple points under 20% (residual floor ≈ derives ~13% + low-confidence at the swept gates). If NO point is under 20%, STOP — re-read the by_reason breakdown, file the dominant reason as a new analysis bullet in `bench/results/calibration/README.md`, and raise it with the human partner before proceeding (further reason-scoping beyond Step 0 remains out of approved scope).

- [ ] **Step 2: Apply the operating point**

Using the selection rule above, set:
- `src/lean_memory/extract/gliner_extractor.py:77`: `DEFAULT_TYPING_THRESHOLD = <chosen>` (update its comment to cite the postfix JSON).
- `src/lean_memory/extract/router.py:243`: `def __init__(self, conf_threshold: float = <chosen>) -> None:` (update the comment likewise).

- [ ] **Step 3: Re-freeze in the goldset module**

In `bench/bet2_goldset.py` (lines 54–56), update and extend the frozen block:

```python
FROZEN_HIGH_SIM = 0.80
FROZEN_LOW_SIM = 0.45
FROZEN_CONF_THRESHOLD = <chosen conf>   # re-frozen 2026-07: endpoint-scoped coref + real-turn probe
FROZEN_TYPING_THRESHOLD = <chosen typing>  # frozen 2026-07 (was implicit 0.5)
```

Grep for consumers: `grep -rn "FROZEN_CONF_THRESHOLD" bench/` — every place that constructs a router for scoring must read the frozen constant (it should already; fix any hardcoded 0.5).

- [ ] **Step 4: Offline suite green**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS. If a test hardcodes 0.5 as *the default* (not as an explicit argument), update it to reference `DEFAULT_TYPING_THRESHOLD` / the router's default.

- [ ] **Step 5: Full three-gate validation (real models + local Ollama)**

```bash
ollama serve >/dev/null 2>&1 &   # skip if already running
ollama pull qwen2.5:3b
.venv/bin/python bench/bet2_ablation.py --real --decodes 3 2>&1 | tee bench/results/calibration/2026-07-bet2-revalidation.txt
```
Expected output (the gate block near the end):
```
  gate 1 (delta upper ≤ 3.0pp): PASS
  gate 2 (escalation upper < 20%): PASS
  gate 3 (hybrid derives-recall ... ≥ 100%-LLM ... − 0.10): PASS
```
All three must PASS. Gate 3 is the recall guard — if it FAILS, the Task 4/6 changes cut too deep: lower the chosen thresholds one probe step and repeat from Step 2 (do NOT touch the goldset).

- [ ] **Step 6: Record and commit**

Append the frozen point + gate verdicts to `bench/results/calibration/README.md`.

```bash
git add src/lean_memory/extract/gliner_extractor.py src/lean_memory/extract/router.py \
        bench/bet2_goldset.py bench/results/calibration/
git commit -m "feat(extract): re-freeze escalation operating point from real-turn calibration"
```

---

### Task 7: Forward `now` through `Memory.search` (revive the recency term)

`Retriever.retrieve` already takes `now` (`src/lean_memory/retrieve/retriever.py:43`); `Memory.search` (`src/lean_memory/memory.py:173-186`) doesn't forward it, so on historical corpora `exp(-λ·age) ≈ 0` for every fact and the 0.2 recency weight is dead.

**Files:**
- Modify: `src/lean_memory/memory.py:173-186`
- Test: `tests/test_search_now.py` (create)

**Interfaces:**
- Consumes: `Retriever.retrieve(query, k, *, as_of, is_latest_only, now)`.
- Produces: `Memory.search(namespace, query, k=5, *, as_of=None, is_latest_only=True, now=None)` — `now` in epoch-ms anchors recency decay; `None` keeps wall-clock behavior. MCP server and README examples rely on the default staying wall-clock.

- [ ] **Step 1: Write the failing test**

Create `tests/test_search_now.py`:

```python
"""Recency anchoring: Memory.search(now=...) must reach the decay term.

Regression for phase2-learnings assumption #8 — with 2023 data read in 2026,
exp(-λ·age) ≈ 0 for every fact and the 0.2 recency weight de-ranks nothing.
"""
from lean_memory import Memory

MONTH_MS = 30 * 24 * 60 * 60 * 1000
T0 = 1_600_000_000_000  # a fixed historical epoch-ms


def _corpus(mem, ns):
    # Different slots (no supersession between them); identical shape, different age.
    mem.add(ns, "I adopted a cat in Berlin.", t_ref=T0)
    mem.add(ns, "I adopted a dog in Berlin.", t_ref=T0 + 11 * MONTH_MS)


def test_now_anchors_recency(tmp_path):
    mem = Memory(root=tmp_path)
    _corpus(mem, "anchored")
    hits = mem.search("anchored", "adopted", k=5, is_latest_only=False,
                      now=T0 + 12 * MONTH_MS)
    rec = {h.fact.fact_text: h.recency for h in hits}
    assert rec["I adopted a dog in Berlin."] > 0.3     # 1 month old  → e^-1 ≈ 0.37
    assert rec["I adopted a cat in Berlin."] < 0.001   # 12 months old → e^-12
    mem.close()


def test_default_now_is_wall_clock(tmp_path):
    mem = Memory(root=tmp_path)
    _corpus(mem, "wallclock")
    hits = mem.search("wallclock", "adopted", k=5, is_latest_only=False)
    assert all(h.recency < 0.001 for h in hits)  # historical corpus, real 'now'
    mem.close()
```

NOTE for the implementer: if the two texts land in the SAME (subject, predicate) slot and supersession retires the cat fact, `is_latest_only=False` still surfaces both — the assertions read `recency`, not presence. If `hits` lacks one text, widen `k` before suspecting the feature.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_search_now.py -v`
Expected: FAIL — `TypeError: search() got an unexpected keyword argument 'now'`.

- [ ] **Step 3: Implement the pass-through**

In `src/lean_memory/memory.py`, replace the `search` definition:

```python
    def search(
        self,
        namespace: str,
        query: str,
        k: int = 5,
        *,
        as_of: Optional[int] = None,
        is_latest_only: bool = True,
        now: Optional[int] = None,
    ) -> list[RetrievedFact]:
        """`now` (epoch ms) anchors the recency-decay term — pass the corpus's
        present when querying historical data, else the wall clock is used and
        recency is ≈0 for everything old (the term de-ranks nothing)."""
        store = self._store(namespace)
        retriever = Retriever(store, self.embedder, self.reranker)
        return retriever.retrieve(
            query, k, as_of=as_of, is_latest_only=is_latest_only, now=now
        )
```

- [ ] **Step 4: Run the test and the suite**

Run: `.venv/bin/python -m pytest tests/test_search_now.py -v && .venv/bin/python -m pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lean_memory/memory.py tests/test_search_now.py
git commit -m "feat(retrieve): Memory.search(now=...) anchors recency decay (dead on historical corpora)"
```

---

### Task 8: Flip the default embedder off the gated gemma repo

`SentenceTransformerEmbedder` defaults to `google/embeddinggemma-300m` (`src/lean_memory/embed/sentence_transformer.py:29`) — a **gated** HF repo requiring a license accept. The MCP server constructs it with no args (`src/lean_memory/mcp_server.py:42`), so the canonical `[mcp,models]` install fails at first model load for every new user. ARCHITECTURE.md already names Qwen3-0.6B as the ungated, stronger choice.

**Files:**
- Modify: `src/lean_memory/embed/sentence_transformer.py:27-38` (default + docstring)
- Test: `tests/test_embedder_default.py` (create)

**Interfaces:**
- Consumes: `KNOWN_MODELS = {"google/embeddinggemma-300m": 768, "Qwen/Qwen3-Embedding-0.6B": 1024}` (line 17).
- Produces: `SentenceTransformerEmbedder()` defaults to `model_name="Qwen/Qwen3-Embedding-0.6B"`, `dim == 1024`. Anything persisting vectors gets the new dim for NEW namespace files only (dim is fixed per store at creation — existing user DBs keep working because their stores were created with their embedder's dim).

- [ ] **Step 1: Write the failing test**

Create `tests/test_embedder_default.py`:

```python
"""The default real embedder must be an UNGATED HF repo (launch gate item 1).

google/embeddinggemma-300m requires a license accept and breaks the canonical
`pip install 'lean-memory[mcp,models]'` first run. Construction is lazy — this
test never loads a model and stays offline.
"""
from lean_memory.embed.sentence_transformer import SentenceTransformerEmbedder


def test_default_is_ungated_qwen3():
    e = SentenceTransformerEmbedder()
    assert e.model_name == "Qwen/Qwen3-Embedding-0.6B"
    assert e.dim == 1024
```

Run: `.venv/bin/python -m pytest tests/test_embedder_default.py -v`
Expected: FAIL — model_name is `google/embeddinggemma-300m`.

- [ ] **Step 2: Flip the default**

In `src/lean_memory/embed/sentence_transformer.py:29`, change:

```python
        model_name: str = "Qwen/Qwen3-Embedding-0.6B",
```

Update the module docstring (lines 4-6) to say Qwen3-0.6B is the default (ungated, MTEB-R 64.65) and EmbeddingGemma remains available by name for multilingual use (gated repo — license accept required).

- [ ] **Step 3: Check for other gemma assumptions**

Run: `grep -rn "embeddinggemma" src tests bench examples docs README.md ARCHITECTURE.md`
Expected: only the KNOWN_MODELS entry, the docstring, and prose mentions. Fix any code path that assumes dim=768 for the default.

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all PASS (offline tests use FakeEmbedder; only the default *name* changed).

- [ ] **Step 5: End-to-end smoke of the canonical install (real download, one-time)**

```bash
SMOKE=$(mktemp -d) && python3 -m venv "$SMOKE/venv" && "$SMOKE/venv/bin/pip" -q install -e '.[mcp,models]'
LM_DATA_ROOT="$SMOKE/data" "$SMOKE/venv/bin/python" - <<'EOF'
from pathlib import Path
import os
from lean_memory.mcp_server import _build_memory
mem = _build_memory(Path(os.environ["LM_DATA_ROOT"]))
mem.add("smoke", "I work at Globex now.")
mem.add("smoke", "My favorite editor is Neovim.")
mem.add("smoke", "I live in Konstanz.")
top = mem.search("smoke", "which editor does the user prefer?", k=1)[0]
print("TOP:", top.fact.fact_text)
assert "Neovim" in top.fact.fact_text
EOF
```
Expected: models download WITHOUT any license/auth prompt; prints `TOP: My favorite editor is Neovim.` (or the stub-extracted fact containing "Neovim"). This is the spec's done-criterion for gate item 1. Record the total download size shown by pip/HF for the README (Task 9).

- [ ] **Step 6: Commit**

```bash
git add src/lean_memory/embed/sentence_transformer.py tests/test_embedder_default.py
git commit -m "fix(embed): default to ungated Qwen3-Embedding-0.6B (gated gemma broke the [models] first run)"
```

---

### Task 9: Two-minute README quickstart + demo GIF

Rewrite the README's MCP section around the canonical install, with honest download sizes, copy-paste client configs, a warm-up command, and a recorded GIF of the add→restart→recall loop.

**Files:**
- Modify: `README.md` (MCP section, "Real Model Quality" section, top TODO block)
- Create: `docs/assets/quickstart.tape` (vhs script), `docs/assets/quickstart.gif`

**Interfaces:**
- Consumes: Task 8's measured download size; `examples/mcp_config.json`; `lean-memory-mcp` console script.
- Produces: README copy other channels (registry listings, Show HN) will reuse verbatim. Task 10 updates the surrounding docs.

- [ ] **Step 1: Rewrite the README "MCP Server" section**

Replace the current section (README.md:80-89) with:

````markdown
## MCP Server — memory for Claude Code / Claude Desktop

Give any MCP agent persistent local memory: three tools (`memory_add`,
`memory_search`, `memory_clear`), one SQLite file per namespace, nothing
leaves your machine.

```bash
pip install 'lean-memory[mcp,models]'
```

> First run downloads two open models (~1.4 GB total: Qwen3-Embedding-0.6B
> + Ettin-32M reranker — both ungated). Pre-warm once so your MCP client
> never waits on a download:
>
> ```bash
> python -c "from lean_memory.embed.sentence_transformer import SentenceTransformerEmbedder; \
> from lean_memory.retrieve.rerank import CrossEncoderReranker; \
> SentenceTransformerEmbedder().embed_one('warm'); CrossEncoderReranker().score('warm', ['up'])"
> ```

**Claude Code:**

```bash
claude mcp add lean-memory -- lean-memory-mcp
```

**Claude Desktop** — add to `mcpServers` (or copy `examples/mcp_config.json`):

```json
{ "lean-memory": { "command": "lean-memory-mcp", "env": { "LM_DATA_ROOT": "~/.lean_memory" } } }
```

Data root: `LM_DATA_ROOT` (default `~/.lean_memory`). Works offline-only too —
without the `models` extra the server falls back to deterministic stub backends
(fine for CI, semantically meaningless for real use — install `[models]`).
````

Adjust the size figure to what Task 8 actually measured; verify the warm-up one-liner runs (`.venv` has the models cached) before committing.

- [ ] **Step 2: Replace the top TODO block**

Replace the README TODO block (README.md:5-13) with:

```markdown
> **Status (2026-07):** working toward the first public launch (MCP-first).
> Roadmap and rationale: `docs/superpowers/specs/2026-07-08-strategic-direction-design.md`.
> Public benchmark runs (LongMemEval/LoCoMo) are deferred until after launch;
> the harness is complete (`bench/phase2_*.py`) and the engine flaws it exposed
> are fixed on this branch — see `docs/phase2-learnings.md`.
```

- [ ] **Step 3: Record the demo GIF**

Install vhs if needed: `command -v vhs || brew install vhs`. Create `docs/assets/quickstart.tape`:

```tape
Output docs/assets/quickstart.gif
Set FontSize 15
Set Width 1000
Set Height 550
Set TypingSpeed 40ms
Type "python"
Enter
Sleep 1s
Type "from lean_memory import Memory"
Enter
Type "mem = Memory(root='./demo-data')"
Enter
Sleep 500ms
Type "mem.add('me', 'I work at Acme Corp.')"
Enter
Sleep 1s
Type "mem.add('me', 'I moved to Globex last week.')  # supersedes Acme"
Enter
Sleep 1s
Type "mem.search('me', 'where does the user work?')[0].fact.fact_text"
Enter
Sleep 2s
Type "exit()"
Enter
Sleep 1s
```

Run: `vhs docs/assets/quickstart.tape`
Expected: `docs/assets/quickstart.gif` created; the final frame shows the Globex fact returned. Embed it in the README directly under the opening code block: `![lean-memory quickstart](docs/assets/quickstart.gif)`. Delete the `./demo-data` directory afterwards.

- [ ] **Step 4: Fresh-eyes timing check**

Read the new MCP section top to bottom and verify a stranger on a warm model cache goes install → config → first recall in ≤ 2 minutes of commands. Trim anything that isn't copy-paste.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/assets/quickstart.tape docs/assets/quickstart.gif examples/mcp_config.json
git commit -m "docs: MCP-first quickstart — canonical install, warm-up, client configs, demo GIF"
```

---

### Task 10: Realign the status docs with the strategy

CLAUDE.md still says "Phase 2 suspended, fix engine then re-run benchmarks" — superseded by the approved spec (benchmarks deferred past launch). ARCHITECTURE.md and the handoff need matching status updates so no doc contradicts another.

**Files:**
- Modify: `CLAUDE.md` (the "START HERE" section)
- Modify: `ARCHITECTURE.md` (Phase 2 table + a calibration row in Measured Performance)
- Modify: `docs/superpowers/phase2-HANDOFF.md` (status header addendum)

**Interfaces:**
- Consumes: calibration numbers from `bench/results/calibration/README.md` (Tasks 3–6); the spec at `docs/superpowers/specs/2026-07-08-strategic-direction-design.md`.
- Produces: consistent docs for Task 11's whole-branch review.

- [ ] **Step 1: Rewrite the CLAUDE.md "START HERE" section**

Replace the `## ⚠️ START HERE` section (keep the `## Project` section untouched) with a current-state block: strategy = quality-gate-then-MCP-launch per the spec (link it and this plan); engine backlog items 1–3 fixed (cite the calibration README for numbers); benchmarks deferred until the post-launch six-week read; remaining next steps = launch execution per spec §3. Keep the phase2-learnings/handoff pointers as historical context, one line each.

- [ ] **Step 2: Update ARCHITECTURE.md**

- Phase 2 table (`ARCHITECTURE.md:43-50`): change the benchmarks row to `⬜ deferred (post-launch) — harness complete, see docs/superpowers/specs/2026-07-08-strategic-direction-design.md`.
- In "Measured Performance", add a short "Escalation recalibration (2026-07)" subsection: baseline rate → post-fix rate at the frozen operating point, the three gate verdicts from Task 6, and the granularity numbers from Task 5 — mirroring the BET-2 section's style, citing the JSON files.
- "Known Limitations": update the recency bullet (fixed — `Memory.search(now=...)`; wall-clock default unchanged).

- [ ] **Step 3: Add a status addendum to the handoff doc**

Under the existing STATUS CHANGE blockquote in `docs/superpowers/phase2-HANDOFF.md`, add:

```markdown
> **UPDATE (2026-07-08+):** backlog items 1–3 fixed on `launch-gate`
> (endpoint-scoped coref, granularity + escalation re-freeze, search-time `now`).
> Benchmark re-runs are DEFERRED past the MCP launch per
> `docs/superpowers/specs/2026-07-08-strategic-direction-design.md`. Secrets
> rotated. The runbook below remains valid for the eventual re-run.
```

- [ ] **Step 4: Consistency sweep**

Run: `grep -rn "suspended\|97%\|96.7%\|0.5" CLAUDE.md README.md ARCHITECTURE.md | grep -v Binary`
Read each hit; fix any statement the gate work made stale (e.g., threshold values quoted as 0.5, "suspended mid-flight" framing). phase2-learnings.md stays untouched — it is a dated postmortem.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md ARCHITECTURE.md docs/superpowers/phase2-HANDOFF.md
git commit -m "docs: align status docs with launch-gate strategy (benchmarks deferred, backlog fixed)"
```

---

### Task 11: Whole-branch review, then merge and clean up

The final review covers BOTH the never-reviewed-whole Phase 2 work (its commits are already in main's history; the per-task reviews live in `.superpowers/sdd/review-*.diff`) and this plan's `launch-gate` branch.

**Files:**
- No planned source changes (review findings may add some).

**Interfaces:**
- Consumes: the deferred-minors ledger at `.superpowers/sdd/progress.md`; review skill `superpowers:requesting-code-review`; merge flow `superpowers:finishing-a-development-branch`.
- Produces: `launch-gate` merged to `main`, `main` pushed, stale `phase2-eval-harness` branch labels deleted.

- [ ] **Step 1: Determine the review range**

```bash
BASE=$(git log --format='%h %ad' --date=short main | awk '$2 < "2026-07-02"' | head -1 | cut -d' ' -f1)
echo "review range: $BASE..HEAD"
git log --oneline "$BASE..HEAD" | wc -l
```
Expected: `$BASE` is the last pre-Phase-2 commit; the range covers all Phase 2 + launch-gate commits.

- [ ] **Step 2: Run the review**

Invoke `superpowers:requesting-code-review` over `$BASE..HEAD`, providing the reviewer: the spec, this plan, and the note that per-task reviews already ran (ledger + diffs in `.superpowers/sdd/`) — this pass is for cross-task issues, drift, and the deferred minors list.

- [ ] **Step 3: Address findings**

Critical/Major: fix now (TDD — failing test first where applicable), commit per fix. Minor: fix if ≤ 15 minutes each, otherwise append to the deferred ledger in `.superpowers/sdd/progress.md` with a one-line rationale.

- [ ] **Step 4: Verify everything green one last time**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all PASS. Also re-run the Task 8 Step 5 smoke if any `src/lean_memory/` file changed during review fixes.

- [ ] **Step 5: Merge and clean up (finishing-a-development-branch flow)**

Invoke `superpowers:finishing-a-development-branch` for `launch-gate`. Expected end state:

```bash
git checkout main && git merge --no-ff launch-gate -m "merge: launch quality gate (spec 2026-07-08)"
git branch -d launch-gate
git branch -d phase2-eval-harness            # identical to a main ancestor — label only
git push origin main
git push origin --delete phase2-eval-harness
```
(The exact merge-vs-PR choice follows the skill's prompt to the human partner.)

- [ ] **Step 6: Confirm the gate is closed**

Check every spec §2 item against reality: (1) canonical install smoke passed (Task 8 Step 5), (2) escalation <20% frozen + gates PASS (Task 6), (3) granularity targets met (Task 5), (4) recency test green (Task 7), (5) secrets rotated + branch merged (Tasks 1, 11), (6) quickstart + GIF live (Task 9). Record the checklist with evidence links in the task report. The launch itself (spec §3) is deliberately NOT part of this plan.
