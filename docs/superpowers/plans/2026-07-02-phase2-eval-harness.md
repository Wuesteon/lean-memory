# Phase 2 Eval Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run the Phase 2 benchmark harness: LongMemEval Knowledge-Updates slice and LoCoMo temporal slice against lean-memory's public API, under sha256-pinned frozen-judge configs, including the is_latest A/B Key Experiment.

**Architecture:** Four flat sibling scripts under `bench/` (`phase2_ingest.py`, `phase2_reader.py`, `phase2_judge.py`, `phase2_eval.py`) that reuse BET-2's statistics and refusal discipline from `bench/bet2_ablation.py`. Staged, resumable pipeline: ingest-once into a cache of per-namespace SQLite files → copy per arm → retrieve+read (JSONL) → judge (JSONL) → aggregate into a pinned result file. One pre-registered engine fix (sparse-arm `as_of`) lands first, TDD.

**Tech Stack:** Python 3.13 venv at `.venv`, pytest, lean-memory public API (`Memory.add`/`Memory.search`), OpenRouter via the `openai` client, local models via existing extras (sentence-transformers, gliner2, ollama).

**Spec:** `docs/superpowers/specs/2026-07-02-phase2-eval-harness-design.md`

## Global Constraints

- Python ≥3.10; run everything with `.venv/bin/python` (Python 3.13.7).
- Ingest uses ONLY the public API: `mem.add(namespace, text, t_ref=<epoch_ms>, source=<str>)`; retrieval only `mem.search(namespace, query, k=..., as_of=..., is_latest_only=...)`.
- Frozen models: backbone `openai/gpt-4.1-mini`; judges `openai/gpt-4o-2024-08-06` (lme-official), `openai/gpt-4o-mini` (locomo-lenient), `openai/gpt-4o` (locomo-strict); embedder `Qwen/Qwen3-Embedding-0.6B`; reranker `cross-encoder/ettin-reranker-32m-v1`; generator `fastino/gliner2-base-v1`; typer `qwen2.5:3b`.
- All LLM calls: `temperature=0`. Reader/judge content is never retried; transport errors retry with backoff then raise `BackendUnavailable` (imported from `bet2_ablation`) — never a silent 0.
- Provider: OpenRouter, `base_url="https://openrouter.ai/api/v1"`, key from env `OPENROUTER_API_KEY`.
- `k` defaults to 10. Bootstrap seed: reuse `BOOTSTRAP_SEED = 20260620` from `bet2_ablation`.
- Offline (no `--real`) runs stubs end-to-end and prints `PLUMBING CHECK ONLY — NO VERDICT`; result files are written only with `--real`.
- bench/ scripts run directly via the same `sys.path` bootstrap `bet2_ablation.py` uses (insert `bench/` and `src/` at import).
- Every result-bearing artifact records: judge_model, judge_prompt (verbatim), backbone_model, dataset sha256, engine model IDs, retrieval constants, git commit.

---

### Task 1: Engine fix — sparse arm honors `as_of` (TDD)

**Files:**
- Modify: `src/lean_memory/store/base.py:67-71` (abstract `sparse_search` signature)
- Modify: `src/lean_memory/store/sqlite_store.py:217-239` (`sparse_search`)
- Modify: `src/lean_memory/retrieve/retriever.py:56` (pass `as_of` to sparse arm)
- Test: `tests/test_asof_sparse.py` (create)

**Interfaces:**
- Produces: `SqliteStore.sparse_search(self, query_text: str, k: int, *, is_latest_only: bool = True, as_of: Optional[int] = None) -> list[tuple[str, float]]` — later tasks call it only through `Memory.search`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_asof_sparse.py
"""Regression: the BM25 sparse arm must honor as_of (it previously ignored it,
leaking facts from outside the temporal window into fused results)."""

from lean_memory.embed.fake import FakeEmbedder
from lean_memory.retrieve.rerank import IdentityReranker
from lean_memory.retrieve.retriever import Retriever
from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Entity, Episode, Fact


def _seed_store(tmp_path):
    emb = FakeEmbedder()
    store = SqliteStore(tmp_path / "t.db", dim=emb.dim, coarse_dim=emb.coarse_dim)
    ep = Episode(namespace="ns", raw="seed", t_ref=1_000)
    store.add_episode(ep)
    ent = store.upsert_entity(Entity(namespace="ns", name="user", type="person"))
    old = Fact(
        namespace="ns", subject_id=ent.id, predicate="works_at",
        fact_text="user works at zorbex", valid_at=1_000, episode_id=ep.id,
        valid_to=2_000, is_latest=0,
    )
    new = Fact(
        namespace="ns", subject_id=ent.id, predicate="works_at",
        fact_text="user works at quandril", valid_at=3_000, episode_id=ep.id,
    )
    for f in (old, new):
        full, coarse = emb.embed_with_coarse(f.fact_text)
        store.add_fact(f, full, coarse)
    return emb, store, old, new


def test_sparse_search_respects_as_of(tmp_path):
    _, store, old, new = _seed_store(tmp_path)
    hits = store.sparse_search("works", 5, is_latest_only=False, as_of=1_500)
    ids = [fid for fid, _ in hits]
    assert old.id in ids
    assert new.id not in ids  # valid_at=3000 > as_of — must not leak via BM25


def test_retriever_as_of_excludes_late_fact_end_to_end(tmp_path):
    emb, store, old, new = _seed_store(tmp_path)
    r = Retriever(store, emb, IdentityReranker())
    got = r.retrieve("works", 5, as_of=1_500, is_latest_only=False)
    got_ids = [x.fact.id for x in got]
    assert old.id in got_ids
    assert new.id not in got_ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_asof_sparse.py -v`
Expected: both tests FAIL — first with `TypeError: sparse_search() got an unexpected keyword argument 'as_of'`, second with `new.id` present in results.

- [ ] **Step 3: Implement the fix**

In `src/lean_memory/store/base.py` change the abstract method to:

```python
    @abstractmethod
    def sparse_search(
        self, query_text: str, k: int, *, is_latest_only: bool = True,
        as_of: Optional[int] = None,
    ) -> list[tuple[str, float]]:
        """BM25 lexical search. Returns [(fact_id, score)] best-first.
        as_of applies the same interval predicate as the dense arm."""
```

In `src/lean_memory/store/sqlite_store.py` replace `sparse_search` with:

```python
    def sparse_search(
        self, query_text: str, k: int, *, is_latest_only: bool = True,
        as_of: Optional[int] = None,
    ) -> list[tuple[str, float]]:
        # FTS5 BM25: lower bm25() is better, so we negate to "higher is better".
        needs_row_check = is_latest_only or as_of is not None
        rows = self._db.execute(
            """SELECT f.fact_id AS fact_id, bm25(fact_fts) AS score
               FROM fact_fts f
               WHERE fact_fts MATCH ?
               ORDER BY score LIMIT ?""",
            (_fts_query(query_text), k * (2 if needs_row_check else 1)),
        ).fetchall()
        out: list[tuple[str, float]] = []
        for r in rows:
            if needs_row_check:
                row = self._db.execute(
                    "SELECT is_latest, valid_at, valid_to FROM fact WHERE id=?",
                    (r["fact_id"],),
                ).fetchone()
                if not row:
                    continue
                if is_latest_only and not row["is_latest"]:
                    continue
                if as_of is not None and not (
                    row["valid_at"] <= as_of
                    and (row["valid_to"] is None or row["valid_to"] > as_of)
                ):
                    continue
            out.append((r["fact_id"], -float(r["score"])))
            if len(out) >= k:
                break
        return out
```

In `src/lean_memory/retrieve/retriever.py:56` change the sparse call to:

```python
        sparse = self.store.sparse_search(
            query, OVER_RETRIEVE, is_latest_only=is_latest_only, as_of=as_of,
        )
```

- [ ] **Step 4: Run the new tests and the full suite**

Run: `.venv/bin/python -m pytest tests/test_asof_sparse.py -v && .venv/bin/python -m pytest tests/ -q`
Expected: new tests PASS; full suite green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_asof_sparse.py src/lean_memory/store/base.py src/lean_memory/store/sqlite_store.py src/lean_memory/retrieve/retriever.py
git commit -m "fix(store): sparse BM25 arm honors as_of interval predicate"
```

---

### Task 2: Bench scaffolding — `[bench]` extra, gitignore, dirs

**Files:**
- Modify: `pyproject.toml:42` (add extra before `dev`)
- Modify: `.gitignore`
- Create: `bench/results/phase2/.gitkeep`, `tests/fixtures/phase2/` (dir arrives with Task 3 fixtures)

**Interfaces:**
- Produces: importable `openai` package in `.venv`; `bench/.phase2_cache/` ignored by git.

- [ ] **Step 1: Add the extra to `pyproject.toml`** (after the `examples` group):

```toml
# Phase 2 benchmark harness (bench/phase2_*.py): OpenRouter reader/judge client.
bench = [
    "openai>=1.40",
]
```

- [ ] **Step 2: Append cache dir to `.gitignore`**

```
bench/.phase2_cache/
```

- [ ] **Step 3: Install and verify**

Run: `.venv/bin/pip install -e '.[bench]' -q && .venv/bin/python -c "import openai; print(openai.__version__)" && mkdir -p bench/results/phase2 && touch bench/results/phase2/.gitkeep`
Expected: prints an openai version ≥1.40.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml .gitignore bench/results/phase2/.gitkeep
git commit -m "chore(bench): add [bench] extra (openai client) and phase2 cache gitignore"
```

---

### Task 3: `phase2_ingest.py` — dataclasses, timestamp parsers, dataset registry

**Files:**
- Create: `bench/phase2_ingest.py`
- Test: `tests/test_phase2_ingest.py` (create)

**Interfaces:**
- Produces (later tasks import these from `phase2_ingest`):
  - `@dataclass(frozen=True) Turn(text: str, t_ref: int, source: str)`
  - `@dataclass(frozen=True) Question(question_id: str, question_type: str, question: str, gold: str, question_date: str = "", is_abstention: bool = False, category: Optional[int] = None)`
  - `@dataclass IngestUnit(namespace: str, turns: list[Turn], questions: list[Question])`
  - `class DatasetError(ValueError)`
  - `parse_lme_timestamp(s: str) -> int` (epoch-ms, UTC)
  - `parse_locomo_timestamp(s: str) -> int` (epoch-ms, UTC)
  - `ensure_dataset(name: str, data_dir: Path) -> tuple[Path, str]` — returns (path, sha256); downloads if missing, verifies recorded sha thereafter
  - `DATASETS: dict[str, dict]` with keys `locomo10`, `lme_oracle`, `lme_s`

- [ ] **Step 1: Write failing tests for the parsers**

```python
# tests/test_phase2_ingest.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from phase2_ingest import parse_lme_timestamp, parse_locomo_timestamp


def test_parse_lme_timestamp():
    # 2023-04-10 23:07 UTC = 1681168020 s
    assert parse_lme_timestamp("2023/04/10 (Mon) 23:07") == 1_681_168_020_000


def test_parse_locomo_timestamp():
    # 2023-05-08 13:56 UTC = 1683554160 s
    assert parse_locomo_timestamp("1:56 pm on 8 May, 2023") == 1_683_554_160_000
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_phase2_ingest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phase2_ingest'`.

- [ ] **Step 3: Create `bench/phase2_ingest.py`**

