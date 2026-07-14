"""Deterministic fixture builder for the console read-path tests (spec §12).

Uses lean_memory.Memory with its default OFFLINE stub backends (no overrides =
FakeEmbedder + StubCandidateGenerator + StubTyper + ...). Output is committed
under console/tests/fixtures/data_root/ and is the acceptance criteria:
  - 2 namespaces (proj-alpha, proj-beta)
  - 2 episodes each
  - >=1 supersession chain of length >=2 (one retired + one latest)
  - >=1 entity with 2 facts
  - _events.db: 1 add event (superseded_count>0), 1 search event (full score
    payload), 1 event with payload.error
Rebuild + re-commit whenever the mirrored engine schema changes.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from lean_memory import Memory

from lean_memory_console.events import EventLog

FIXTURE_DIR = Path(__file__).resolve().parent / "data_root"

# Fixed epoch-ms reference times so the fixture is byte-stable across builds.
T0 = 1_700_000_000_000
DAY = 86_400_000


def _facts_of(mem: Memory, namespace: str) -> list[tuple]:
    path = mem.root / f"{namespace}.db"
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, subject_id, predicate, is_latest, superseded_by "
        "FROM fact ORDER BY created_at, id"
    ).fetchall()
    db.close()
    return rows


def build(target: Path = FIXTURE_DIR) -> Path:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    mem = Memory(root=str(target))
    try:
        # ── proj-alpha: 2 episodes; a supersession chain on a repeated slot,
        #    and an entity ("Ada") mentioned in both episodes (2+ facts). ──
        add1 = mem.add(
            "proj-alpha", "Ada works at Acme.", t_ref=T0, source="user"
        )
        add2 = mem.add(
            "proj-alpha", "Ada works at Globex now.", t_ref=T0 + DAY, source="user"
        )

        # ── proj-beta: 2 episodes, plain facts (no forced supersession). ──
        mem.add("proj-beta", "The project ships on Friday.", t_ref=T0, source="user")
        mem.add(
            "proj-beta", "The demo is scheduled for Monday.",
            t_ref=T0 + DAY, source="user",
        )
    finally:
        mem.close()

    # Determine which of proj-alpha's adds produced a supersession, so the add
    # event payload carries a real superseded id.
    alpha_rows = _facts_of(Memory(root=str(target)), "proj-alpha")
    retired = [r for r in alpha_rows if r["superseded_by"] is not None]
    latest_add_ids = list(add2)
    superseded_ids = [r["id"] for r in retired]

    # ── events sidecar: the three required rows (spec §12). ──
    log = EventLog(target)
    try:
        log.record(
            "proj-alpha",
            "add",
            7.5,
            {
                "episode_text_chars": len("Ada works at Globex now."),
                "source": "user",
                "t_ref": T0 + DAY,
                "fact_ids": latest_add_ids,
                "fact_count": len(latest_add_ids),
                "superseded_fact_ids": superseded_ids,
                "superseded_count": len(superseded_ids),
                "origin": "agent",
            },
        )
        log.record(
            "proj-alpha",
            "search",
            4.2,
            {
                "query": "where does Ada work?",
                "k": 5,
                "latest_only": True,
                "origin": "agent",
                "hits": [
                    {
                        "fact_id": latest_add_ids[0] if latest_add_ids else "f1",
                        "fact_text": "Ada works at Globex now.",
                        "final_score": 0.81,
                        "relevance": 0.88,
                        "recency": 0.72,
                        "importance": 0.40,
                        "dense_rank": 1,
                        "sparse_rank": 1,
                        "rrf_score": 0.032,
                    }
                ],
            },
        )
        log.record(
            "proj-alpha",
            "search",
            0.0,
            {
                "query": "bad query",
                "k": 5,
                "latest_only": True,
                "origin": "agent",
                "error": "engine raised: simulated failure",
            },
        )
    finally:
        log.close()

    assert superseded_ids, "fixture must contain >=1 supersession chain"
    return target


if __name__ == "__main__":
    out = build()
    print(f"fixture built at {out}")
