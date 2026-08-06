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


def test_lean_memory_preserves_source_casing_verbatim(tmp_path):
    """Pin exact casing, which the shared `_contains` matcher deliberately ignores.

    `_contains` is case-insensitive so that mem0's third-person rewrites are
    not penalised for cosmetics. That relaxation must not silently cost the
    lean-memory arm its casing guarantee: lean-memory echoes the source
    sentence verbatim, so assert that case-sensitively here.
    """
    from lean_memory import Memory

    scenario = next(s for s in SCENARIOS if s.key == "employer_change")
    mem = Memory(root=tmp_path)
    try:
        for step in scenario.steps:
            mem.add(scenario.key, step.text, t_ref=step.t)
        hits = mem.search(scenario.key, scenario.query, k=10,
                          now=scenario.steps[-1].t)
        top1 = hits[0].fact.fact_text
    finally:
        mem.close()

    assert "Zorbex" in top1, f"expected verbatim casing, got {top1!r}"
    assert "zorbex" not in top1, f"stored text was down-cased: {top1!r}"


def test_markdown_report_renders_and_passes(tmp_path, capsys):
    from update_integrity import main

    rc = main(["--markdown", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "| Scenario |" in out
    assert "employer_change" in out and "restatement_no_duplicate" in out
    assert "FAIL" not in out