```python
"""Phase 2 ingest adapters: LongMemEval + LoCoMo → lean-memory public API.

Maps each benchmark's conversation structure onto exactly two calls:
    mem.add(namespace, text, t_ref=<epoch_ms>, source=<str>)
No private engine paths. See docs/superpowers/specs/2026-07-02-phase2-eval-harness-design.md.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_BENCH = Path(__file__).resolve().parent
_ROOT = _BENCH.parent
for _p in (str(_BENCH), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dateutil import parser as dateparser  # noqa: E402  (core dep python-dateutil)


class DatasetError(ValueError):
    """A dataset file failed validation — abort loudly, never mis-score."""


@dataclass(frozen=True)
class Turn:
    text: str
    t_ref: int  # epoch-ms world time
    source: str


@dataclass(frozen=True)
class Question:
    question_id: str
    question_type: str
    question: str
    gold: str
    question_date: str = ""
    is_abstention: bool = False
    category: Optional[int] = None


@dataclass
class IngestUnit:
    namespace: str
    turns: list[Turn] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)


# ── timestamps ──

_PAREN = re.compile(r"\s*\([^)]*\)")


def parse_lme_timestamp(s: str) -> int:
    """LongMemEval session/question dates: '2023/04/10 (Mon) 23:07' → epoch-ms UTC."""
    clean = _PAREN.sub("", s).strip()
    dt = datetime.strptime(clean, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_locomo_timestamp(s: str) -> int:
    """LoCoMo session dates: '1:56 pm on 8 May, 2023' → epoch-ms UTC."""
    dt = dateparser.parse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ── dataset registry + download ──

DATASETS = {
    "locomo10": {
        "url": "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
        "filename": "locomo10.json",
        "license": "CC BY-NC 4.0 (eval use only, not redistributed)",
    },
    "lme_oracle": {
        "url": "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json",
        "filename": "longmemeval_oracle.json",
        "license": "MIT",
    },
    "lme_s": {
        "url": "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json",
        "filename": "longmemeval_s_cleaned.json",
        "license": "MIT",
    },
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_dataset(name: str, data_dir: Path) -> tuple[Path, str]:
    """Download-if-missing; record sha256 on first download; verify thereafter."""
    spec = DATASETS[name]
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / spec["filename"]
    sha_path = data_dir / (spec["filename"] + ".sha256")
    if not path.exists():
        print(f"downloading {name} ← {spec['url']}")
        urllib.request.urlretrieve(spec["url"], path)  # noqa: S310
    sha = _sha256_file(path)
    if sha_path.exists():
        pinned = sha_path.read_text().strip()
        if pinned != sha:
            raise DatasetError(
                f"{path.name}: sha256 {sha[:16]}… != pinned {pinned[:16]}… "
                "(dataset changed upstream — delete both files to re-pin deliberately)"
            )
    else:
        sha_path.write_text(sha)
    return path, sha
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_phase2_ingest.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add bench/phase2_ingest.py tests/test_phase2_ingest.py
git commit -m "feat(bench): phase2 ingest skeleton — types, timestamp parsers, dataset registry"
```

---

### Task 4: `phase2_ingest.py` — LongMemEval loader (both shapes)

**Files:**
- Modify: `bench/phase2_ingest.py`
- Create: `tests/fixtures/phase2/lme_s_mini.json`, `tests/fixtures/phase2/lme_oracle_mini.json`
- Test: `tests/test_phase2_ingest.py`

**Interfaces:**
- Produces: `load_longmemeval(path: Path, slice: str = "all", expect_counts: bool = False) -> list[IngestUnit]` — one unit per question; `LME_TOTAL_QUESTIONS = 500`, `LME_KU_QUESTIONS = 78`.

- [ ] **Step 1: Create the fixtures**

`tests/fixtures/phase2/lme_s_mini.json` (public `_s` shape: turn-arrays + parallel id/date lists):

```json
[
  {
    "question_id": "ku_001",
    "question_type": "knowledge-update",
    "question": "Where does the user work now?",
    "answer": "Quandril",
    "question_date": "2023/06/01 (Thu) 10:00",
    "haystack_session_ids": ["s1", "s2"],
    "haystack_dates": ["2023/04/10 (Mon) 23:07", "2023/05/20 (Sat) 11:30"],
    "haystack_sessions": [
      [
        {"role": "user", "content": "I just started working at Zorbex."},
        {"role": "assistant", "content": "Congrats on the Zorbex job!"}
      ],
      [
        {"role": "user", "content": "I left Zorbex and now I work at Quandril."}
      ]
    ],
    "answer_session_ids": ["s2"]
  },
  {
    "question_id": "ms_001_abs",
    "question_type": "multi-session",
    "question": "What color is the user's bike?",
    "answer": "The user never mentioned owning a bike.",
    "question_date": "2023/06/02 (Fri) 09:00",
    "haystack_session_ids": ["s3"],
    "haystack_dates": ["2023/04/11 (Tue) 08:00"],
    "haystack_sessions": [
      [
        {"role": "user", "content": "I love hiking near the coast."}
      ]
    ],
    "answer_session_ids": []
  }
]
```

`tests/fixtures/phase2/lme_oracle_mini.json` (oracle shape: `{session_id, turns}` objects, no parallel lists):

```json
[
  {
    "question_id": "ku_001",
    "question_type": "knowledge-update",
    "question": "Where does the user work now?",
    "answer": "Quandril",
    "question_date": "2023/06/01 (Thu) 10:00",
    "haystack_dates": ["2023/04/10 (Mon) 23:07", "2023/05/20 (Sat) 11:30"],
    "haystack_sessions": [
      {
        "session_id": "s1",
        "turns": [
          {"role": "user", "content": "I just started working at Zorbex."},
          {"role": "assistant", "content": "Congrats on the Zorbex job!"}
        ]
      },
      {
        "session_id": "s2",
        "turns": [
          {"role": "user", "content": "I left Zorbex and now I work at Quandril."}
        ]
      }
    ],
    "answer_session_ids": ["s2"]
  }
]
```

- [ ] **Step 2: Write failing tests** (append to `tests/test_phase2_ingest.py`)

```python
from phase2_ingest import DatasetError, load_longmemeval

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "phase2"


def test_lme_s_shape_loads_units_with_ordered_trefs():
    units = load_longmemeval(FIXTURES / "lme_s_mini.json")
    assert [u.namespace for u in units] == ["ku_001", "ms_001_abs"]
    u = units[0]
    assert len(u.turns) == 3
    t1, t2, t3 = u.turns
    assert t1.t_ref == 1_681_168_020_000            # session 1 start
    assert t2.t_ref == 1_681_168_020_000 + 1_000    # +1s per turn
    assert t3.t_ref == parse_lme_timestamp("2023/05/20 (Sat) 11:30")
    assert (t1.source, t2.source, t3.source) == ("user", "assistant", "user")
    assert u.questions[0].gold == "Quandril"
    assert u.questions[0].is_abstention is False
    assert units[1].questions[0].is_abstention is True


def test_lme_oracle_shape_matches_s_shape():
    s = load_longmemeval(FIXTURES / "lme_s_mini.json", slice="ku")
    o = load_longmemeval(FIXTURES / "lme_oracle_mini.json", slice="ku")
    assert [t.text for t in s[0].turns] == [t.text for t in o[0].turns]
    assert [t.t_ref for t in s[0].turns] == [t.t_ref for t in o[0].turns]


def test_lme_ku_slice_filters():
    units = load_longmemeval(FIXTURES / "lme_s_mini.json", slice="ku")
    assert [u.namespace for u in units] == ["ku_001"]


def test_lme_expect_counts_aborts_on_fixture():
    import pytest
    with pytest.raises(DatasetError):
        load_longmemeval(FIXTURES / "lme_s_mini.json", expect_counts=True)
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_phase2_ingest.py -v`
Expected: new tests FAIL with `ImportError: cannot import name 'load_longmemeval'`.

- [ ] **Step 4: Implement the loader** (append to `bench/phase2_ingest.py`)

```python
# ── LongMemEval ──

LME_TOTAL_QUESTIONS = 500
LME_KU_QUESTIONS = 78
_TURN_STEP_MS = 1_000  # +1s per turn inside a session: supersession order is defined


def _lme_sessions(entry: dict) -> list[tuple[str, Optional[str], list[dict]]]:
    """Normalize both on-disk shapes → [(session_id, date_str|None, turns)]."""
    ids = entry.get("haystack_session_ids") or []
    dates = entry.get("haystack_dates") or []
    out = []
    for i, item in enumerate(entry["haystack_sessions"]):
        if isinstance(item, list):  # _s shape: the entry IS the turn list
            sid = ids[i] if i < len(ids) else f"{entry['question_id']}_s{i}"
            turns = item
        elif isinstance(item, dict) and isinstance(item.get("turns"), list):
            sid = item.get("session_id") or f"{entry['question_id']}_s{i}"
            turns = item["turns"]
        else:
            raise DatasetError(f"{entry['question_id']}: malformed haystack session #{i}")
        out.append((sid, dates[i] if i < len(dates) else None, turns))
    return out


def load_longmemeval(path: Path, slice: str = "all", expect_counts: bool = False) -> list[IngestUnit]:
    data = json.loads(Path(path).read_text())
    if expect_counts and len(data) != LME_TOTAL_QUESTIONS:
        raise DatasetError(f"expected {LME_TOTAL_QUESTIONS} LME questions, got {len(data)}")
    units: list[IngestUnit] = []
    for entry in data:
        q = Question(
            question_id=entry["question_id"],
            question_type=entry["question_type"],
            question=entry["question"],
            gold=str(entry["answer"]),
            question_date=entry.get("question_date", ""),
            is_abstention=entry["question_id"].endswith("_abs"),
        )
        if slice == "ku" and q.question_type != "knowledge-update":
            continue
        sessions = []
        prev_t0: Optional[int] = None
        for sid, date, sturns in _lme_sessions(entry):
            if date:
                t0 = parse_lme_timestamp(date)
            elif prev_t0 is not None:
                t0 = prev_t0 + 3_600_000  # dateless session: 1h after the previous
            elif q.question_date:
                t0 = parse_lme_timestamp(q.question_date) - 86_400_000 * len(entry["haystack_sessions"])
            else:
                raise DatasetError(f"{q.question_id}: session {sid} has no usable date")
            prev_t0 = t0
            sessions.append((t0, sturns))
        # oracle variant is NOT time-sorted; _s is. Sorting is a no-op for _s.
        sessions.sort(key=lambda x: x[0])
        turns = [
            Turn(text=t["content"], t_ref=t0 + j * _TURN_STEP_MS, source=t["role"])
            for t0, sturns in sessions
            for j, t in enumerate(sturns)
        ]
        units.append(IngestUnit(namespace=q.question_id, turns=turns, questions=[q]))
    if expect_counts and slice == "ku" and len(units) != LME_KU_QUESTIONS:
        raise DatasetError(f"expected {LME_KU_QUESTIONS} KU questions, got {len(units)}")
    return units
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_phase2_ingest.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add bench/phase2_ingest.py tests/test_phase2_ingest.py tests/fixtures/phase2/
git commit -m "feat(bench): LongMemEval loader — both on-disk shapes, ku slice, count anchors"
```

---

### Task 5: `phase2_ingest.py` — LoCoMo loader

**Files:**
- Modify: `bench/phase2_ingest.py`
- Create: `tests/fixtures/phase2/locomo_mini.json`
- Test: `tests/test_phase2_ingest.py`

