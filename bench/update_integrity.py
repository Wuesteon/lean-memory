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

        if scenario.as_of is not None and scenario.expect_as_of_top1_contains is not None:
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
