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


def test_markdown_report_renders_and_passes(tmp_path, capsys):
    from update_integrity import main

    rc = main(["--markdown", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "| Scenario |" in out
    assert "employer_change" in out and "restatement_no_duplicate" in out
    assert "FAIL" not in out