**Interfaces:**
- Produces: `load_locomo(path: Path, slice: str = "all", expect_counts: bool = False) -> list[IngestUnit]`; `LOCOMO_CATEGORY_NAMES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}`; `LOCOMO_CATEGORY_ANCHORS = {1: 282, 2: 321, 3: 96, 4: 841, 5: 446}`; `LOCOMO_CONVS = 10`. Question ids are `f"{sample_id}_q{idx:03d}"` over the conversation's original qa list order (stable across slices).

- [ ] **Step 1: Create the fixture** `tests/fixtures/phase2/locomo_mini.json`

```json
[
  {
    "sample_id": "conv-mini",
    "qa": [
      {"question": "Where did Caroline move to?", "answer": "Seattle", "category": 2, "evidence": ["D2:1"]},
      {"question": "What does Melanie paint?", "answer": "landscapes", "category": 4, "evidence": ["D1:2"]},
      {"question": "What is Caroline's dog's name?", "adversarial_answer": "No information available", "category": 5, "evidence": []}
    ],
    "conversation": {
      "speaker_a": "Caroline",
      "speaker_b": "Melanie",
      "session_1_date_time": "1:56 pm on 8 May, 2023",
      "session_1": [
        {"speaker": "Caroline", "dia_id": "D1:1", "text": "I'm thinking about moving out of Portland."},
        {"speaker": "Melanie", "dia_id": "D1:2", "text": "I spent the weekend painting landscapes."}
      ],
      "session_2_date_time": "7:55 pm on 9 June, 2023",
      "session_2": [
        {"speaker": "Caroline", "dia_id": "D2:1", "text": "I finally moved to Seattle last week!", "img_url": ["http://example.com/1.jpg"], "blip_caption": "a moving truck"}
      ]
    }
  }
]
```

- [ ] **Step 2: Write failing tests** (append to `tests/test_phase2_ingest.py`)

```python
from phase2_ingest import load_locomo, parse_locomo_timestamp


def test_locomo_loads_conversation_unit():
    units = load_locomo(FIXTURES / "locomo_mini.json")
    assert len(units) == 1
    u = units[0]
    assert u.namespace == "conv-mini"
    assert len(u.turns) == 3
    assert u.turns[0].text == "Caroline: I'm thinking about moving out of Portland."
    assert u.turns[0].source == "Caroline"
    assert u.turns[0].t_ref == parse_locomo_timestamp("1:56 pm on 8 May, 2023")
    assert u.turns[1].t_ref == u.turns[0].t_ref + 1_000
    # image turn carries the caption on its own line
    assert u.turns[2].text == (
        "Caroline: I finally moved to Seattle last week!\n"
        "Caroline shared a photo: a moving truck"
    )
    # slice "all" keeps categories 1-4 only (adversarial excluded)
    assert [q.category for q in u.questions] == [2, 4]
    assert u.questions[0].question_id == "conv-mini_q000"
    assert u.questions[0].question_type == "temporal"


def test_locomo_temporal_slice():
    units = load_locomo(FIXTURES / "locomo_mini.json", slice="temporal")
    assert [q.category for q in units[0].questions] == [2]


def test_locomo_expect_counts_aborts_on_fixture():
    import pytest
    with pytest.raises(DatasetError):
        load_locomo(FIXTURES / "locomo_mini.json", expect_counts=True)
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_phase2_ingest.py -v`
Expected: FAIL with `ImportError: cannot import name 'load_locomo'`.

- [ ] **Step 4: Implement** (append to `bench/phase2_ingest.py`)

```python
# ── LoCoMo ──

LOCOMO_CATEGORY_NAMES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}
LOCOMO_CATEGORY_ANCHORS = {1: 282, 2: 321, 3: 96, 4: 841, 5: 446}
LOCOMO_CONVS = 10
_SESSION_KEY = re.compile(r"^session_(\d+)$")


def load_locomo(path: Path, slice: str = "all", expect_counts: bool = False) -> list[IngestUnit]:
    data = json.loads(Path(path).read_text())
    if expect_counts:
        if len(data) != LOCOMO_CONVS:
            raise DatasetError(f"expected {LOCOMO_CONVS} conversations, got {len(data)}")
        counts: dict[int, int] = {}
        for conv in data:
            for qa in conv["qa"]:
                counts[qa["category"]] = counts.get(qa["category"], 0) + 1
        if counts != LOCOMO_CATEGORY_ANCHORS:
            raise DatasetError(f"category counts {counts} != anchors {LOCOMO_CATEGORY_ANCHORS}")
    wanted = {2} if slice == "temporal" else {1, 2, 3, 4}
    units: list[IngestUnit] = []
    for conv in data:
        c = conv["conversation"]
        sessions = sorted(
            (int(m.group(1)) for key in c if (m := _SESSION_KEY.match(key))),
        )
        turns: list[Turn] = []
        for n in sessions:
            date_key = f"session_{n}_date_time"
            if date_key not in c:
                raise DatasetError(f"{conv['sample_id']}: missing {date_key}")
            t0 = parse_locomo_timestamp(c[date_key])
            for j, d in enumerate(c[f"session_{n}"]):
                text = f"{d['speaker']}: {d['text']}"
                if d.get("blip_caption"):
                    text += f"\n{d['speaker']} shared a photo: {d['blip_caption']}"
                turns.append(Turn(text=text, t_ref=t0 + j * _TURN_STEP_MS, source=d["speaker"]))
        questions = [
            Question(
                question_id=f"{conv['sample_id']}_q{i:03d}",
                question_type=LOCOMO_CATEGORY_NAMES[qa["category"]],
                question=qa["question"],
                gold=str(qa.get("answer", qa.get("adversarial_answer", ""))),
                category=qa["category"],
            )
            for i, qa in enumerate(conv["qa"])
            if qa["category"] in wanted
        ]
        units.append(IngestUnit(namespace=conv["sample_id"], turns=turns, questions=questions))
    return units
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_phase2_ingest.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add bench/phase2_ingest.py tests/test_phase2_ingest.py tests/fixtures/phase2/locomo_mini.json
git commit -m "feat(bench): LoCoMo loader — speaker prefixes, photo captions, category anchors"
```

---

### Task 6: `phase2_ingest.py` — memory factory, cached resumable ingest, telemetry, CLI

**Files:**
- Modify: `bench/phase2_ingest.py`
- Test: `tests/test_phase2_ingest.py`

**Interfaces:**
- Produces:
  - `DEFAULT_EMBEDDER = "Qwen/Qwen3-Embedding-0.6B"`
  - `build_memory(root: Path, *, real: bool, embedder_name: str = DEFAULT_EMBEDDER) -> Memory` — offline: all stubs; real: `SentenceTransformerEmbedder(embedder_name)` + `CrossEncoderReranker()` + `Gliner2Generator()` + `OllamaTyper()` (router/contradiction keep frozen defaults 0.5 / 0.80/0.45).
  - `preflight_real() -> None` — raises `BackendUnavailable` if Ollama is unreachable.
  - `ingest_units(units: list[IngestUnit], cache_dir: Path, *, real: bool, embedder_name: str = DEFAULT_EMBEDDER, limit: Optional[int] = None) -> dict` — resumable; returns the manifest dict.
  - Manifest JSON at `cache_dir/manifest.json`: `{"engine": {...}, "namespaces": {ns: {"done": true, "turns": n, "facts": n, "supersessions": n, "escalated": n, "seen": n, "add_ms_p50": f, "add_ms_p95": f}}}`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_phase2_ingest.py`)

```python
import json


def test_offline_ingest_cache_and_resume(tmp_path):
    from phase2_ingest import build_memory, ingest_units, load_longmemeval

    units = load_longmemeval(FIXTURES / "lme_s_mini.json")
    cache = tmp_path / "cache"
    m1 = ingest_units(units, cache, real=False)
    assert set(m1["namespaces"]) == {"ku_001", "ms_001_abs"}
    assert all(v["done"] for v in m1["namespaces"].values())
    assert (cache / "manifest.json").exists()
    assert (cache / "ku_001.db").exists()
    # resume: second call must not re-ingest (facts counters unchanged)
    m2 = ingest_units(units, cache, real=False)
    assert m2["namespaces"] == m1["namespaces"]
    # searchable through the public API
    mem = build_memory(cache, real=False)
    hits = mem.search("ku_001", "where does the user work", k=3)
    mem.close()
    assert isinstance(hits, list)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_phase2_ingest.py::test_offline_ingest_cache_and_resume -v`
Expected: FAIL — `ImportError: cannot import name 'build_memory'`.

- [ ] **Step 3: Implement** (append to `bench/phase2_ingest.py`)

```python
# ── memory factory + cached ingest ──

import sqlite3  # noqa: E402
import time  # noqa: E402

from lean_memory.memory import Memory  # noqa: E402

DEFAULT_EMBEDDER = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_TYPER_MODEL = "qwen2.5:3b"
DEFAULT_GENERATOR_MODEL = "fastino/gliner2-base-v1"
DEFAULT_RERANKER_MODEL = "cross-encoder/ettin-reranker-32m-v1"


def build_memory(root: Path, *, real: bool, embedder_name: str = DEFAULT_EMBEDDER) -> Memory:
    """Offline: every backend is the deterministic stub (plumbing only).
    Real: the full production stack; router/contradiction keep frozen defaults."""
    if not real:
        return Memory(root=root)
    from lean_memory.embed.sentence_transformer import SentenceTransformerEmbedder
    from lean_memory.extract.gliner_extractor import Gliner2Generator
    from lean_memory.extract.llm_typer import OllamaTyper
    from lean_memory.retrieve.rerank import CrossEncoderReranker

    return Memory(
        root=root,
        embedder=SentenceTransformerEmbedder(embedder_name),
        reranker=CrossEncoderReranker(),
        generator=Gliner2Generator(),
        typer=OllamaTyper(DEFAULT_TYPER_MODEL),
    )


def preflight_real() -> None:
    """Abort with guidance (never mid-ingest) when Ollama is down."""
    from bet2_ablation import BackendUnavailable

    try:
        import ollama

        ollama.list()
    except Exception as exc:  # noqa: BLE001 — any transport failure = unavailable
        raise BackendUnavailable(
            f"ollama unreachable: {exc}\n  Start it first:  ollama serve  &&  ollama pull {DEFAULT_TYPER_MODEL}"
        ) from exc


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    i = min(len(ys) - 1, int(round(p * (len(ys) - 1))))
    return ys[i]


def _count_supersessions(db_path: Path) -> int:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        return db.execute(
            "SELECT COUNT(*) FROM fact WHERE superseded_by IS NOT NULL"
        ).fetchone()[0]


