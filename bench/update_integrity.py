"""WP2 — update-integrity scenario suite (the supersession head-to-head).

Small, honest, reproducible: when a fact changes, does the engine return the
current truth, retire the old fact, and keep it queryable via `as_of`?
Public API only (`Memory.add` / `Memory.search`); no LLM judge, no frozen
backbone. `python bench/update_integrity.py --markdown` renders the table.

`--arm mem0` runs the identical scenarios against mem0's public API instead
(opt-in, needs `pip install mem0ai` plus a configured LLM backend); the two
tables use the same assertion names so they compare cell-for-cell.
"""

from __future__ import annotations

import signal
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
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
    # False only when the arm under test has no equivalent of the concept the
    # assertion checks (see Mem0Arm). Such rows render `n/a (unsupported)` and
    # are excluded from the PASS tally — never silently dropped.
    supported: bool = True


def _texts(hits) -> list[str]:
    return [h.fact.fact_text for h in hits]


def _contains(needle: str, haystack: str) -> bool:
    """Case-insensitive substring match — identical semantics in every arm.

    Engines differ in how they render a stored fact (lean-memory keeps the
    source sentence, mem0 canonicalises to third person, e.g. "Works at
    Acme"). Casing is cosmetic, so it must not decide a comparison; the
    matcher is deliberately the same for both arms rather than lenient for
    one. Every needle and haystack in `SCENARIOS` is already lower-case, so
    the default arm's results are unchanged by this.
    """
    return needle.casefold() in haystack.casefold()


def assertion_names(scenario: Scenario) -> list[str]:
    """The assertion rows a scenario produces, in order — shared by all arms.

    This is the contract that makes two arms' tables comparable cell-for-cell.
    """
    names = ["top1-is-current"]
    if scenario.expect_retired_contains is not None:
        names.append("old-fact-retired")
    if scenario.as_of is not None and scenario.expect_as_of_top1_contains is not None:
        names.append("as-of-returns-old-truth")
    if scenario.expect_all_latest_contain is not None:
        names.append("latest-set-exact")
    return names


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
            _contains(scenario.expect_top1_contains, top1),
            f"expected {scenario.expect_top1_contains!r} in top-1, got {top1!r}",
        ))

        if scenario.expect_retired_contains is not None:
            everything = mem.search(
                ns, scenario.query, k=20, is_latest_only=False,
                now=scenario.steps[-1].t,
            )
            match = next(
                (h.fact for h in everything
                 if _contains(scenario.expect_retired_contains, h.fact.fact_text)
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
                _contains(scenario.expect_as_of_top1_contains, h1),
                f"expected {scenario.expect_as_of_top1_contains!r} in as-of top-1, got {h1!r}",
            ))

        if scenario.expect_all_latest_contain is not None:
            texts = _texts(latest)
            missing = [s for s in scenario.expect_all_latest_contain
                       if not any(_contains(s, t) for t in texts)]
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


# ---------------------------------------------------------------------------
# mem0 comparison arm (WP2 plan, Task 5) — opt-in via `--arm mem0`.
#
# Fairness rule: the adapter maps each scenario onto mem0's own public API as
# faithfully as that API allows, and runs the SAME assertions under the SAME
# names and in the same order as the lean-memory arm, so the two tables compare
# cell-for-cell. Nothing is relaxed for one arm or tightened for the other.
# Where a mem0 concept genuinely does not exist (point-in-time reads), the
# assertion still runs: the adapter probes the installed library, records the
# library's own refusal as the Detail, and the row renders
# `n/a (unsupported)` — never a silent skip.
# ---------------------------------------------------------------------------

MEM0_INSTALL_HINT = "mem0 is not installed — install it with: pip install mem0ai"


def _import_mem0():
    """Import the mem0 package. Raises ImportError when it is absent."""
    import importlib

    return importlib.import_module("mem0")


