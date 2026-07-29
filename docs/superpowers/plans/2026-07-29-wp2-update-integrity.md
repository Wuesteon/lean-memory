# WP2 Update-Integrity Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reproducible scenario suite proving that when a fact changes, lean-memory returns the current truth, retires the old fact (`is_latest=0` + `superseded_by`), and keeps it queryable via `as_of` — emitted as a markdown results table and pinned as offline regression tests.

**Architecture:** One scenario engine in `bench/update_integrity.py` (dataclass scenarios → `run_scenario()` → per-assertion results) drives BOTH the bench CLI (markdown table) and `tests/test_update_integrity_scenarios.py` (pytest parametrized over the same scenarios via the existing `sys.path.insert` bench-import pattern from `tests/test_phase2_ingest.py`). Everything goes through the public API only: `Memory.add()` / `Memory.search()` (including `is_latest_only=False` and `as_of=` for retirement/history checks) — no store internals.

**Tech Stack:** Python stdlib + pytest. No new dependencies. Offline by default (stub backends); the same scenarios must also pass with `[models]` installed.

## Global Constraints

- Offline suite green at every commit: `PYTHONPATH=src /Users/wuesteon/research/lean-memory/.venv/bin/python -m pytest tests/ -q` — no test may require network or model downloads. (The worktree needs `PYTHONPATH=src`; the venv's editable install points at the main checkout.)
- ADD-only discipline: scenarios only observe; nothing in this packet mutates or deletes history.
- Zero overlap with lanes A/C: only `bench/update_integrity.py`, `tests/test_update_integrity_scenarios.py`, `docs/competitive-landscape.md` (appendix), `CHANGELOG.md`, and the sdist exclude list in `pyproject.toml` may change. **No `src/` changes.**
- No benchmark claim beyond reproduced, versioned behavior; the optional mem0 arm must pin its exact version in the output.
- Commit messages follow the repo's emoji-conventional format (e.g. `✨ feat:`, `✅ test:`, `📝 docs:`); never add a Claude signature.

**Scenario-text rule (load-bearing):** the offline extractor only emits facts for sentences matching its relation-verb lexicon — `works_at` (work/working at|for), `lives_in` (live/living/based in), `likes` (like/love/enjoy/prefer), `dislikes` (dislike/hate), `is_a` (is/am/are + a/an), `has` (has/have/own), `uses` (use/using). Functional (single-value) slots: `works_at`, `lives_in`, `is_a`. Multi-valued: `likes`, `dislikes`, `uses`, `has`, etc. Every scenario text below was chosen to hit this lexicon — do not reword them.

**Determinism rationale (why outcomes are embedder-independent):** on a functional slot, a distinct new object supersedes in EVERY resolver band (high → refinement supersede; low → replacement; ambiguous → safe-default supersede), so the assertions hold under FakeEmbedder and real `[models]` embedders alike. EXTENDS outcomes are forced by the additive-cue regex ("also") or multi-valued predicates — also embedder-independent.

---

### Task 1: Scenario engine + first scenario (employer change)

**Files:**
- Create: `bench/update_integrity.py`
- Test: `tests/test_update_integrity_scenarios.py`

**Interfaces:**
- Produces (later tasks rely on these exact names):
  - `Step(text: str, t: int)` — one `mem.add(ns, text, t_ref=t)` call.
  - `Scenario(key, title, steps, query, expect_top1_contains, expect_retired_contains=None, as_of=None, expect_as_of_top1_contains=None, expect_all_latest_contain=None, reopen=False)` — frozen dataclass; `expect_all_latest_contain: tuple[str, ...] | None` asserts the exact set of latest facts matching the query (count must equal len of tuple, each substring found once); `reopen=True` closes and reopens the Memory before querying.
  - `SCENARIOS: list[Scenario]`
  - `AssertionResult(name: str, ok: bool, detail: str)`
  - `run_scenario(scenario: Scenario, root: Path) -> list[AssertionResult]` — builds `Memory(root=root)` (default offline backends), plays steps, runs every non-None expectation, closes the Memory.
- Consumes: `lean_memory.Memory` public API only.

- [ ] **Step 1: Write the failing test**

Create `tests/test_update_integrity_scenarios.py`:

```python
"""WP2 — update-integrity scenarios as offline regression tests.

Supersession is resolver logic and must hold with the deterministic stub
backends; the same scenarios also back `bench/update_integrity.py`'s
markdown table. Bench import follows the test_phase2_* precedent (bench/ is
not part of the installed package)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from update_integrity import SCENARIOS, run_scenario  # noqa: E402


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.key)
def test_scenario(scenario, tmp_path):
    results = run_scenario(scenario, tmp_path)
    failures = [r for r in results if not r.ok]
    assert not failures, "\n".join(f"{r.name}: {r.detail}" for r in failures)


def test_scenario_keys_are_unique():
    keys = [s.key for s in SCENARIOS]
    assert len(keys) == len(set(keys))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src /Users/wuesteon/research/lean-memory/.venv/bin/python -m pytest tests/test_update_integrity_scenarios.py -q`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'update_integrity'`

- [ ] **Step 3: Write the engine with the first scenario**

Create `bench/update_integrity.py`:

```python
"""WP2 — update-integrity scenario suite (the supersession head-to-head).

Small, honest, reproducible: when a fact changes, does the engine return the
current truth, retire the old fact, and keep it queryable via `as_of`?
Public API only (`Memory.add` / `Memory.search`); no LLM judge, no frozen
backbone. `python bench/update_integrity.py --markdown` renders the table.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lean_memory import Memory  # noqa: E402

HOUR = 3_600_000
T0 = 1_000_000_000  # fixed epoch-ms base so runs are byte-reproducible


@dataclass(frozen=True)
class Step:
    text: str
    t: int


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    steps: tuple[Step, ...]
    query: str
    expect_top1_contains: str
    expect_retired_contains: str | None = None
    as_of: int | None = None
    expect_as_of_top1_contains: str | None = None
    expect_all_latest_contain: tuple[str, ...] | None = None
    reopen: bool = False


@dataclass
class AssertionResult:
    name: str
    ok: bool
    detail: str = ""


def _texts(hits) -> list[str]:
    return [h.fact.fact_text for h in hits]


def run_scenario(scenario: Scenario, root: Path) -> list[AssertionResult]:
    ns = scenario.key
    mem = Memory(root=root)
    try:
        for step in scenario.steps:
            mem.add(ns, step.text, t_ref=step.t)
        if scenario.reopen:
            mem.close()
            mem = Memory(root=root)

        out: list[AssertionResult] = []
        latest = mem.search(ns, scenario.query, k=10, now=scenario.steps[-1].t)

        top1 = latest[0].fact.fact_text if latest else "<no results>"
        out.append(AssertionResult(
            "top1-is-current",
            scenario.expect_top1_contains in top1,
            f"expected {scenario.expect_top1_contains!r} in top-1, got {top1!r}",
        ))

        if scenario.expect_retired_contains is not None:
            everything = mem.search(
                ns, scenario.query, k=20, is_latest_only=False,
                now=scenario.steps[-1].t,
            )
            match = next(
                (h.fact for h in everything
                 if scenario.expect_retired_contains in h.fact.fact_text
                 and h.fact.fact_text != top1),
                None,
            )
            ok = (match is not None and not match.is_latest
                  and match.superseded_by is not None)
            out.append(AssertionResult(
                "old-fact-retired",
                ok,
                "retired fact not found" if match is None else
                f"is_latest={match.is_latest} superseded_by={match.superseded_by}",
            ))

        if scenario.as_of is not None:
            # Point-in-time semantics (per test_spine.py): as_of applies the
            # world-time interval predicate, but the caller must open the
            # latest-only filter or superseded facts stay invisible.
            hist = mem.search(ns, scenario.query, k=10, as_of=scenario.as_of,
                              is_latest_only=False, now=scenario.as_of)
            h1 = hist[0].fact.fact_text if hist else "<no results>"
            out.append(AssertionResult(
                "as-of-returns-old-truth",
                scenario.expect_as_of_top1_contains in h1,
                f"expected {scenario.expect_as_of_top1_contains!r} in as-of top-1, got {h1!r}",
            ))

        if scenario.expect_all_latest_contain is not None:
            texts = _texts(latest)
            missing = [s for s in scenario.expect_all_latest_contain
                       if not any(s in t for t in texts)]
            ok = not missing and len(texts) == len(scenario.expect_all_latest_contain)
            out.append(AssertionResult(
                "latest-set-exact",
                ok,
                f"latest={texts!r} expected-substrings={scenario.expect_all_latest_contain!r}",
            ))
        return out
    finally:
        mem.close()


SCENARIOS: list[Scenario] = [
    Scenario(
        key="employer_change",
        title="Employer change (functional slot supersedes)",
        steps=(Step("I work at Acme.", T0), Step("I work at Zorbex now.", T0 + 2 * HOUR)),
        query="where does the user work?",
        expect_top1_contains="Zorbex",
        expect_retired_contains="Acme",
        as_of=T0 + HOUR,
        expect_as_of_top1_contains="Acme",
    ),
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src /Users/wuesteon/research/lean-memory/.venv/bin/python -m pytest tests/test_update_integrity_scenarios.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add bench/update_integrity.py tests/test_update_integrity_scenarios.py
git commit -m "✨ feat: WP2 update-integrity scenario engine + employer-change scenario"
```

---

### Task 2: The remaining nine scenarios

**Files:**
- Modify: `bench/update_integrity.py` (append to `SCENARIOS`)

**Interfaces:**
- Consumes: `Scenario`, `Step`, `T0`, `HOUR` exactly as defined in Task 1.
- Produces: `SCENARIOS` with ten total entries; keys listed below are FROZEN (the docs appendix in Task 4 references them).

- [ ] **Step 1: Append the nine scenarios (test already exists — parametrization picks them up)**

Append to `SCENARIOS` in `bench/update_integrity.py`:

```python
    Scenario(
        key="name_identity_change",
        title="Identity change — mem0#4896's class of case, on the is_a slot",
        steps=(Step("I am an engineer.", T0), Step("I am a designer now.", T0 + 2 * HOUR)),
        query="what is the user?",
        expect_top1_contains="designer",
        expect_retired_contains="engineer",
        as_of=T0 + HOUR,
        expect_as_of_top1_contains="engineer",
    ),
    Scenario(
        key="city_move",
        title="City move (functional slot supersedes)",
        steps=(Step("I live in Berlin.", T0), Step("I live in Munich now.", T0 + 2 * HOUR)),
        query="where does the user live?",
        expect_top1_contains="Munich",
        expect_retired_contains="Berlin",
        as_of=T0 + HOUR,
        expect_as_of_top1_contains="Berlin",
    ),
    Scenario(
        key="preference_flip",
        title="Preference flip on a functional identity slot",
        steps=(Step("I am a vim user.", T0), Step("I am an emacs user now.", T0 + 2 * HOUR)),
        query="which editor does the user prefer?",
        expect_top1_contains="emacs",
        expect_retired_contains="vim",
        as_of=T0 + HOUR,
        expect_as_of_top1_contains="vim",
    ),
    Scenario(
        key="additive_extends",
        title="Additive 'also' must EXTEND, not supersede",
        steps=(Step("I work at Acme.", T0), Step("I also work at Globex.", T0 + 2 * HOUR)),
        query="where does the user work?",
        expect_top1_contains="work",
        expect_all_latest_contain=("Acme", "Globex"),
    ),
    Scenario(
        key="replacement_after_additive",
        title="Replacement retires ALL co-valid facts on a functional slot",
        steps=(
            Step("I work at Acme.", T0),
            Step("I also work at Globex.", T0 + 2 * HOUR),
            Step("I work at Zorbex now.", T0 + 4 * HOUR),
        ),
        query="where does the user work?",
        expect_top1_contains="Zorbex",
        expect_retired_contains="Acme",
        expect_all_latest_contain=("Zorbex",),
    ),
    Scenario(
        key="multivalued_preserved",
        title="Multi-valued slot keeps co-valid values (no false supersede)",
        steps=(Step("I like jazz.", T0), Step("I also like blues.", T0 + 2 * HOUR)),
        query="what music does the user like?",
        expect_top1_contains="like",
        expect_all_latest_contain=("jazz", "blues"),
    ),
    Scenario(
        key="as_of_before_everything",
        title="as_of earlier than all facts returns nothing (no time travel forward)",
        steps=(Step("I live in Berlin.", T0),),
        query="where does the user live?",
        expect_top1_contains="Berlin",
        as_of=T0 - HOUR,
        expect_as_of_top1_contains="<no results>",
    ),
    Scenario(
        key="restart_persistence",
        title="Close + reopen: current truth and history survive restart",
        steps=(Step("I work at Acme.", T0), Step("I work at Zorbex now.", T0 + 2 * HOUR)),
        query="where does the user work?",
        expect_top1_contains="Zorbex",
        expect_retired_contains="Acme",
        as_of=T0 + HOUR,
        expect_as_of_top1_contains="Acme",
        reopen=True,
    ),
    Scenario(
        key="restatement_no_duplicate",
        title="Verbatim restatement does not duplicate the fact (WP11)",
        steps=(Step("I live in Berlin.", T0), Step("I live in Berlin.", T0 + 2 * HOUR)),
        query="where does the user live?",
        expect_top1_contains="Berlin",
        expect_all_latest_contain=("Berlin",),
    ),
]
```

- [ ] **Step 2: Run the scenario tests**

Run: `PYTHONPATH=src /Users/wuesteon/research/lean-memory/.venv/bin/python -m pytest tests/test_update_integrity_scenarios.py -q`
Expected: 11 passed (10 scenarios + key-uniqueness). If a scenario fails, the fix is the scenario TEXT or expectation (respect the lexicon rule) — never a `src/` change; a genuine engine bug found here is reported, not patched in this packet.

- [ ] **Step 3: Run the full offline suite**

Run: `PYTHONPATH=src /Users/wuesteon/research/lean-memory/.venv/bin/python -m pytest tests/ -q`
Expected: all pass (289 + the new ones), no new warnings.

- [ ] **Step 4: Commit**

```bash
git add bench/update_integrity.py
git commit -m "✅ test: complete the ten WP2 update-integrity scenarios"
```

---

### Task 3: Markdown report CLI

**Files:**
- Modify: `bench/update_integrity.py` (append `main()` + `__main__` guard)

**Interfaces:**
- Consumes: `SCENARIOS`, `run_scenario`, `AssertionResult` from Tasks 1–2.
- Produces: `python bench/update_integrity.py --markdown` → markdown table on stdout, exit 0 iff every assertion passed; runs in a `tempfile.TemporaryDirectory()` per scenario.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_update_integrity_scenarios.py`:

```python
def test_markdown_report_renders_and_passes(tmp_path, capsys):
    from update_integrity import main

    rc = main(["--markdown", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "| Scenario |" in out
    assert "employer_change" in out and "restatement_no_duplicate" in out
    assert "FAIL" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src /Users/wuesteon/research/lean-memory/.venv/bin/python -m pytest tests/test_update_integrity_scenarios.py::test_markdown_report_renders_and_passes -q`
Expected: FAIL with `ImportError: cannot import name 'main'`

- [ ] **Step 3: Implement `main()`**

Append to `bench/update_integrity.py`:

```python
def main(argv: list[str] | None = None) -> int:
    import argparse
    import platform
    import tempfile

    from lean_memory import __version__

    ap = argparse.ArgumentParser(description="WP2 update-integrity scenario suite")
    ap.add_argument("--markdown", action="store_true", help="emit a markdown results table")
    ap.add_argument("--root", default=None,
                    help="directory for scenario stores (default: a temp dir per scenario)")
    args = ap.parse_args(argv)

    rows: list[tuple[str, list[AssertionResult]]] = []
    for sc in SCENARIOS:
        if args.root:
            root = Path(args.root) / sc.key
            root.mkdir(parents=True, exist_ok=True)
            rows.append((sc.key, run_scenario(sc, root)))
        else:
            with tempfile.TemporaryDirectory() as td:
                rows.append((sc.key, run_scenario(sc, Path(td))))

    all_ok = all(r.ok for _, results in rows for r in results)
    if args.markdown:
        print(f"# Update-integrity results — lean-memory {__version__} "
              f"(offline stub backends, Python {platform.python_version()})\n")
        print("| Scenario | Assertion | Result | Detail |")
        print("|---|---|---|---|")
        for key, results in rows:
            for r in results:
                status = "PASS" if r.ok else "FAIL"
                detail = "" if r.ok else r.detail.replace("|", "\\|")
                print(f"| {key} | {r.name} | {status} | {detail} |")
        print(f"\n**{'ALL PASS' if all_ok else 'FAILURES PRESENT'}** — "
              f"{sum(r.ok for _, res in rows for r in res)}/"
              f"{sum(len(res) for _, res in rows)} assertions.")
    else:
        for key, results in rows:
            for r in results:
                print(f"{key:32s} {r.name:28s} {'PASS' if r.ok else 'FAIL  ' + r.detail}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test, then the CLI end-to-end**

Run: `PYTHONPATH=src /Users/wuesteon/research/lean-memory/.venv/bin/python -m pytest tests/test_update_integrity_scenarios.py -q`
Expected: 12 passed
Run: `PYTHONPATH=src /Users/wuesteon/research/lean-memory/.venv/bin/python bench/update_integrity.py --markdown`
Expected: table renders, ends with **ALL PASS**, exit 0.

- [ ] **Step 5: Commit**

```bash
git add bench/update_integrity.py tests/test_update_integrity_scenarios.py
git commit -m "✨ feat: markdown report CLI for the update-integrity suite"
```

---

### Task 4: Packaging exclude + docs appendix + changelog

**Files:**
- Modify: `pyproject.toml` (sdist `exclude` list)
- Modify: `docs/competitive-landscape.md` (append appendix section)
- Modify: `CHANGELOG.md` (Unreleased)

**Interfaces:**
- Consumes: scenario keys and the verification commands exactly as frozen in Tasks 1–3.

- [ ] **Step 1: Exclude the bench-importing test from the sdist**

In `pyproject.toml`, the `[tool.hatch.build.targets.sdist]` `exclude` list already carries this comment and the phase2 pattern; add one line beneath `"/tests/test_escalation_probe.py",`:

```toml
    "/tests/test_update_integrity_scenarios.py",
```

(Same reason as the existing excludes: it imports `bench/`, which is not shipped.)

- [ ] **Step 2: Append the results appendix**

Append to `docs/competitive-landscape.md`:

```markdown
## Appendix: update-integrity results (WP2)

*When a fact changes, does the engine return the current truth and keep the
old one queryable?* Ten scripted scenarios through the public API only
(`Memory.add` → `Memory.search`), asserting per scenario: top-1 is the new
fact; the superseded fact has `is_latest=False` and `superseded_by` set; and
`as_of=<t before the update>` returns the old fact. Offline deterministic
backends by default; the identical scenarios run as regression tests in CI
(`tests/test_update_integrity_scenarios.py`).

Reproduce:

```bash
.venv/bin/python bench/update_integrity.py --markdown
```

Scenario keys: `employer_change`, `name_identity_change`, `city_move`,
`preference_flip`, `additive_extends`, `replacement_after_additive`,
`multivalued_preserved`, `as_of_before_everything`, `restart_persistence`,
`restatement_no_duplicate`.

Paste the emitted table below when refreshing this appendix (include the
lean-memory version header the tool prints):

<!-- update-integrity results table goes here -->
```

Then run `PYTHONPATH=src /Users/wuesteon/research/lean-memory/.venv/bin/python bench/update_integrity.py --markdown` and paste its full output over the placeholder comment.

- [ ] **Step 3: Changelog entry**

Under `## [Unreleased]` in `CHANGELOG.md` (create the section atop `## [0.2.2]` if absent), add:

```markdown
### Added

- **Update-integrity benchmark (WP2)** — `bench/update_integrity.py`: ten
  scripted supersession scenarios through the public API (current-truth
  top-1, retirement flags, `as_of` readback, restart persistence), rendered
  as a markdown table and pinned offline as
  `tests/test_update_integrity_scenarios.py`. Results appendix in
  `docs/competitive-landscape.md`.
```

- [ ] **Step 4: Run the full offline suite**

Run: `PYTHONPATH=src /Users/wuesteon/research/lean-memory/.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml docs/competitive-landscape.md CHANGELOG.md
git commit -m "📝 docs: WP2 results appendix, changelog, sdist exclude"
```

---

### Task 5 (OPTIONAL — separate user go-ahead): mem0 comparison arm

Per the packet this arm is opt-in: it needs `mem0ai` installed plus their LLM
path (Ollama or an API key), and publishes nothing beyond reproduced,
versioned behavior. **Do not implement in the same session unless asked**;
recorded here so the flag surface is designed up front.

- CLI shape: `--arm mem0` on `main()`; default arm stays `lean-memory`.
- Adapter contract: a `Mem0Arm` class mapping `Step.text` → `mem0.Memory.add`
  and `Scenario.query` → their search; assertion names stay identical so the
  two tables are side-by-side comparable.
- If `import mem0` fails: exit 2 with the install hint
  `pip install mem0ai` — never a silent skip.
- The emitted header MUST pin `mem0.__version__` and the configured LLM
  backend.

---

## Verification (packet acceptance criteria)

- `PYTHONPATH=src .venv/bin/python bench/update_integrity.py --markdown` renders the table from a single command, ALL PASS, exit 0.
- `PYTHONPATH=src .venv/bin/python -m pytest tests/test_update_integrity_scenarios.py -q` green offline.
- With `[models]` installed (if available locally): rerun both — scenarios are designed to be embedder-independent (see Determinism rationale).
- Full offline suite green; no `src/` diffs in `git diff --stat main`.