def ingest_units(
    units: list[IngestUnit],
    cache_dir: Path,
    *,
    real: bool,
    embedder_name: str = DEFAULT_EMBEDDER,
    limit: Optional[int] = None,
) -> dict:
    from lean_memory.extract.llm_typer import TyperError
    from bet2_ablation import BackendUnavailable
    from lean_memory.memory import _SAFE_NS

    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / "manifest.json"
    engine = {"real": real, "embedder": embedder_name if real else "FakeEmbedder",
              "generator": DEFAULT_GENERATOR_MODEL if real else "StubCandidateGenerator",
              "typer": DEFAULT_TYPER_MODEL if real else "StubTyper"}
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {
        "engine": engine, "namespaces": {}}
    if manifest["engine"] != engine:
        raise DatasetError(
            f"cache {cache_dir} was built with engine {manifest['engine']}, requested {engine} — use a fresh cache dir")

    if real:
        preflight_real()
    mem = build_memory(cache_dir, real=real, embedder_name=embedder_name)
    try:
        todo = [u for u in units if not manifest["namespaces"].get(u.namespace, {}).get("done")]
        for u in todo[: limit if limit is not None else len(todo)]:
            base = dict(mem.router.cumulative_stats)
            add_ms: list[float] = []
            facts = 0
            for turn in u.turns:
                t0 = time.perf_counter()
                try:
                    facts += len(mem.add(u.namespace, turn.text, t_ref=turn.t_ref, source=turn.source))
                except TyperError as exc:
                    raise BackendUnavailable(
                        f"typer died mid-ingest on {u.namespace}: {exc}\n"
                        f"  Restart ollama and re-run — completed namespaces are cached."
                    ) from exc
                add_ms.append((time.perf_counter() - t0) * 1000)
            cur = mem.router.cumulative_stats
            db_path = cache_dir / f"{_SAFE_NS.sub('_', u.namespace) or 'default'}.db"
            manifest["namespaces"][u.namespace] = {
                "done": True,
                "turns": len(u.turns),
                "facts": facts,
                "supersessions": _count_supersessions(db_path),
                "escalated": cur.get("escalated", 0) - base.get("escalated", 0),
                "seen": cur.get("seen", 0) - base.get("seen", 0),
                "add_ms_p50": round(_percentile(add_ms, 0.50), 2),
                "add_ms_p95": round(_percentile(add_ms, 0.95), 2),
            }
            tmp = manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(manifest, indent=1))
            tmp.replace(manifest_path)
            print(f"ingested {u.namespace}: {len(u.turns)} turns → {facts} facts")
    finally:
        mem.close()
    return manifest
```

Note: if `mem.router.cumulative_stats` is a method rather than a dict property, call it (`mem.router.cumulative_stats()`); check `src/lean_memory/extract/router.py` at implementation time and adapt the two call sites — the manifest fields stay the same.

- [ ] **Step 4: Add a `__main__` validation mode** (append to `bench/phase2_ingest.py`)

```python
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Download + validate Phase 2 datasets.")
    ap.add_argument("--data-dir", default=str(_BENCH / ".phase2_cache" / "data"))
    ap.add_argument("--datasets", default="locomo10,lme_oracle,lme_s")
    args = ap.parse_args()
    for name in args.datasets.split(","):
        path, sha = ensure_dataset(name, Path(args.data_dir))
        if name == "locomo10":
            n = sum(len(u.questions) for u in load_locomo(path, expect_counts=True))
            print(f"{name}: sha256 {sha[:16]}  OK  ({n} scorable questions)")
        else:
            ku = load_longmemeval(path, slice="ku", expect_counts=True)
            print(f"{name}: sha256 {sha[:16]}  OK  ({len(ku)} KU questions)")
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_phase2_ingest.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add bench/phase2_ingest.py tests/test_phase2_ingest.py
git commit -m "feat(bench): cached resumable ingest with telemetry + dataset validation CLI"
```

---

### Task 7: `phase2_reader.py` — frozen reader, OpenRouter client, FC baseline

**Files:**
- Create: `bench/phase2_reader.py`
- Test: `tests/test_phase2_reader.py` (create)

**Interfaces:**
- Consumes: `RetrievedFact` (`h.fact.fact_text`, `h.fact.valid_at`); `Turn` from `phase2_ingest`; `BackendUnavailable` from `bet2_ablation`.
- Produces:
  - `READER_SYSTEM_PROMPT`, `FC_SYSTEM_PROMPT` (frozen strings below)
  - `render_user_prompt(question: str, hits: list) -> str`
  - `unit_transcript(unit) -> str`
  - `openrouter_chat(model: str, messages: list[dict], *, temperature: float = 0.0, max_tokens: int = 256) -> str` — shared by judges
  - `class EchoReader: answer(question, hits) -> str` ; `answer_full_context(question, transcript) -> str`
  - `class OpenRouterReader(model: str = "openai/gpt-4.1-mini")` with the same two methods

- [ ] **Step 1: Write failing tests**

```python
# tests/test_phase2_reader.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from lean_memory.types import Fact, RetrievedFact
from phase2_reader import EchoReader, READER_SYSTEM_PROMPT, render_user_prompt


def _hit(text: str, valid_at: int) -> RetrievedFact:
    f = Fact(namespace="ns", subject_id="e1", predicate="about",
             fact_text=text, valid_at=valid_at, episode_id="ep1")
    return RetrievedFact(fact=f, final_score=1.0, relevance=1.0, recency=0.0, importance=0.5)


def test_render_user_prompt_golden():
    hits = [_hit("user works at Quandril", 1_684_580_000_000),   # 2023-05-20
            _hit("user works at Zorbex", 1_681_168_020_000)]     # 2023-04-10
    got = render_user_prompt("Where does the user work now?", hits)
    assert got == (
        "- [2023-05-20] user works at Quandril\n"
        "- [2023-04-10] user works at Zorbex\n"
        "\nQuestion: Where does the user work now?"
    )


def test_render_user_prompt_empty():
    got = render_user_prompt("Q?", [])
    assert got == "(no facts retrieved)\n\nQuestion: Q?"


def test_echo_reader_returns_top1():
    assert EchoReader().answer("q", [_hit("alpha", 0), _hit("beta", 0)]) == "alpha"
    assert EchoReader().answer("q", []) == "I don't know"