@contextmanager
def _time_budget(seconds: int | None):
    """Wall-clock budget for one scenario (POSIX main thread; falsy disables).

    A stalled scenario becomes a FAIL row with the elapsed budget in its
    Detail instead of killing the whole run.
    """
    if not seconds or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _fire(signum, frame):  # pragma: no cover - timing dependent
        raise TimeoutError(f"scenario exceeded the {seconds}s budget")

    try:
        previous = signal.signal(signal.SIGALRM, _fire)
    except ValueError:  # not the main thread
        yield
        return
    signal.alarm(int(seconds))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@dataclass(frozen=True)
class Mem0Config:
    """Exactly what the mem0 arm was configured with — pinned in the header."""

    llm_provider: str = "ollama"
    llm_model: str = "qwen2.5:3b"
    embedder_provider: str = "ollama"
    embedder_model: str = "nomic-embed-text"
    embedding_dims: int = 768
    ollama_base_url: str = "http://localhost:11434"
    vector_store: str = "qdrant"

    def label(self) -> str:
        return (
            f"llm={self.llm_provider}/{self.llm_model}, "
            f"embedder={self.embedder_provider}/{self.embedder_model} "
            f"({self.embedding_dims}d), "
            f"vector_store={self.vector_store} (local, on-disk), "
            f"ollama_base_url={self.ollama_base_url}"
        )


