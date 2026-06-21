"""SqliteStore — the default Phase 0 store. One SQLite file per namespace.

Design-spec mapping:
  - sqlite-vec `vec0` virtual table holds the int8 768-dim + 256-dim coarse vectors
    (two-stage Matryoshka dense arm).
  - FTS5 holds `fact_text` for the BM25 sparse arm.
  - The relational `fact`/`entity`/`episode` tables hold the monotemporal spine.
  - Per BET 4: this object backs ONE namespace (one file), turning SQLite's
    single-writer limit into free write-isolation.

Quantization: vectors arrive L2-normalized float32 in [-1, 1]; we map to int8 by
scaling by 127 and rounding. vec0 does the distance math in int8 space.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
from sqlite_vec import serialize_float32

from ..types import Entity, Episode, Fact
from .base import Store
from .schema import SCHEMA_SQL


def _serialize(vec: np.ndarray) -> bytes:
    """L2-normalized float32 → vec0's float32 wire format.

    NOTE (Phase 0 decision): we store float32, not int8. The schema/spec target int8
    (size win, ~0.2pt quality cost per BET 1), but sqlite-vec 0.1.9's int8 *insert*
    path is broken ("expected int8, but float32 provided" even for valid int8 blobs),
    while the float32 path is solid. int8 is a documented future optimization to flip
    once the upstream bug is fixed — it does not affect spine correctness.
    """
    return serialize_float32(vec.astype(np.float32).tolist())


class SqliteStore(Store):
    def __init__(self, path: str | Path, *, dim: int = 768, coarse_dim: int = 256) -> None:
        self.path = str(path)
        self.dim = dim
        self.coarse_dim = coarse_dim
        self._db = self._connect()
        self._init_schema()

    # ── connection / schema ──
    def _connect(self) -> sqlite3.Connection:
        import sqlite_vec  # lazy: only needed when a real store is opened

        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        db.execute("PRAGMA journal_mode=WAL")  # better single-writer concurrency
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_schema(self) -> None:
        sql = SCHEMA_SQL.format(dim=self.dim, coarse_dim=self.coarse_dim)
        self._db.executescript(sql)
        self._db.commit()

    # ── provenance ──
    def add_episode(self, episode: Episode) -> None:
        self._db.execute(
            "INSERT INTO episode(id, namespace, raw, source, t_ref, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (episode.id, episode.namespace, episode.raw, episode.source,
             episode.t_ref, episode.created_at),
        )
        self._db.commit()

    # ── entities ──
    def upsert_entity(self, entity: Entity) -> Entity:
        row = self._db.execute(
            "SELECT * FROM entity WHERE namespace=? AND name=? AND IFNULL(type,'')=IFNULL(?,'')",
            (entity.namespace, entity.name, entity.type),
        ).fetchone()
        if row:
            return _row_to_entity(row)
        self._db.execute(
            "INSERT INTO entity(id, namespace, name, type, summary, resolved_id, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (entity.id, entity.namespace, entity.name, entity.type,
             entity.summary, entity.resolved_id, entity.created_at),
        )
        self._db.commit()
        return entity

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        row = self._db.execute("SELECT * FROM entity WHERE id=?", (entity_id,)).fetchone()
        return _row_to_entity(row) if row else None

    # ── facts ──
    def add_fact(self, fact: Fact, embedding: np.ndarray, embedding_256: np.ndarray) -> None:
        db = self._db
        db.execute(
            """INSERT INTO fact(
                 id, namespace, subject_id, predicate, object_id, object_literal, fact_text,
                 valid_at, valid_to, superseded_by, is_latest,
                 ingested_at, expired_at, invalidated_by,
                 confidence, salience, last_access, access_count, is_inference, tier,
                 episode_id, created_at)
               VALUES (?,?,?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?,?,?, ?,?)""",
            (fact.id, fact.namespace, fact.subject_id, fact.predicate, fact.object_id,
             fact.object_literal, fact.fact_text,
             fact.valid_at, fact.valid_to, fact.superseded_by, fact.is_latest,
             fact.ingested_at, fact.expired_at, fact.invalidated_by,
             fact.confidence, fact.salience, fact.last_access, fact.access_count,
             fact.is_inference, fact.tier,
             fact.episode_id, fact.created_at),
        )
        db.execute(
            "INSERT INTO fact_vec(fact_id, namespace, is_latest, tier, embedding, embedding_256) "
            "VALUES (?,?,?,?,?,?)",
            (fact.id, fact.namespace, fact.is_latest, fact.tier,
             _serialize(embedding), _serialize(embedding_256)),
        )
        db.execute(
            "INSERT INTO fact_fts(fact_id, fact_text) VALUES (?,?)",
            (fact.id, fact.fact_text),
        )
        db.commit()

    def supersede_fact(self, old_fact_id: str, new_fact_id: str, valid_to: int) -> None:
        db = self._db
        db.execute(
            "UPDATE fact SET superseded_by=?, valid_to=?, is_latest=0 WHERE id=?",
            (new_fact_id, valid_to, old_fact_id),
        )
        # keep the vec0 metadata filter column in sync so superseded facts drop out
        db.execute("UPDATE fact_vec SET is_latest=0 WHERE fact_id=?", (old_fact_id,))
        db.commit()

    def get_fact(self, fact_id: str) -> Optional[Fact]:
        row = self._db.execute("SELECT * FROM fact WHERE id=?", (fact_id,)).fetchone()
        return _row_to_fact(row) if row else None

    def find_latest_in_slot(self, subject_id: str, predicate: str) -> Sequence[Fact]:
        rows = self._db.execute(
            "SELECT * FROM fact WHERE subject_id=? AND predicate=? AND is_latest=1",
            (subject_id, predicate),
        ).fetchall()
        return [_row_to_fact(r) for r in rows]

    # ── retrieval primitives ──
    def dense_search(
        self,
        query_256: np.ndarray,
        query_768: np.ndarray,
        k: int,
        *,
        is_latest_only: bool = True,
        as_of: Optional[int] = None,
    ) -> list[tuple[str, float]]:
        """Two-stage Matryoshka: coarse 256-dim KNN over a wider pool, then re-score
        the survivors at full 768-dim. The coarse pool is k*COARSE_FACTOR wide so the
        cheaper first stage doesn't drop gold before the precise re-score."""
        COARSE_FACTOR = 8
        coarse_k = max(k * COARSE_FACTOR, k)

        latest_clause = "AND is_latest = 1" if is_latest_only else ""
        # Stage 1: coarse KNN. vec0 KNN must use a single MATCH + LIMIT.
        coarse_rows = self._db.execute(
            f"""SELECT fact_id, distance FROM fact_vec
                WHERE embedding_256 MATCH ? {latest_clause}
                ORDER BY distance LIMIT ?""",
            (_serialize(query_256), coarse_k),
        ).fetchall()
        if not coarse_rows:
            return []

        candidate_ids = [r["fact_id"] for r in coarse_rows]
        # Stage 2: re-score candidates at full 768-dim (exact distance, small set).
        # vec0 doesn't take an IN-list on KNN, so we read the full vectors back and
        # compute cosine here — exact, and the candidate set is tiny (coarse_k).
        placeholders = ",".join("?" * len(candidate_ids))
        vec_rows = self._db.execute(
            f"SELECT fact_id, embedding FROM fact_vec WHERE fact_id IN ({placeholders})",
            candidate_ids,
        ).fetchall()

        q = query_768.astype(np.float32)
        q = q / (np.linalg.norm(q) or 1.0)
        scored: list[tuple[str, float]] = []
        for vr in vec_rows:
            stored = np.frombuffer(vr["embedding"], dtype=np.float32)
            sn = np.linalg.norm(stored) or 1.0
            cos = float(np.dot(q, stored) / sn)
            scored.append((vr["fact_id"], 1.0 - cos))  # distance = 1 - cosine

        if as_of is not None:
            scored = self._apply_as_of(scored, as_of)

        scored.sort(key=lambda x: x[1])
        return scored[:k]

    def _apply_as_of(self, scored: list[tuple[str, float]], as_of: int) -> list[tuple[str, float]]:
        ids = [fid for fid, _ in scored]
        if not ids:
            return scored
        placeholders = ",".join("?" * len(ids))
        valid = {
            r["id"]
            for r in self._db.execute(
                f"""SELECT id FROM fact WHERE id IN ({placeholders})
                    AND valid_at <= ? AND (valid_to IS NULL OR valid_to > ?)""",
                (*ids, as_of, as_of),
            ).fetchall()
        }
        return [(fid, d) for fid, d in scored if fid in valid]

    def sparse_search(
        self, query_text: str, k: int, *, is_latest_only: bool = True
    ) -> list[tuple[str, float]]:
        # FTS5 BM25: lower bm25() is better, so we negate to "higher is better".
        rows = self._db.execute(
            """SELECT f.fact_id AS fact_id, bm25(fact_fts) AS score
               FROM fact_fts f
               WHERE fact_fts MATCH ?
               ORDER BY score LIMIT ?""",
            (_fts_query(query_text), k * (2 if is_latest_only else 1)),
        ).fetchall()
        out: list[tuple[str, float]] = []
        for r in rows:
            if is_latest_only:
                live = self._db.execute(
                    "SELECT is_latest FROM fact WHERE id=?", (r["fact_id"],)
                ).fetchone()
                if not live or not live["is_latest"]:
                    continue
            out.append((r["fact_id"], -float(r["score"])))
            if len(out) >= k:
                break
        return out

    def hydrate(self, fact_ids: Sequence[str]) -> dict[str, Fact]:
        if not fact_ids:
            return {}
        placeholders = ",".join("?" * len(fact_ids))
        rows = self._db.execute(
            f"SELECT * FROM fact WHERE id IN ({placeholders})", list(fact_ids)
        ).fetchall()
        return {r["id"]: _row_to_fact(r) for r in rows}

    def touch(self, fact_id: str, when_ms: int) -> None:
        self._db.execute(
            "UPDATE fact SET last_access=?, access_count=access_count+1 WHERE id=?",
            (when_ms, fact_id),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


# ── FTS query sanitization ──
def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query of bare terms (avoids syntax errors
    from punctuation/operators in user text)."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in text).split() if t]
    if not terms:
        return '""'
    return " OR ".join(terms)


# ── row → dataclass ──
def _row_to_entity(row: sqlite3.Row) -> Entity:
    return Entity(
        id=row["id"], namespace=row["namespace"], name=row["name"], type=row["type"],
        summary=row["summary"], resolved_id=row["resolved_id"], created_at=row["created_at"],
    )


def _row_to_fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=row["id"], namespace=row["namespace"], subject_id=row["subject_id"],
        predicate=row["predicate"], object_id=row["object_id"],
        object_literal=row["object_literal"], fact_text=row["fact_text"],
        valid_at=row["valid_at"], valid_to=row["valid_to"],
        superseded_by=row["superseded_by"], is_latest=row["is_latest"],
        ingested_at=row["ingested_at"], expired_at=row["expired_at"],
        invalidated_by=row["invalidated_by"], confidence=row["confidence"],
        salience=row["salience"], last_access=row["last_access"],
        access_count=row["access_count"], is_inference=row["is_inference"],
        tier=row["tier"], episode_id=row["episode_id"], created_at=row["created_at"],
    )