def test_reader_prompt_frozen():
    assert READER_SYSTEM_PROMPT == (
        "Answer the question using only the provided facts. If none of the "
        "facts answer the question, say 'I don't know'."
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_phase2_reader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phase2_reader'`.

- [ ] **Step 3: Implement `bench/phase2_reader.py`**

```python
"""Phase 2 reader — an instrument, not a place to squeeze points.

Frozen prompts; temperature 0; no chain-of-thought; content never retried.
Offline EchoReader proves plumbing; OpenRouterReader produces the real number.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_BENCH = Path(__file__).resolve().parent
_ROOT = _BENCH.parent
for _p in (str(_BENCH), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bet2_ablation import BackendUnavailable  # noqa: E402

READER_SYSTEM_PROMPT = (
    "Answer the question using only the provided facts. If none of the "
    "facts answer the question, say 'I don't know'."
)
FC_SYSTEM_PROMPT = (
    "Answer the question using only the provided conversation transcript. "
    "If the transcript does not answer the question, say 'I don't know'."
)
DEFAULT_BACKBONE = "openai/gpt-4.1-mini"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_MAX_TRANSPORT_RETRIES = 5


def _iso_date(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def render_user_prompt(question: str, hits: list) -> str:
    """Facts block, ranked order, each line dated from the monotemporal spine."""
    if hits:
        block = "\n".join(f"- [{_iso_date(h.fact.valid_at)}] {h.fact.fact_text}" for h in hits)
    else:
        block = "(no facts retrieved)"
    return f"{block}\n\nQuestion: {question}"


def unit_transcript(unit) -> str:
    """Full-context baseline input. LoCoMo turn text already carries the
    speaker prefix; LME text does not, so prefix with the role there."""
    lines = []
    for t in unit.turns:
        body = t.text if t.text.startswith(f"{t.source}:") else f"{t.source}: {t.text}"
        lines.append(f"[{_iso_date(t.t_ref)}] {body}")
    return "\n".join(lines)


def openrouter_chat(model: str, messages: list[dict], *, temperature: float = 0.0,
                    max_tokens: int = 256) -> str:
    """One content attempt; transport errors retry with backoff then abort."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise BackendUnavailable(
            "OPENROUTER_API_KEY is not set.\n  export OPENROUTER_API_KEY=sk-or-…  and re-run."
        )
    import openai
    from openai import OpenAI

    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=key)
    last: Exception | None = None
    for attempt in range(_MAX_TRANSPORT_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages,
                temperature=temperature, max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content
            if content is None:
                raise BackendUnavailable(f"{model} returned empty content")
            return content
        except (openai.RateLimitError, openai.APIConnectionError, openai.APIError) as exc:
            last = exc
            time.sleep(2**attempt)
    raise BackendUnavailable(f"OpenRouter unreachable after {_MAX_TRANSPORT_RETRIES} tries: {last}")


class EchoReader:
    """Offline stub: deterministic, semantically meaningless — plumbing only."""

    model = "echo"

    def answer(self, question: str, hits: list) -> str:
        return hits[0].fact.fact_text if hits else "I don't know"

    def answer_full_context(self, question: str, transcript: str) -> str:
        return transcript.splitlines()[0] if transcript else "I don't know"


class OpenRouterReader:
    def __init__(self, model: str = DEFAULT_BACKBONE) -> None:
        self.model = model

    def answer(self, question: str, hits: list) -> str:
        return openrouter_chat(self.model, [
            {"role": "system", "content": READER_SYSTEM_PROMPT},
            {"role": "user", "content": render_user_prompt(question, hits)},
        ]).strip()

    def answer_full_context(self, question: str, transcript: str) -> str:
        return openrouter_chat(self.model, [
            {"role": "system", "content": FC_SYSTEM_PROMPT},
            {"role": "user", "content": f"{transcript}\n\nQuestion: {question}"},
        ], max_tokens=256).strip()
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_phase2_reader.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add bench/phase2_reader.py tests/test_phase2_reader.py
git commit -m "feat(bench): frozen phase2 reader — echo stub, OpenRouter gpt-4.1-mini, FC baseline"
```

---

### Task 8: `phase2_judge.py` — three pinned judges + hashed EvalConfig

**Files:**
- Create: `bench/phase2_judge.py`
- Test: `tests/test_phase2_judge.py` (create)

**Interfaces:**
- Consumes: `Question` from `phase2_ingest`; `openrouter_chat` from `phase2_reader`.
- Produces:
  - `LME_TEMPLATES: dict[str, str]` (keys: `default`, `temporal-reasoning`, `knowledge-update`, `single-session-preference`, `abstention`) — verbatim from LongMemEval `src/evaluation/evaluate_qa.py::get_anscheck_prompt`
  - `lme_anscheck_prompt(qtype, question, answer, response, abstention) -> str`
  - `LOCOMO_JUDGE_SYSTEM_PROMPT`, `LOCOMO_LENIENT_PROMPT` — verbatim from mem0ai/memory-benchmarks `benchmarks/locomo/prompts.py` (`JUDGE_SYSTEM_PROMPT`, `JUDGE_PROMPT` = the no-evidence build)
  - `LOCOMO_STRICT_PROMPT` — ours, frozen below
  - `preprocess_locomo_gold(category: Optional[int], answer: str) -> str`
  - `parse_correct_label(out: str) -> bool` (raises `JudgeParseError` when ambiguous)
  - `class StubJudge / LMEOfficialJudge / LocomoLenientJudge / LocomoStrictJudge`, each with `judge_id: str`, `model: str`, `prompt_repr() -> str`, and `grade(q: Question, hypothesis: str) -> bool`
  - `@dataclass(frozen=True) EvalConfig(benchmark, slice, dataset_file, dataset_sha256, judge_id, judge_model, judge_prompt, backbone_model, provider, k, is_latest_only, reader_prompt, embedder_model, reranker_model, generator_model, typer_model, retrieval_constants, git_commit)` — `retrieval_constants: str` (sorted-JSON string so the dataclass stays hashable/frozen)
  - `config_hash(cfg: EvalConfig) -> str` (full sha256 hex; callers print `[:16]`)
  - `RETRIEVAL_CONSTANTS: dict` built from engine imports (`RRF_K`, `OVER_RETRIEVE`, `W_REL`, `W_REC`, `W_IMP`, `DECAY_LAMBDA` from `lean_memory.retrieve.retriever`; `DEFAULT_HIGH_SIM`, `DEFAULT_LOW_SIM` from `lean_memory.extract.contradiction`; router `conf_threshold` 0.5)

- [ ] **Step 1: Write failing golden tests**

```python
# tests/test_phase2_judge.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from phase2_ingest import Question
from phase2_judge import (
    EvalConfig, LME_TEMPLATES, LOCOMO_LENIENT_PROMPT, StubJudge,
    config_hash, lme_anscheck_prompt, parse_correct_label, preprocess_locomo_gold,
)


def test_lme_ku_template_verbatim():
    # Golden: must match evaluate_qa.py character-for-character.
    assert LME_TEMPLATES["knowledge-update"] == (
        "I will give you a question, a correct answer, and a response from a "
        "model. Please answer yes if the response contains the correct answer. "
        "Otherwise, answer no. If the response contains some previous "
        "information along with an updated answer, the response should be "
        "considered as correct as long as the updated answer is the required "
        "answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: "
        "{}\n\nIs the model response correct? Answer yes or no only."
    )


def test_lme_prompt_dispatch():
    p = lme_anscheck_prompt("multi-session", "Q", "A", "R", abstention=False)
    assert p.startswith("I will give you a question, a correct answer,")
    assert "Question: Q" in p and "Correct Answer: A" in p and "Model Response: R" in p
    pa = lme_anscheck_prompt("multi-session", "Q", "A", "R", abstention=True)
    assert pa.startswith("I will give you an unanswerable question,")


def test_locomo_lenient_contains_load_bearing_rules():
    assert "PARTIAL CREDIT" in LOCOMO_LENIENT_PROMPT
    assert "DATE TOLERANCE" in LOCOMO_LENIENT_PROMPT
    assert "{question}" in LOCOMO_LENIENT_PROMPT


def test_preprocess_locomo_gold_cat3_semicolon():
    assert preprocess_locomo_gold(3, "The Lakers; maybe the Celtics") == "The Lakers"
    assert preprocess_locomo_gold(2, "8 May, 2023; ish") == "8 May, 2023; ish"


def test_parse_correct_label():
    assert parse_correct_label('{"reasoning": "x", "label": "CORRECT"}') is True
    assert parse_correct_label('{"reasoning": "x", "label": "WRONG"}') is False
    assert parse_correct_label("label: CORRECT") is True
    import pytest
    from phase2_judge import JudgeParseError
    with pytest.raises(JudgeParseError):
        parse_correct_label("CORRECT or WRONG, who knows")


def test_stub_judge_substring():
    q = Question(question_id="x", question_type="knowledge-update", question="Q", gold="Quandril")
    assert StubJudge().grade(q, "They work at Quandril now.") is True
    assert StubJudge().grade(q, "They work at Zorbex.") is False


def test_config_hash_stable():
    cfg = EvalConfig(
        benchmark="longmemeval", slice="ku", dataset_file="x.json", dataset_sha256="abc",
        judge_id="lme-official", judge_model="openai/gpt-4o-2024-08-06", judge_prompt="P",
        backbone_model="openai/gpt-4.1-mini", provider="openrouter", k=10,
        is_latest_only=True, reader_prompt="R", embedder_model="E", reranker_model="RR",
        generator_model="G", typer_model="T", retrieval_constants="{}", git_commit="deadbeef",
    )
    h1, h2 = config_hash(cfg), config_hash(cfg)
    assert h1 == h2 and len(h1) == 64
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_phase2_judge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phase2_judge'`.

- [ ] **Step 3: Implement `bench/phase2_judge.py`**

The two upstream prompt sets are transcribed **verbatim** — sources:
- `https://raw.githubusercontent.com/xiaowu0162/LongMemEval/main/src/evaluation/evaluate_qa.py` (`get_anscheck_prompt`)
- `https://raw.githubusercontent.com/mem0ai/memory-benchmarks/main/benchmarks/locomo/prompts.py` (`JUDGE_SYSTEM_PROMPT`, `JUDGE_PROMPT` — the no-evidence template with `{{question}}` → `{question}` etc. already resolved)

```python
"""Phase 2 frozen judges. Three judges, all pinned; EvalConfig sha256 is the
run's identity. The three disputed variables (judge_model, judge_prompt,
backbone_model) live verbatim in every EvalConfig.

Verbatim transcriptions:
  LME_TEMPLATES   ← github.com/xiaowu0162/LongMemEval src/evaluation/evaluate_qa.py
  LOCOMO_LENIENT  ← github.com/mem0ai/memory-benchmarks benchmarks/locomo/prompts.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

_BENCH = Path(__file__).resolve().parent
_ROOT = _BENCH.parent
for _p in (str(_BENCH), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from phase2_ingest import Question  # noqa: E402
from phase2_reader import openrouter_chat  # noqa: E402


class JudgeParseError(RuntimeError):
    """Judge output had no unambiguous label — abort, never silently score 0."""


# ── LongMemEval official templates (verbatim) ──

_LME_DEFAULT = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."

LME_TEMPLATES = {
    "default": _LME_DEFAULT,
    "temporal-reasoning": "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "knowledge-update": "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "single-session-preference": "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "abstention": "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only.",
}


def lme_anscheck_prompt(qtype: str, question: str, answer: str, response: str,
                        abstention: bool = False) -> str:
    if abstention:
        template = LME_TEMPLATES["abstention"]
    elif qtype in ("single-session-user", "single-session-assistant", "multi-session"):
        template = LME_TEMPLATES["default"]
    elif qtype in ("temporal-reasoning", "knowledge-update", "single-session-preference"):
        template = LME_TEMPLATES[qtype]
    else:
        raise NotImplementedError(qtype)
    return template.format(question, answer, response)


# ── LoCoMo lenient judge (Mem0 memory-benchmarks, verbatim; no-evidence build) ──

LOCOMO_JUDGE_SYSTEM_PROMPT = "You are evaluating conversational AI memory recall. Return JSON only with the format requested."

LOCOMO_LENIENT_PROMPT = """Label the generated answer as CORRECT or WRONG.

## Rules

1. **PARTIAL CREDIT**: If the generated answer includes AT LEAST ONE correct item from the gold answer's list, mark CORRECT. Getting 1 out of 2, 2 out of 4, etc. is always acceptable. Only mark WRONG if NONE of the gold answer items appear.

2. **PARAPHRASES COUNT**: Same concept in different words is CORRECT. "Chocolate raspberry tart" = "chocolate cake with raspberries". "Shelter meal service" = "volunteering at a homeless shelter". Emotions and sentiments in the same positive/negative family count as paraphrases: "proud" = "fulfilled" = "accomplished"; "huge success" = "relieved" = "thrilled" (all express positive achievement). Judge semantic meaning, not exact wording.

3. **EXTRA DETAIL IS FINE**: A longer answer that includes the gold answer's key facts plus additional information is CORRECT. Never penalize for being more detailed or specific. If the generated answer adds extra descriptive details beyond the gold answer while still referencing the same core entity or concept, mark CORRECT.

4. **DATE TOLERANCE**: Dates within 14 days of each other are CORRECT. Durations within 50% are CORRECT (e.g., "5 months" matches "six months"; "19 days" matches "two weeks"). Relative dates ("few days before November") match specific dates in the same window. A specific date (e.g., "February 2020") that is consistent with a vague reference (e.g., "a few years ago" relative to 2023) is CORRECT. Converting "last year" to the actual year (e.g., "2022" when conversations are in 2023) is CORRECT.

5. **SEMANTIC OVERLAP**: Judge whether the generated answer addresses the same topic and captures the core idea of the gold answer. Different wording, phrasing, or level of detail should not result in WRONG if the underlying concept matches. For EMOTIONS and FEELINGS questions, answers expressing sentiments in the same valence (positive/negative) about the same event are CORRECT — do not require the exact same emotion word.

6. **SAME REFERENT**: If the generated answer mentions or references the same named entity, character, person, or concept as the gold answer, mark CORRECT — even if the generated answer provides a different physical description or includes additional details. The key question is: does the generated answer identify the same core entity? If yes, it is CORRECT.

7. **FOCUS ON KNOWLEDGE, NOT WORDING**: The goal is to assess whether the system recalled the right fact. Minor differences in specificity, phrasing, or scope should not result in WRONG. Only mark WRONG when the generated answer demonstrates a genuinely different or incorrect understanding.

## ONLY mark WRONG if:
- The generated answer contains ZERO correct items from the gold answer
- The answer addresses a completely different topic

## Question
Question: {question}
Gold answer: {answer}
Generated answer: {response}

Return JSON with "reasoning" (one sentence) and "label" (CORRECT or WRONG). Do NOT include both labels."""


# ── LoCoMo strict judge (ours, frozen) ──

LOCOMO_STRICT_PROMPT = """Label the generated answer as CORRECT or WRONG.

You are a strict grader. Apply these rules exactly:

1. CORRECT only if the generated answer states the same value as the gold answer, or an unambiguous paraphrase of it. Generic or vague answers that merely overlap in topic are WRONG.
2. TEMPORAL PRECISION: For questions about dates or times, the answer must be point-in-time correct. Dates must match within 1 day. Durations must match within 1 of the stated unit (e.g. "18 days" accepts 17-19 days; "3 months" accepts 2-4 months). Anything looser is WRONG.
3. LIST ANSWERS: If the gold answer is a list, the generated answer must contain every item of the list to be CORRECT. A single matching item is not enough.
4. NO CREDIT FOR HEDGING: "I don't know", "not specified", or a refusal is WRONG whenever the gold answer contains a value. It is CORRECT only when the gold answer itself states that the question is unanswerable.
5. STALE VALUES: If the question asks for a current value and the generated answer gives only an earlier, superseded value, it is WRONG — even if that value was once true.

Question: {question}
Gold answer: {answer}
Generated answer: {response}

Return JSON with "reasoning" (one sentence) and "label" (CORRECT or WRONG). Do NOT include both labels."""


def preprocess_locomo_gold(category: Optional[int], answer: str) -> str:
    """Mem0 harness rule: category 3 gold truncated at the first ';'."""
    if category == 3 and ";" in answer:
        return answer.split(";")[0].strip()
    return answer


_LABEL_RE = re.compile(r'"label"\s*:\s*"(CORRECT|WRONG)"', re.IGNORECASE)


def parse_correct_label(out: str) -> bool:
    m = _LABEL_RE.search(out)
    if m:
        return m.group(1).upper() == "CORRECT"
    up = out.upper()
    has_c, has_w = "CORRECT" in up.replace("INCORRECT", ""), "WRONG" in up
    if has_c != has_w:
        return has_c
    raise JudgeParseError(f"ambiguous judge output: {out[:200]!r}")


# ── judge classes ──

class StubJudge:
    """Offline: case-insensitive substring of gold in hypothesis. Plumbing only."""

    judge_id = "stub"
    model = "stub"

    def prompt_repr(self) -> str:
        return "substring(gold, hypothesis)"

    def grade(self, q: Question, hypothesis: str) -> bool:
        return q.gold.lower() in hypothesis.lower()


class LMEOfficialJudge:
    judge_id = "lme-official"
    model = "openai/gpt-4o-2024-08-06"

    def prompt_repr(self) -> str:
        return json.dumps(LME_TEMPLATES, sort_keys=True)

    def grade(self, q: Question, hypothesis: str) -> bool:
        prompt = lme_anscheck_prompt(q.question_type, q.question, q.gold, hypothesis,
                                     abstention=q.is_abstention)
        out = openrouter_chat(self.model, [{"role": "user", "content": prompt}],
                              temperature=0.0, max_tokens=10)
        return "yes" in out.strip().lower()


class _LocomoJudge:
    prompt_template = ""  # subclass sets

    def prompt_repr(self) -> str:
        return self.prompt_template

    def grade(self, q: Question, hypothesis: str) -> bool:
        gold = preprocess_locomo_gold(q.category, q.gold)
        prompt = self.prompt_template.format(question=q.question, answer=gold,
                                             response=hypothesis)
        out = openrouter_chat(self.model, [
            {"role": "system", "content": LOCOMO_JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], temperature=0.0, max_tokens=300)
        return parse_correct_label(out)


class LocomoLenientJudge(_LocomoJudge):
    judge_id = "locomo-lenient"
    model = "openai/gpt-4o-mini"
    prompt_template = LOCOMO_LENIENT_PROMPT


class LocomoStrictJudge(_LocomoJudge):
    judge_id = "locomo-strict"
    model = "openai/gpt-4o"
    prompt_template = LOCOMO_STRICT_PROMPT


# ── frozen config ──

from lean_memory.extract.contradiction import DEFAULT_HIGH_SIM, DEFAULT_LOW_SIM  # noqa: E402
from lean_memory.retrieve.retriever import (  # noqa: E402
    DECAY_LAMBDA, OVER_RETRIEVE, RRF_K, W_IMP, W_REC, W_REL,
)

RETRIEVAL_CONSTANTS = {
    "RRF_K": RRF_K, "OVER_RETRIEVE": OVER_RETRIEVE,
    "W_REL": W_REL, "W_REC": W_REC, "W_IMP": W_IMP,
    "DECAY_LAMBDA": DECAY_LAMBDA,
    "HIGH_SIM": DEFAULT_HIGH_SIM, "LOW_SIM": DEFAULT_LOW_SIM,
    "ROUTER_CONF_THRESHOLD": 0.5,
}


@dataclass(frozen=True)
class EvalConfig:
    benchmark: str
    slice: str
    dataset_file: str
    dataset_sha256: str
    judge_id: str
    judge_model: str
    judge_prompt: str
    backbone_model: str
    provider: str
    k: int
    is_latest_only: object  # True | False | "fc"
    reader_prompt: str
    embedder_model: str
    reranker_model: str
    generator_model: str
    typer_model: str
    retrieval_constants: str  # sorted-JSON string of RETRIEVAL_CONSTANTS
    git_commit: str


def config_hash(cfg: EvalConfig) -> str:
    payload = json.dumps(asdict(cfg), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_phase2_judge.py -v`
Expected: all PASS. If the KU golden test fails on whitespace, fix the transcription in `phase2_judge.py` against the upstream source (the upstream file is the truth; note the `default`/`temporal-reasoning` templates end with a trailing space before `\n\nQuestion:` — keep it).

- [ ] **Step 5: Commit**

```bash
git add bench/phase2_judge.py tests/test_phase2_judge.py
git commit -m "feat(bench): three pinned judges (LME official, LoCoMo lenient+strict) + hashed EvalConfig"
```

---

### Task 9: `phase2_eval.py` — paired accuracy bootstrap + aggregation

**Files:**
- Create: `bench/phase2_eval.py`
- Test: `tests/test_phase2_eval.py` (create)

**Interfaces:**
- Consumes: `wilson_ci`, `BOOTSTRAP_SEED` from `bet2_ablation`.
- Produces:
  - `paired_bootstrap_acc_delta(arm_a: list[bool], arm_b: list[bool], *, n: int = 1000, seed: int = BOOTSTRAP_SEED) -> tuple[float, float, float]` — (delta_pp, lo95_pp, hi95_pp), delta = (mean(a) − mean(b))·100, indices resampled once per replicate for both arms
  - `aggregate_scores(verdicts: list[dict], qtypes: dict[str, str]) -> dict` — `{"overall": f, "wilson_ci": [lo, hi], "n": int, "by_type": {type: {"acc": f, "n": int}}}`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_phase2_eval.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from phase2_eval import aggregate_scores, paired_bootstrap_acc_delta


def test_paired_bootstrap_acc_delta_identical_arms_is_zero():
    a = [True, False, True, True, False] * 10
    point, lo, hi = paired_bootstrap_acc_delta(a, a)
    assert point == 0.0 and lo == 0.0 and hi == 0.0


def test_paired_bootstrap_acc_delta_deterministic_and_signed():
    a = [True] * 30 + [False] * 10
    b = [True] * 20 + [False] * 20
    p1 = paired_bootstrap_acc_delta(a, b)
    p2 = paired_bootstrap_acc_delta(a, b)
    assert p1 == p2                       # seeded
    assert p1[0] == 25.0                  # (0.75-0.50)*100
    assert p1[1] <= p1[0] <= p1[2]


def test_aggregate_scores():
    verdicts = [
        {"question_id": "a", "label": True},
        {"question_id": "b", "label": False},
        {"question_id": "c", "label": True},
    ]
    qtypes = {"a": "knowledge-update", "b": "knowledge-update", "c": "multi-session"}
    got = aggregate_scores(verdicts, qtypes)
    assert got["n"] == 3
    assert abs(got["overall"] - 2 / 3) < 1e-9
    assert got["by_type"]["knowledge-update"] == {"acc": 0.5, "n": 2}
    assert 0.0 <= got["wilson_ci"][0] <= got["overall"] <= got["wilson_ci"][1] <= 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_phase2_eval.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phase2_eval'`.

- [ ] **Step 3: Create `bench/phase2_eval.py`** (stats + aggregation only; runner lands in Task 10)

```python
"""Phase 2 runner — LongMemEval / LoCoMo slices against lean-memory's public API.

Stages: ingest → arms (copy) → read → judge → aggregate. Every stage resumable.
Offline default is a plumbing check and REFUSES a verdict; --real produces the
pinned result file. See docs/superpowers/specs/2026-07-02-phase2-eval-harness-design.md.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

_BENCH = Path(__file__).resolve().parent
_ROOT = _BENCH.parent
for _p in (str(_BENCH), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bet2_ablation import BOOTSTRAP_SEED, wilson_ci  # noqa: E402


def paired_bootstrap_acc_delta(
    arm_a: list[bool], arm_b: list[bool], *, n: int = 1000, seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float, float]:
    """95% CI on (acc_A − acc_B) in percentage points, paired over questions."""
    assert len(arm_a) == len(arm_b) and arm_a, "arms must be same-length, non-empty"
    m = len(arm_a)
    point = (sum(arm_a) / m - sum(arm_b) / m) * 100.0
    rng = random.Random(seed)
    deltas = []
    for _ in range(n):
        idx = [rng.randrange(m) for _ in range(m)]
        da = sum(arm_a[i] for i in idx) / m
        db = sum(arm_b[i] for i in idx) / m
        deltas.append((da - db) * 100.0)
    deltas.sort()
    lo = deltas[int(0.025 * n)]
    hi = deltas[min(n - 1, int(0.975 * n))]
    return point, lo, hi


def aggregate_scores(verdicts: list[dict], qtypes: dict[str, str]) -> dict:
    n = len(verdicts)
    if n == 0:
        return {"overall": 0.0, "wilson_ci": [0.0, 0.0], "n": 0, "by_type": {}}
    wins = sum(1 for v in verdicts if v["label"])
    _, lo, hi = wilson_ci(wins, n)
    by_type: dict[str, dict] = {}
    for v in verdicts:
        t = qtypes[v["question_id"]]
        b = by_type.setdefault(t, {"wins": 0, "n": 0})
        b["n"] += 1
        b["wins"] += 1 if v["label"] else 0
    return {
        "overall": wins / n if n else 0.0,
        "wilson_ci": [lo, hi],
        "n": n,
        "by_type": {t: {"acc": b["wins"] / b["n"], "n": b["n"]} for t, b in by_type.items()},
    }
```

Note: `wilson_ci(successes, total)` returns `(p, lo, hi)` — the aggregation uses the lo/hi.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_phase2_eval.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add bench/phase2_eval.py tests/test_phase2_eval.py
git commit -m "feat(bench): paired accuracy bootstrap + per-type aggregation for phase2"
```

---

### Task 10: `phase2_eval.py` — staged runner, arms, resume, result file, offline e2e

**Files:**
- Modify: `bench/phase2_eval.py`
- Test: `tests/test_phase2_eval.py`

**Interfaces:**
- Consumes: everything produced by Tasks 3–9.
- Produces (all in `phase2_eval.py`):
  - `stage_arms(cache_dir: Path, arms_root: Path, arms: list[str]) -> dict[str, Path]` — copies `*.db` per arm dir (skip if the arm dir exists), `fc` gets no copy (needs no DB)
  - `stage_read(units, arm: str, arm_dir: Optional[Path], out_path: Path, *, reader, k: int, real: bool, embedder_name: str) -> None` — appends `{"question_id", "hypothesis", "hits": [{"fact_id", "fact_text", "valid_at", "final_score"}]}` lines; resumes by skipping ids already present
  - `stage_judge(units, hyp_path: Path, out_path: Path, judge) -> None` — appends `{"question_id", "judge_id", "label", "raw"}`; resumes
  - `stage_aggregate(...) -> dict` + `write_result_file(...) -> Path`
  - CLI: `--benchmark {longmemeval,locomo} --slice {ku,temporal,all} --variant {s,oracle} --arms a,b,fc --k 10 --limit N --real --stage {all,ingest,read,judge,aggregate} --data-dir ... --cache-root ...`

- [ ] **Step 1: Write the failing offline e2e tests** (append to `tests/test_phase2_eval.py`)

```python
import hashlib
import json


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "phase2"


def _run_offline(tmp_path, benchmark, fixture, slice_):
    from phase2_eval import run_pipeline

    return run_pipeline(
        benchmark=benchmark, slice=slice_, dataset_path=FIXTURES / fixture,
        dataset_sha256="fixture", arms=["a", "b"], k=3, real=False,
        cache_root=tmp_path / "cache", results_dir=tmp_path / "results", limit=None,
    )


def test_offline_e2e_longmemeval(tmp_path):
    summary = _run_offline(tmp_path, "longmemeval", "lme_s_mini.json", "ku")
    assert summary["plumbing_only"] is True
    assert summary["arms"]["a"]["judges"]["stub"]["n"] == 1
    assert "key_experiment" in summary
    # offline: no result file is written
    assert not list((tmp_path / "results").glob("*.json"))


def test_offline_e2e_locomo(tmp_path):
    summary = _run_offline(tmp_path, "locomo", "locomo_mini.json", "temporal")
    assert summary["arms"]["a"]["judges"]["stub"]["n"] == 1


def test_read_stage_resumes(tmp_path):
    from phase2_eval import run_pipeline

    kw = dict(benchmark="longmemeval", slice="ku", dataset_path=FIXTURES / "lme_s_mini.json",
              dataset_sha256="fixture", arms=["a"], k=3, real=False,
              cache_root=tmp_path / "cache", results_dir=tmp_path / "results", limit=None)
    run_pipeline(**kw)
    hyp = next((tmp_path / "cache").rglob("hypotheses_a_*.jsonl"))
    lines_before = hyp.read_text().splitlines()
    run_pipeline(**kw)  # second run must not duplicate work
    assert hyp.read_text().splitlines() == lines_before


def test_arm_isolation_regression(tmp_path):
    """Running arm A's read stage must not mutate arm B's databases (touch())."""
    from phase2_eval import run_pipeline

    run_pipeline(benchmark="longmemeval", slice="ku",
                 dataset_path=FIXTURES / "lme_s_mini.json", dataset_sha256="fixture",
                 arms=["a", "b"], k=3, real=False, cache_root=tmp_path / "cache",
                 results_dir=tmp_path / "results", limit=None, stop_after="arms")
    b_dbs = sorted((tmp_path / "cache").rglob("arms/b/*.db"))
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in b_dbs]
    run_pipeline(benchmark="longmemeval", slice="ku",
                 dataset_path=FIXTURES / "lme_s_mini.json", dataset_sha256="fixture",
                 arms=["a"], k=3, real=False, cache_root=tmp_path / "cache",
                 results_dir=tmp_path / "results", limit=None)
    after = [hashlib.sha256(p.read_bytes()).hexdigest() for p in b_dbs]
    assert before == after
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_phase2_eval.py -v`
Expected: new tests FAIL — `ImportError: cannot import name 'run_pipeline'`.

- [ ] **Step 3: Implement the runner** (append to `bench/phase2_eval.py`)

```python
# ── staged runner ──