class Mem0Arm:
    """Runs `SCENARIOS` against mem0's public API.

    Mapping: `Step.text` → `mem0.Memory.add`, `Scenario.query` → mem0 search,
    one mem0 session id (`user_id`) per scenario, one on-disk store per
    scenario. Retirement is read from the live set plus `Memory.history()`,
    which is mem0's own record of an UPDATE/DELETE.
    """

    def __init__(self, mem0_module, config: Mem0Config | None = None,
                 timeout: int | None = 600, progress=None):
        self._mem0 = mem0_module
        self.config = config or Mem0Config()
        self.timeout = timeout
        self._progress = progress
        self._timestamp_supported: bool | None = None
        self._timestamp_error: str | None = None
        self._reference_date_supported: bool | None = None
        self._reference_date_error: str | None = None

    # -- identity -----------------------------------------------------------

    @property
    def version(self) -> str:
        return str(getattr(self._mem0, "__version__", "unknown"))

    def header(self, python_version: str) -> str:
        return (
            f"# Update-integrity results — mem0 {self.version} "
            f"({self.config.label()}, Python {python_version})\n\n"
            "*Same scenarios and same assertion names as the lean-memory arm. "
            "`n/a (unsupported)` marks an assertion with no equivalent in this "
            "library's public API (probed at runtime — the library's own refusal "
            "is quoted in Detail); those rows are excluded from the PASS tally.*"
        )

    # -- plumbing -----------------------------------------------------------

    def _note(self, message: str) -> None:
        if self._progress is not None:
            print(message, file=self._progress, flush=True)

    def memory_config(self, root: Path, ns: str) -> dict:
        llm_cfg: dict = {"model": self.config.llm_model, "temperature": 0.0}
        emb_cfg: dict = {"model": self.config.embedder_model,
                         "embedding_dims": self.config.embedding_dims}
        if self.config.llm_provider == "ollama":
            llm_cfg["ollama_base_url"] = self.config.ollama_base_url
        if self.config.embedder_provider == "ollama":
            emb_cfg["ollama_base_url"] = self.config.ollama_base_url
        return {
            "llm": {"provider": self.config.llm_provider, "config": llm_cfg},
            "embedder": {"provider": self.config.embedder_provider, "config": emb_cfg},
            "vector_store": {
                "provider": self.config.vector_store,
                "config": {
                    "collection_name": f"wp2_{ns}",
                    "path": str(root / "qdrant"),
                    "embedding_model_dims": self.config.embedding_dims,
                    "on_disk": True,
                },
            },
            "history_db_path": str(root / "mem0-history.db"),
        }

    def _open(self, root: Path, ns: str):
        return self._mem0.Memory.from_config(self.memory_config(root, ns))

    @staticmethod
    def _close(client) -> None:
        """Best-effort teardown so a reopen can take the on-disk store's lock."""
        targets = [getattr(getattr(client, "vector_store", None), "client", None),
                   getattr(client, "db", None)]
        for target in targets:
            close = getattr(target, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - teardown is advisory
                    pass

    @staticmethod
    def _items(response) -> list[dict]:
        if isinstance(response, dict):
            response = response.get("results", [])
        if not isinstance(response, (list, tuple)):
            return []
        return [item for item in response if isinstance(item, dict)]

    @staticmethod
    def _text(item: dict) -> str:
        for key in ("memory", "text", "data"):
            value = item.get(key)
            if value:
                return str(value)
        return ""

    @staticmethod
    def _iso(epoch_ms: int) -> str:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()

    # -- mem0 calls ---------------------------------------------------------

    def _add(self, client, ns: str, step: Step) -> list[dict]:
        """`Step.text` → mem0 add, carrying `Step.t` when mem0 accepts it."""
        if self._timestamp_supported is not False:
            try:
                items = self._items(
                    client.add(step.text, user_id=ns, timestamp=self._iso(step.t)))
                self._timestamp_supported = True
                return items
            except (ValueError, TypeError) as exc:
                self._timestamp_supported = False
                self._timestamp_error = f"{type(exc).__name__}: {exc}"
        return self._items(client.add(step.text, user_id=ns))

    def _search(self, client, ns: str, query: str, k: int, **extra) -> list[dict]:
        try:
            return self._items(
                client.search(query, filters={"user_id": ns}, top_k=k, **extra))
        except TypeError:  # pre-2.0 mem0 signature
            return self._items(client.search(query, user_id=ns, limit=k, **extra))

    def _get_all(self, client, ns: str, k: int) -> list[dict]:
        try:
            return self._items(client.get_all(filters={"user_id": ns}, top_k=k))
        except TypeError:  # pre-2.0 mem0 signature
            return self._items(client.get_all(user_id=ns, limit=k))

    def _history(self, client, memory_id: str) -> list[dict]:
        try:
            return self._items(client.history(memory_id))
        except Exception:
            return []

    # -- assertions ---------------------------------------------------------

    def _retired_assertion(self, client, ns: str, scenario: Scenario,
                           seen_ids: list[str], top1: str, ingest: str) -> AssertionResult:
        needle = scenario.expect_retired_contains or ""
        live = [self._text(item) for item in self._get_all(client, ns, 50)]
        still_live = [t for t in live if _contains(needle, t) and t != top1]
        if still_live:
            return AssertionResult(
                "old-fact-retired", False,
                f"old value is still a current memory in mem0: {still_live!r}")
        evidence: list[str] = []
        for memory_id in seen_ids:
            for row in self._history(client, memory_id):
                event = str(row.get("event", "")).upper()
                old = str(row.get("old_memory") or row.get("prev_value") or "")
                new = str(row.get("new_memory") or row.get("new_value") or "")
                if event in ("UPDATE", "DELETE") and (
                        _contains(needle, old) or _contains(needle, new)):
                    evidence.append(f"{event} {memory_id}: old={old!r} new={new!r}")
        if evidence:
            return AssertionResult("old-fact-retired", True, "; ".join(evidence))
        return AssertionResult(
            "old-fact-retired", False,
            f"no retirement record for {needle!r}: it is absent from the live set and "
            f"no UPDATE/DELETE row in mem0's history carries it ({ingest})")

    def _reference_date_ok(self, client, ns: str, query: str) -> bool:
        """Probe mem0 for a point-in-time read (`as_of`) once per run."""
        if self._reference_date_supported is None:
            try:
                self._search(client, ns, query, 1,
                             reference_date="1970-01-01T00:00:00+00:00")
                self._reference_date_supported = True
            except Exception as exc:
                self._reference_date_supported = False
                self._reference_date_error = f"{type(exc).__name__}: {exc}"
        return bool(self._reference_date_supported)

    def _as_of_assertion(self, client, ns: str, scenario: Scenario) -> AssertionResult:
        name = "as-of-returns-old-truth"
        if not self._reference_date_ok(client, ns, scenario.query):
            return AssertionResult(
                name, False,
                f"mem0 {self.version} has no point-in-time read: "
                f"search(reference_date=…) → {self._reference_date_error}; "
                f"add(timestamp=…) → {self._timestamp_error or 'accepted'}",
                supported=False)
        hits = self._search(client, ns, scenario.query, 10,
                            reference_date=self._iso(scenario.as_of or 0))
        top1 = self._text(hits[0]) if hits else "<no results>"
        return AssertionResult(
            name, _contains(scenario.expect_as_of_top1_contains or "", top1),
            f"expected {scenario.expect_as_of_top1_contains!r} in as-of top-1, "
            f"got {top1!r}")

    # -- driver -------------------------------------------------------------

    def run_scenario(self, scenario: Scenario, root: Path) -> list[AssertionResult]:
        started = time.monotonic()
        self._note(f"[mem0] {scenario.key}: start")
        try:
            with _time_budget(self.timeout):
                results = self._run(scenario, root)
        except Exception as exc:
            results = self._error_rows(scenario, f"{type(exc).__name__}: {exc}")
        self._note(f"[mem0] {scenario.key}: {time.monotonic() - started:.1f}s "
                   + " ".join(f"{r.name}={'n/a' if not r.supported else r.ok}"
                              for r in results))
        return results

    def _error_rows(self, scenario: Scenario, detail: str) -> list[AssertionResult]:
        """Keep the table aligned when a scenario blows up or times out."""
        rows = []
        for name in assertion_names(scenario):
            unsupported = (name == "as-of-returns-old-truth"
                           and self._reference_date_supported is False)
            rows.append(AssertionResult(
                name, False,
                self._reference_date_error if unsupported else detail,
                supported=not unsupported))
        return rows

    def _run(self, scenario: Scenario, root: Path) -> list[AssertionResult]:
        ns = scenario.key
        client = self._open(root, ns)
        try:
            seen_ids: list[str] = []
            events: list[str] = []
            for step in scenario.steps:
                items = self._add(client, ns, step)
                self._note(f"[mem0]   add {step.text!r} -> "
                           + (", ".join(f"{i.get('event')} {self._text(i)!r}"
                                        for i in items) or "no memory extracted"))
                events.extend(str(i.get("event", "?")).upper() for i in items)
                for item in items:
                    memory_id = item.get("id")
                    if memory_id and str(memory_id) not in seen_ids:
                        seen_ids.append(str(memory_id))
            ingest = (f"mem0's LLM emitted {len(events)} memory event(s) "
                      f"[{', '.join(events) or 'none'}] across "
                      f"{len(scenario.steps)} add() call(s)")
            if scenario.reopen:
                self._close(client)
                client = self._open(root, ns)

            out: list[AssertionResult] = []
            latest = self._search(client, ns, scenario.query, 10)
            texts = [self._text(item) for item in latest]
            top1 = texts[0] if texts else "<no results>"
            out.append(AssertionResult(
                "top1-is-current",
                _contains(scenario.expect_top1_contains, top1),
                f"expected {scenario.expect_top1_contains!r} in top-1, got {top1!r} "
                f"({ingest})",
            ))

            if scenario.expect_retired_contains is not None:
                out.append(
                    self._retired_assertion(client, ns, scenario, seen_ids, top1, ingest))

            if scenario.as_of is not None and scenario.expect_as_of_top1_contains is not None:
                out.append(self._as_of_assertion(client, ns, scenario))

            if scenario.expect_all_latest_contain is not None:
                missing = [s for s in scenario.expect_all_latest_contain
                           if not any(_contains(s, t) for t in texts)]
                ok = not missing and len(texts) == len(scenario.expect_all_latest_contain)
                out.append(AssertionResult(
                    "latest-set-exact",
                    ok,
                    f"latest={texts!r} "
                    f"expected-substrings={scenario.expect_all_latest_contain!r} "
                    f"({ingest})",
                ))
            return out
        finally:
            self._close(client)


def emit(rows: list[tuple[str, list[AssertionResult]]], header: str,
         markdown: bool) -> bool:
    """Render the results table; returns True when every graded assertion passed."""
    graded = [r for _, results in rows for r in results if r.supported]
    unsupported = [r for _, results in rows for r in results if not r.supported]
    all_ok = all(r.ok for r in graded)
    if markdown:
        print(header + "\n")
        print("| Scenario | Assertion | Result | Detail |")
        print("|---|---|---|---|")
        for key, results in rows:
            for r in results:
                if not r.supported:
                    status = "n/a (unsupported)"
                else:
                    status = "PASS" if r.ok else "FAIL"
                detail = "" if (r.ok and r.supported) else r.detail.replace("|", "\\|")
                print(f"| {key} | {r.name} | {status} | {detail} |")
        summary = (f"\n**{'ALL PASS' if all_ok else 'FAILURES PRESENT'}** — "
                   f"{sum(r.ok for r in graded)}/{len(graded)} assertions.")
        if unsupported:
            summary += (f" {len(unsupported)} further assertion(s) rendered "
                        f"`n/a (unsupported)` — no equivalent in this arm's API, "
                        f"excluded from the tally.")
        print(summary)
    else:
        for key, results in rows:
            for r in results:
                if not r.supported:
                    print(f"{key:32s} {r.name:28s} n/a   {r.detail}")
                else:
                    print(f"{key:32s} {r.name:28s} {'PASS' if r.ok else 'FAIL  ' + r.detail}")
    return all_ok


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import platform
    import tempfile

    from lean_memory import __version__

    ap = argparse.ArgumentParser(description="WP2 update-integrity scenario suite")
    ap.add_argument("--markdown", action="store_true", help="emit a markdown results table")
    ap.add_argument("--root", default=None,
                    help="directory for scenario stores (default: a temp dir per scenario)")
    ap.add_argument("--arm", choices=("lean-memory", "mem0"), default="lean-memory",
                    help="engine under test (default: lean-memory)")
    ap.add_argument("--mem0-llm-provider", default=Mem0Config.llm_provider)
    ap.add_argument("--mem0-llm-model", default=Mem0Config.llm_model)
    ap.add_argument("--mem0-embedder-provider", default=Mem0Config.embedder_provider)
    ap.add_argument("--mem0-embedder-model", default=Mem0Config.embedder_model)
    ap.add_argument("--mem0-embedding-dims", type=int, default=Mem0Config.embedding_dims)
    ap.add_argument("--mem0-ollama-base-url", default=Mem0Config.ollama_base_url)
    ap.add_argument("--mem0-timeout", type=int, default=600,
                    help="mem0 arm: per-scenario wall-clock budget in seconds (0 disables)")
    args = ap.parse_args(argv)

    if args.arm == "mem0":
        # mem0 OSS ships telemetry on by default; the benchmark never phones home.
        os.environ.setdefault("MEM0_TELEMETRY", "false")
        try:
            mem0 = _import_mem0()
        except ImportError as exc:
            print(f"{MEM0_INSTALL_HINT} ({exc})", file=sys.stderr)
            return 2
        arm = Mem0Arm(
            mem0,
            Mem0Config(
                llm_provider=args.mem0_llm_provider,
                llm_model=args.mem0_llm_model,
                embedder_provider=args.mem0_embedder_provider,
                embedder_model=args.mem0_embedder_model,
                embedding_dims=args.mem0_embedding_dims,
                ollama_base_url=args.mem0_ollama_base_url,
            ),
            timeout=args.mem0_timeout,
            progress=sys.stderr,
        )
        runner = arm.run_scenario
        header = arm.header(platform.python_version())
    else:
        runner = run_scenario
        header = (f"# Update-integrity results — lean-memory {__version__} "
                  f"(offline stub backends, Python {platform.python_version()})")

    rows: list[tuple[str, list[AssertionResult]]] = []
    for sc in SCENARIOS:
        if args.root:
            root = Path(args.root) / sc.key
            root.mkdir(parents=True, exist_ok=True)
            rows.append((sc.key, runner(sc, root)))
        else:
            with tempfile.TemporaryDirectory() as td:
                rows.append((sc.key, runner(sc, Path(td))))

    return 0 if emit(rows, header, args.markdown) else 1


if __name__ == "__main__":
    raise SystemExit(main())