import argparse  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time as _time  # noqa: E402
from typing import Optional  # noqa: E402

from phase2_ingest import (  # noqa: E402
    DEFAULT_EMBEDDER, DEFAULT_GENERATOR_MODEL, DEFAULT_RERANKER_MODEL,
    DEFAULT_TYPER_MODEL, IngestUnit, build_memory, ensure_dataset, ingest_units,
    load_locomo, load_longmemeval,
)
from phase2_judge import (  # noqa: E402
    EvalConfig, LMEOfficialJudge, LocomoLenientJudge, LocomoStrictJudge,
    RETRIEVAL_CONSTANTS, StubJudge, config_hash,
)
from phase2_reader import (  # noqa: E402
    DEFAULT_BACKBONE, EchoReader, FC_SYSTEM_PROMPT, OpenRouterReader,
    READER_SYSTEM_PROMPT, unit_transcript,
)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, cwd=_ROOT).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _jsonl_done(path: Path, key: str) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(line)[key] for line in path.read_text().splitlines() if line}


def _append_jsonl(path: Path, obj: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def stage_arms(cache_dir: Path, arms_root: Path, arms: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for arm in arms:
        if arm == "fc":
            out[arm] = None
            continue
        arm_dir = arms_root / arm
        if not arm_dir.exists():
            arm_dir.mkdir(parents=True)
            for db in cache_dir.glob("*.db"):
                shutil.copy2(db, arm_dir / db.name)
        out[arm] = arm_dir
    return out


def stage_read(units: list[IngestUnit], arm: str, arm_dir: Optional[Path],
               out_path: Path, *, reader, k: int, real: bool, embedder_name: str) -> None:
    done = _jsonl_done(out_path, "question_id")
    mem = None if arm == "fc" else build_memory(arm_dir, real=real, embedder_name=embedder_name)
    try:
        for unit in units:
            for q in unit.questions:
                if q.question_id in done:
                    continue
                if arm == "fc":
                    hyp = reader.answer_full_context(q.question, unit_transcript(unit))
                    hits_out = []
                else:
                    hits = mem.search(unit.namespace, q.question, k=k,
                                      is_latest_only=(arm == "a"))
                    hyp = reader.answer(q.question, hits)
                    hits_out = [{"fact_id": h.fact.id, "fact_text": h.fact.fact_text,
                                 "valid_at": h.fact.valid_at,
                                 "final_score": round(h.final_score, 6)} for h in hits]
                _append_jsonl(out_path, {"question_id": q.question_id,
                                         "hypothesis": hyp, "hits": hits_out})
    finally:
        if mem is not None:
            mem.close()


def stage_judge(units: list[IngestUnit], hyp_path: Path, out_path: Path, judge) -> None:
    qs = {q.question_id: q for u in units for q in u.questions}
    done = _jsonl_done(out_path, "question_id")
    for line in hyp_path.read_text().splitlines():
        entry = json.loads(line)
        qid = entry["question_id"]
        if qid in done or qid not in qs:
            continue
        label = judge.grade(qs[qid], entry["hypothesis"])
        _append_jsonl(out_path, {"question_id": qid, "judge_id": judge.judge_id,
                                 "label": bool(label), "raw": ""})


def _load_units(benchmark: str, slice: str, dataset_path: Path, expect_counts: bool):
    if benchmark == "longmemeval":
        return load_longmemeval(dataset_path, slice=slice, expect_counts=expect_counts)
    return load_locomo(dataset_path, slice=slice, expect_counts=expect_counts)


def run_pipeline(*, benchmark: str, slice: str, dataset_path: Path, dataset_sha256: str,
                 arms: list[str], k: int, real: bool, cache_root: Path, results_dir: Path,
                 limit: Optional[int], embedder_name: str = DEFAULT_EMBEDDER,
                 stage: str = "all", stop_after: Optional[str] = None) -> dict:
    units = _load_units(benchmark, slice, dataset_path, expect_counts=real)
    if limit is not None:
        units = units[:limit]
    qtypes = {q.question_id: q.question_type for u in units for q in u.questions}
    expected_qids = set(qtypes)

    mode = "real" if real else "offline"
    run_key = f"{benchmark}_{slice}_{mode}"
    cache_dir = cache_root / run_key
    arms_root = cache_dir / "arms"

    reader = OpenRouterReader() if real else EchoReader()
    if not real:
        judges = [StubJudge()]
    elif benchmark == "longmemeval":
        judges = [LMEOfficialJudge()]
    else:
        judges = [LocomoLenientJudge(), LocomoStrictJudge()]

    def cfg_for(arm: str, judge) -> EvalConfig:
        return EvalConfig(
            benchmark=benchmark, slice=slice, dataset_file=dataset_path.name,
            dataset_sha256=dataset_sha256, judge_id=judge.judge_id,
            judge_model=judge.model, judge_prompt=judge.prompt_repr(),
            backbone_model=reader.model, provider="openrouter" if real else "offline",
            k=k, is_latest_only={"a": True, "b": False, "fc": "fc"}[arm],
            reader_prompt=READER_SYSTEM_PROMPT if arm != "fc" else FC_SYSTEM_PROMPT,
            embedder_model=embedder_name if real else "FakeEmbedder",
            reranker_model=DEFAULT_RERANKER_MODEL if real else "IdentityReranker",
            generator_model=DEFAULT_GENERATOR_MODEL if real else "StubCandidateGenerator",
            typer_model=DEFAULT_TYPER_MODEL if real else "StubTyper",
            retrieval_constants=json.dumps(RETRIEVAL_CONSTANTS, sort_keys=True),
            git_commit=_git_commit(),
        )

    primary_hash = config_hash(cfg_for("a" if "a" in arms else arms[0], judges[0]))
    print(f"phase2 config sha256: {primary_hash[:16]}")
    if not real:
        print("PLUMBING CHECK ONLY — NO VERDICT (offline stubs; use --real for a number)")

    # stage 1: ingest
    if stage in ("all", "ingest"):
        manifest = ingest_units(units, cache_dir, real=real,
                                embedder_name=embedder_name, limit=None)
    else:
        manifest = json.loads((cache_dir / "manifest.json").read_text())
    if stage == "ingest":
        return {"manifest": manifest}

    # stage 2: arms
    arm_dirs = stage_arms(cache_dir, arms_root, arms)
    if stop_after == "arms":
        return {"arm_dirs": {a: str(p) for a, p in arm_dirs.items()}}

    # stage 3: read
    if stage in ("all", "read"):
        for arm in arms:
            hyp = cache_dir / f"hypotheses_{arm}_{primary_hash[:16]}.jsonl"
            stage_read(units, arm, arm_dirs[arm], hyp, reader=reader, k=k,
                       real=real, embedder_name=embedder_name)
    if stage == "read":
        return {"read": "done"}

    # stage 4: judge
    if stage in ("all", "judge"):
        for arm in arms:
            hyp = cache_dir / f"hypotheses_{arm}_{primary_hash[:16]}.jsonl"
            for judge in judges:
                out = cache_dir / f"verdicts_{arm}_{judge.judge_id}_{primary_hash[:16]}.jsonl"
                stage_judge(units, hyp, out, judge)
    if stage == "judge":
        return {"judge": "done"}

    # stage 5: aggregate (refuses on missing verdicts)
    summary: dict = {"plumbing_only": not real, "config_hash": primary_hash,
                     "benchmark": benchmark, "slice": slice, "arms": {}}
    arm_bools: dict[str, dict[str, list[bool]]] = {}
    for arm in arms:
        summary["arms"][arm] = {"config_hash": config_hash(cfg_for(arm, judges[0])),
                                "config": cfg_for(arm, judges[0]).__dict__ if real else "offline",
                                "judges": {}}
        for judge in judges:
            out = cache_dir / f"verdicts_{arm}_{judge.judge_id}_{primary_hash[:16]}.jsonl"
            verdicts = [json.loads(l) for l in out.read_text().splitlines()] if out.exists() else []
            got = {v["question_id"] for v in verdicts}
            if got != expected_qids:
                raise SystemExit(
                    f"REFUSING to aggregate: {arm}/{judge.judge_id} judged {len(got)}/"
                    f"{len(expected_qids)} questions — finish the run first.")
            verdicts.sort(key=lambda v: v["question_id"])
            summary["arms"][arm]["judges"][judge.judge_id] = aggregate_scores(verdicts, qtypes)
            arm_bools.setdefault(judge.judge_id, {})[arm] = [v["label"] for v in verdicts]

    if "a" in arms and "b" in arms:
        summary["key_experiment"] = {}
        for judge_id, byarm in arm_bools.items():
            point, lo, hi = paired_bootstrap_acc_delta(byarm["a"], byarm["b"])
            summary["key_experiment"][judge_id] = {
                "delta_pp": point, "ci95_pp": [lo, hi],
                "n_paired": len(byarm["a"]), "seed": BOOTSTRAP_SEED,
            }

    # spec: the lenient-vs-strict gap is a first-class result-file field on LoCoMo
    if benchmark == "locomo" and real:
        summary["judge_gap"] = {
            arm: round(
                summary["arms"][arm]["judges"]["locomo-lenient"]["overall"]
                - summary["arms"][arm]["judges"]["locomo-strict"]["overall"], 4)
            for arm in arms
        }

    summary["ingest_telemetry"] = manifest.get("namespaces", {})

    if real:
        results_dir.mkdir(parents=True, exist_ok=True)
        out_file = results_dir / f"{benchmark}_{slice}_{primary_hash[:16]}.json"
        summary["created_at"] = _time.strftime("%Y-%m-%dT%H:%M:%S%z")
        out_file.write_text(json.dumps(summary, indent=1, default=str))
        print(f"result file: {out_file}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 2 benchmark runner")
    ap.add_argument("--benchmark", required=True, choices=["longmemeval", "locomo"])
    ap.add_argument("--slice", default=None, help="ku|temporal|all (default per benchmark)")
    ap.add_argument("--variant", default="s", choices=["s", "oracle"],
                    help="longmemeval file variant")
    ap.add_argument("--arms", default="a,b")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--stage", default="all",
                    choices=["all", "ingest", "read", "judge", "aggregate"])
    ap.add_argument("--embedder", default=DEFAULT_EMBEDDER)
    ap.add_argument("--data-dir", default=str(_BENCH / ".phase2_cache" / "data"))
    ap.add_argument("--cache-root", default=str(_BENCH / ".phase2_cache"))
    ap.add_argument("--results-dir", default=str(_BENCH / "results" / "phase2"))
    args = ap.parse_args()

    slice_ = args.slice or ("ku" if args.benchmark == "longmemeval" else "temporal")
    ds_name = "locomo10" if args.benchmark == "locomo" else (
        "lme_oracle" if args.variant == "oracle" else "lme_s")
    dataset_path, sha = ensure_dataset(ds_name, Path(args.data_dir))
    from bet2_ablation import BackendUnavailable

    try:
        run_pipeline(benchmark=args.benchmark, slice=slice_, dataset_path=dataset_path,
                     dataset_sha256=sha, arms=args.arms.split(","), k=args.k,
                     real=args.real, cache_root=Path(args.cache_root),
                     results_dir=Path(args.results_dir), limit=args.limit,
                     embedder_name=args.embedder, stage=args.stage)
    except BackendUnavailable as exc:
        print(f"\nBACKEND UNAVAILABLE — no verdict (environment error, not FAIL):\n  {exc}",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Implementation notes for this step:
- `run_pipeline` passes `expect_counts=real` — real runs validate count anchors; fixture runs don't.
- The `--variant oracle` cache key is shared with `s` via `run_key` — this is wrong for real use; include the variant: change `run_key` to `f"{benchmark}_{slice}_{mode}_{dataset_path.stem}"`. Do this in this step (and the tests still pass — they use one dataset each).

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all PASS (including the four new e2e tests).

- [ ] **Step 5: Commit**

```bash
git add bench/phase2_eval.py tests/test_phase2_eval.py
git commit -m "feat(bench): phase2 staged runner — arms, resume, key experiment, result file"
```

---

### Task 11: Download real datasets, validate, pin sha256s

Runbook task (no new code). Requires network; LME `_s` is 264 MB.

- [ ] **Step 1: Download + validate all three datasets**

Run: `.venv/bin/python bench/phase2_ingest.py`
Expected output (shas will be recorded on first run):

```
locomo10: sha256 79fa87e9…  OK  (1540 scorable questions)
lme_oracle: sha256 <hex>  OK  (78 KU questions)
lme_s: sha256 <hex>  OK  (78 KU questions)
```

If a count anchor fails, STOP — the upstream dataset changed; re-verify against the spec's research notes before proceeding.

- [ ] **Step 2: Record the pinned shas in git** (the data itself stays out of git)

```bash
mkdir -p bench/results/phase2
.venv/bin/python - <<'EOF'
import json
from pathlib import Path
d = Path("bench/.phase2_cache/data")
pins = {p.name.removesuffix(".sha256"): p.read_text().strip() for p in d.glob("*.sha256")}
Path("bench/results/phase2/dataset_pins.json").write_text(json.dumps(pins, indent=1))
print(json.dumps(pins, indent=1))
EOF
git add bench/results/phase2/dataset_pins.json
git commit -m "chore(bench): pin phase2 dataset sha256s"
```

---

### Task 12: Real shakeout — LME oracle, 5 questions

Runbook task. Requires: `export OPENROUTER_API_KEY=…` in the shell, `ollama serve` running with `qwen2.5:3b` pulled. Cost: cents.

- [ ] **Step 1: Verify prerequisites**

Run: `ollama list | grep qwen2.5 && [ -n "$OPENROUTER_API_KEY" ] && echo READY`
Expected: `READY`. If the key is missing, ask the user to `export OPENROUTER_API_KEY=…` (or run via `! export …` in the session).

- [ ] **Step 2: Shakeout run (arm a only, oracle variant, 5 questions)**

Run: `.venv/bin/python bench/phase2_eval.py --benchmark longmemeval --variant oracle --slice ku --arms a --limit 5 --real`
Expected: config hash header printed; 5 questions ingest (real extraction — first run downloads GLiNER2 + Qwen3 + Ettin weights), read, judge; then a REFUSE on aggregate is NOT expected since all 5 are judged — but note `expect_counts=real` will fail on `--limit 5`… **`--limit` truncates units after loading, and count validation runs on the full file, so this passes.** A result file appears under `bench/results/phase2/`.
- Inspect `bench/.phase2_cache/longmemeval_ku_real_longmemeval_oracle/hypotheses_a_*.jsonl` — hypotheses should be plausible answers, not garbage.
- If any OpenRouter model slug 404s (e.g. `openai/gpt-4o-2024-08-06`), check the slug on openrouter.ai and update the judge's `model` constant + spec + commit — this is a config re-freeze.

- [ ] **Step 3: Delete the shakeout result file (it is a 5-question sample, not a number)**

```bash
rm bench/results/phase2/longmemeval_ku_*.json
```

- [ ] **Step 4: Fix anything the shakeout surfaced, commit fixes**

Any engine bug found here follows the spec's policy: fix engine → re-freeze → re-run shakeout. Commit each fix separately with a test where feasible.

---

### Task 13: LongMemEval KU slice — full real run + Key Experiment

Runbook task. Ingest is the long pole (~5–10 h estimate — calibrate first).

- [ ] **Step 1: Calibrate ingest on 10 questions**

Run: `time .venv/bin/python bench/phase2_eval.py --benchmark longmemeval --slice ku --stage ingest --limit 10 --real`
Multiply per-question wall time × 78 for the full estimate; report it to the user before continuing. (Cache persists — these 10 are not re-paid.)

- [ ] **Step 2: Full KU ingest (background, keep the machine awake)**

Run: `caffeinate -i .venv/bin/python bench/phase2_eval.py --benchmark longmemeval --slice ku --stage ingest --real` (run in background; it resumes if interrupted)
Expected: 78 namespaces `done` in `bench/.phase2_cache/longmemeval_ku_real_longmemeval_s_cleaned/manifest.json`.

- [ ] **Step 3: Read + judge + aggregate, all three arms**

```bash
.venv/bin/python bench/phase2_eval.py --benchmark longmemeval --slice ku --arms a,b,fc --real --stage read
.venv/bin/python bench/phase2_eval.py --benchmark longmemeval --slice ku --arms a,b,fc --real --stage judge
.venv/bin/python bench/phase2_eval.py --benchmark longmemeval --slice ku --arms a,b,fc --real --stage aggregate
```

Expected: result file `bench/results/phase2/longmemeval_ku_<hash16>.json` containing arm a/b/fc scores with per-type breakdown, the Key Experiment delta with CI, and ingest telemetry.

- [ ] **Step 4: Copy the per-question JSONLs beside the result file and commit**

```bash
cp bench/.phase2_cache/longmemeval_ku_real_longmemeval_s_cleaned/{hypotheses,verdicts}_*.jsonl bench/results/phase2/
git add bench/results/phase2/
git commit -m "results(bench): LongMemEval KU slice — arms A/B/FC, key experiment delta"
```

- [ ] **Step 5: Sanity-review the numbers before believing them**

Check: (a) arm A ≥ arm B is *expected* but not certain — either direction is a finding; (b) FC should be in the ballpark of published full-context KU numbers (~70–78% under gpt-4o-class readers; ours is gpt-4.1-mini + a stricter minimal prompt, so lower is plausible); (c) abstention questions (6) — inspect their hypotheses by hand; (d) telemetry: escalation rate <20%?, supersessions >0 on KU (if zero, the supersession machinery never fired — investigate before publishing). Record anomalies as findings in the commit message or a notes file.

---

### Task 14: LoCoMo temporal slice — full real run, dual judge

Runbook task. Ingest ~30–60 min (10 conversations, ~5.9k turns).

- [ ] **Step 1: Ingest**

Run: `caffeinate -i .venv/bin/python bench/phase2_eval.py --benchmark locomo --slice temporal --stage ingest --real`
Expected: 10 namespaces done in the manifest. Note: ingest is slice-independent (all turns); only questions differ by slice.

- [ ] **Step 2: Read + judge (lenient AND strict) + aggregate**

```bash
.venv/bin/python bench/phase2_eval.py --benchmark locomo --slice temporal --arms a,b,fc --real --stage read
.venv/bin/python bench/phase2_eval.py --benchmark locomo --slice temporal --arms a,b,fc --real --stage judge
.venv/bin/python bench/phase2_eval.py --benchmark locomo --slice temporal --arms a,b,fc --real --stage aggregate
```

Expected: result file `bench/results/phase2/locomo_temporal_<hash16>.json` with BOTH judges' scores per arm (never blended), key experiment per judge, telemetry.

- [ ] **Step 3: Commit results**

```bash
cp bench/.phase2_cache/locomo_temporal_real_locomo10/{hypotheses,verdicts}_*.jsonl bench/results/phase2/
git add bench/results/phase2/
git commit -m "results(bench): LoCoMo temporal slice — dual judge (lenient+strict), arms A/B/FC"
```

- [ ] **Step 4: Sanity-review**

Check the lenient−strict gap (expected: lenient ≫ strict — that gap is itself a publishable finding about judge leniency); compare lenient arm-a to published temporal numbers (Mem0 55.5, MemMachine 72.6, both gpt-4o-mini-judge configs — label configs when citing).

---

### Task 15: Documentation — record results and status

**Files:**
- Modify: `ARCHITECTURE.md` (Phase 2 row + a results section mirroring the BET-1/BET-2 sections)
- Modify: `docs/benchmarks.md` (add a "lean-memory measured results" section)
- Modify: `docs/phase2-eval-plan.md` (status line → done for slices 1–2)

- [ ] **Step 1: Update ARCHITECTURE.md**

Flip `Public benchmarks (LongMemEval / LoCoMo + frozen judge)` from `⬜` to `✅ (KU + temporal slices; full runs pending)` and add a results block styled like the BET-2 section: config hash, judge/backbone/provider, per-arm scores with CIs, the Key Experiment delta, engine bugs found (at minimum the sparse-arm `as_of` leak from Task 1), and the recency-decay-dead-at-3-years finding.

- [ ] **Step 2: Update docs/benchmarks.md**

Add our numbers with the full config line (backbone, judge, k, variant, config hash) and the explicit warning that they are comparable to other systems ONLY under matching configs.

- [ ] **Step 3: Commit**

```bash
git add ARCHITECTURE.md docs/benchmarks.md docs/phase2-eval-plan.md
git commit -m "docs: record Phase 2 slice results (LongMemEval KU, LoCoMo temporal)"
```

---

## Self-Review Notes

- **Spec coverage:** engine fix (T1), ingest adapters + cache + telemetry (T3–T6), reader + FC (T7), three judges + EvalConfig (T8), stats (T9), runner/arms/resume/result-file/refusal (T10), datasets (T11), shakeout (T12), KU + Key Experiment (T13), LoCoMo temporal dual-judge (T14), docs (T15). Deferred per spec: `as_of` reader policy on LoCoMo, k sweep, backbone sensitivity, `touch()`/`now` engine changes.
- **Known judgment calls encoded here:** `run_key` includes the dataset stem (variant isolation); `expect_counts=real` so fixtures skip anchors; offline aggregate returns a summary but writes no file; `--limit` slices loaded units, not the ingest loop.
- **Verify at implementation time:** `RecallBiasedRouter.cumulative_stats` — property vs method (Task 6 note); exact OpenRouter slug availability (Task 12 step 2).
