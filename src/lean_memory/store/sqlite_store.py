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

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Sequence

import numpy as np
from sqlite_vec import serialize_float32

from ..types import Entity, Episode, Fact, new_id
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
    def __init__(
        self,
        path: str | Path,
        *,
        dim: int = 768,
        coarse_dim: int = 256,
        busy_timeout_ms: int = 1500,
    ) -> None:
        self.path = str(path)
        self.dim = dim
        self.coarse_dim = coarse_dim
        # Lock budget (§7.1): 1500 ms serving default; maintenance opens with 5000.
        self.busy_timeout_ms = busy_timeout_ms
        # When True, per-call commits are suppressed — set only inside batch().
        self._in_batch = False
        self._db = self._connect()
        self._init_schema()

    # ── connection / schema ──
    def _connect(self) -> sqlite3.Connection:
        import sqlite_vec  # lazy: only needed when a real store is opened

        # check_same_thread=False: mcp SDK 2.0 runs sync tool handlers in
        # anyio worker threads (1.x ran them inline on the event loop), so the
        # serving connection is touched from a different thread per call.
        # Sequential cross-thread use is safe — CPython's sqlite3 ships
        # threadsafety=3 (serialized) — and MCP stdio traffic is serial;
        # nothing here enables CONCURRENT cross-thread access.
        db = sqlite3.connect(self.path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        db.execute("PRAGMA journal_mode=WAL")  # better single-writer concurrency
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        return db

    # ── commit routing (single choke point for batch suppression) ──
    def _commit(self) -> None:
        """Every mutator commits through here so batch() can suppress it in ONE place.
        Inside a batch window the commit is deferred to the single COMMIT at exit."""
        if not self._in_batch:
            self._db.commit()

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Atomic unit-of-work (§4.0): BEGIN IMMEDIATE, suppress per-call commits, one
        COMMIT at exit, ROLLBACK on exception. execute() only — executescript() would
        implicitly commit and break atomicity. Not re-entrant (a nested batch is a bug)."""
        if self._in_batch:
            raise RuntimeError("batch() is not re-entrant")
        self._db.execute("BEGIN IMMEDIATE")
        self._in_batch = True
        try:
            yield
        except BaseException:
            self._in_batch = False
            self._db.rollback()
            raise
        else:
            self._in_batch = False
            self._db.commit()

    def _init_schema(self) -> None:
        self._check_existing_dims()
        # Always-run blob: every statement is CREATE ... IF NOT EXISTS, so it is a
        # no-op on an already-created DB (fresh or migrated) and creates the v2
        # tables/indexes on any DB that lacks them.
        sql = SCHEMA_SQL.format(dim=self.dim, coarse_dim=self.coarse_dim)
        self._db.executescript(sql)

        # Schema-version stamp — the migration anchor for future releases.
        # Version 1 == the 0.1.x layout; pre-stamp files (0.1.0–0.1.2, version 0)
        # have an identical spine and are treated as version 1. Never write over a
        # NEWER release's stamp.
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            version = 1
            self._db.execute("PRAGMA user_version = 1")

        # ── Versioned migrations. Each branch runs the NON-idempotent DDL for its
        # version exactly once, keyed off user_version. A fresh DB is version 1
        # here (just stamped above), so it flows through the same `< 2` branch and
        # gains record_kind via the SAME ALTER — there is no separate fresh path.
        # ADD COLUMN is not idempotent (raises 'duplicate column name' on reopen),
        # so it MUST live here and never in the always-run blob.
        if version < 2:
            self._db.execute(
                "ALTER TABLE fact ADD COLUMN record_kind TEXT NOT NULL "
                "DEFAULT 'fact'"  # 'fact'|'summary'
            )
            self._db.execute("PRAGMA user_version = 2")
            version = 2

        self._db.commit()

    def _check_existing_dims(self) -> None:
        """Refuse to open a store whose vec0 table was created for a different embedder.

        The vec0 DDL bakes the dim in at creation and CREATE ... IF NOT EXISTS keeps
        the old table on reopen, so a dim mismatch (e.g. 768-dim offline stub → the
        1024-dim Qwen default after installing [models]) would otherwise surface deep
        in the pipeline as an opaque insert/shape error against a half-usable DB.
        """
        row = self._db.execute(
            "SELECT sql FROM sqlite_master WHERE name='fact_vec'"
        ).fetchone()
        if row is None or not row["sql"]:
            return  # fresh file — schema created below with the current dims
        stored = {
            m.group(1): int(m.group(2))
            for m in re.finditer(r"(embedding(?:_256)?)\s+FLOAT\[(\d+)\]", row["sql"])
        }
        expected = {"embedding": self.dim, "embedding_256": self.coarse_dim}
        for column, want in expected.items():
            have = stored.get(column)
            if have is not None and have != want:
                self._db.close()
                raise ValueError(
                    f"embedder dimension mismatch: {self.path} was created with "
                    f"{column} FLOAT[{have}], but the current embedder produces "
                    f"{want}-dim vectors for that column. Either keep using the "
                    f"embedder this namespace was created with, or delete the "
                    f"namespace file (plus its -wal/-shm siblings) to rebuild it "
                    f"with the new embedder — its facts will need re-adding."
                )

    # ── provenance ──
    def add_episode(self, episode: Episode) -> None:
        self._db.execute(
            "INSERT INTO episode(id, namespace, raw, source, t_ref, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (episode.id, episode.namespace, episode.raw, episode.source,
             episode.t_ref, episode.created_at),
        )
        self._commit()

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
        self._commit()
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
                 record_kind, episode_id, created_at)
               VALUES (?,?,?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?,?,?, ?,?,?)""",
            (fact.id, fact.namespace, fact.subject_id, fact.predicate, fact.object_id,
             fact.object_literal, fact.fact_text,
             fact.valid_at, fact.valid_to, fact.superseded_by, fact.is_latest,
             fact.ingested_at, fact.expired_at, fact.invalidated_by,
             fact.confidence, fact.salience, fact.last_access, fact.access_count,
             fact.is_inference, fact.tier,
             fact.record_kind, fact.episode_id, fact.created_at),
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
        self._commit()

    def supersede_fact(
        self, old_fact_id: str, new_fact_id: str, valid_to: int
    ) -> list[str]:
        """Close `old` at world-time `valid_to`, cascade-close its OPEN retired
        duplicates at the same V, and return the full closed-id list.

        Ingest hook 1 — DUPLICATE-CASCADE (§4.0): retire_duplicate leaves a
        duplicate with superseded_by=survivor but valid_to NULL (verb (c), as-of
        safe at dedup time). Such an open duplicate is invisible to
        find_latest_in_slot, so ordinary ingest supersession never closes it —
        after the survivor is superseded it would resurrect as a permanently-open
        interval on the pure as-of surface (empirically demonstrated wrong answer,
        §14). Closing `WHERE superseded_by=old AND valid_to IS NULL` at the same V
        restores commutation. A SINGLE level suffices because retire_duplicate's
        chain invariant keeps every open duplicate pointing DIRECTLY at the live
        survivor (§4.0) — no recursion. Whole thing is ONE transaction.

        RETURNS [old_id] + cascade-closed ids so the summary-staleness cascade
        (§4.3) keys on every closed row, not just the explicit target — the
        cascade-closed ids are collected by SELECT before the UPDATE (rev-3 seam
        fix). No-op cascade until retire_duplicate has produced such rows.
        """
        db = self._db
        db.execute(
            "UPDATE fact SET superseded_by=?, valid_to=?, is_latest=0 WHERE id=?",
            (new_fact_id, valid_to, old_fact_id),
        )
        # keep the vec0 metadata filter column in sync so superseded facts drop out
        db.execute("UPDATE fact_vec SET is_latest=0 WHERE fact_id=?", (old_fact_id,))
        # Duplicate-cascade: collect the open retired duplicates FIRST (for the
        # returned set), then close them at the same V — same transaction.
        cascade_ids = [
            r["id"]
            for r in db.execute(
                "SELECT id FROM fact WHERE superseded_by=? AND valid_to IS NULL",
                (old_fact_id,),
            ).fetchall()
        ]
        if cascade_ids:
            db.execute(
                "UPDATE fact SET valid_to=? WHERE superseded_by=? AND valid_to IS NULL",
                (valid_to, old_fact_id),
            )
        self._commit()
        return [old_fact_id, *cascade_ids]

    # ── derivation lineage + staleness cascade support (schema v2; §4.3) ──
    def add_derivation(
        self, summary_id: str, source_id: str, run_id: str, created_at: int
    ) -> None:
        """Record one summary←source lineage edge (fact_derivation). The staleness
        cascade reads these via ix_derivation_source to find summaries to invalidate
        when a source is closed by ingest (§4.3). INSERT OR IGNORE keeps the
        (summary_id, source_id) PK idempotent."""
        self._db.execute(
            "INSERT OR IGNORE INTO fact_derivation(summary_id, source_id, run_id, created_at) "
            "VALUES (?,?,?,?)",
            (summary_id, source_id, run_id, created_at),
        )
        self._commit()

    def find_summaries_derived_from(self, source_ids: Sequence[str]) -> list[str]:
        """DISTINCT still-latest summary ids derived from any of `source_ids` — the
        staleness cascade's lookup (§4.3), served by ix_derivation_source. Restricted
        to is_latest=1 so the caller never re-invalidates an already-retired summary."""
        ids = list(source_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self._db.execute(
            f"""SELECT DISTINCT d.summary_id AS summary_id
                FROM fact_derivation d JOIN fact f ON f.id = d.summary_id
                WHERE d.source_id IN ({placeholders}) AND f.is_latest = 1""",
            ids,
        ).fetchall()
        return [r["summary_id"] for r in rows]

    def invalidate_summary(
        self, summary_id: str, valid_to: int, invalidated_by: str
    ) -> None:
        """Ingest hook 2 write — retire a summary stale-invalidated by live ingest
        (§4.3): is_latest=0, valid_to, invalidated_by on fact + is_latest=0 mirror on
        fact_vec, one txn. Scoped to is_latest=1 so a re-fire is a no-op. As-of
        windows [t_a, valid_to) still show it — accurate: it was the believed state."""
        db = self._db
        db.execute(
            "UPDATE fact SET is_latest=0, valid_to=?, invalidated_by=? "
            "WHERE id=? AND is_latest=1",
            (valid_to, invalidated_by, summary_id),
        )
        db.execute("UPDATE fact_vec SET is_latest=0 WHERE fact_id=?", (summary_id,))
        self._commit()

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
        include_cold: bool = False,
    ) -> list[tuple[str, float]]:
        """Two-stage Matryoshka: coarse 256-dim KNN over a wider pool, then re-score
        the survivors at full 768-dim. The coarse pool is k*COARSE_FACTOR wide so the
        cheaper first stage doesn't drop gold before the precise re-score."""
        COARSE_FACTOR = 8
        coarse_k = max(k * COARSE_FACTOR, k)

        latest_clause = "AND is_latest = 1" if is_latest_only else ""
        # Tier filter (§8): drop cold facts from the default hot surface ONLY — the
        # vec0 'tier' metadata column ANDed into the KNN WHERE. Applied EXCLUSIVELY in
        # default latest-mode: as_of queries NEVER filter tier (historical reads see
        # everything), and include_cold=True opts out explicitly. Every existing row is
        # tier='hot', so this clause is byte-identical for anyone who never runs
        # maintenance (regression pin).
        tier_clause = (
            "AND tier = 'hot'"
            if (is_latest_only and as_of is None and not include_cold)
            else ""
        )
        # Stage 1: coarse KNN. vec0 KNN must use a single MATCH + LIMIT.
        coarse_rows = self._db.execute(
            f"""SELECT fact_id, distance FROM fact_vec
                WHERE embedding_256 MATCH ? {latest_clause} {tier_clause}
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
        self, query_text: str, k: int, *, is_latest_only: bool = True,
        as_of: Optional[int] = None, include_cold: bool = False,
    ) -> list[tuple[str, float]]:
        # FTS5 BM25: lower bm25() is better, so we negate to "higher is better".
        # Tier filter (§8): drop cold facts in default latest-mode ONLY — same
        # condition as the dense arm, applied in the existing per-row recheck so both
        # arms read the single flag set_tier writes. as_of NEVER filters tier;
        # include_cold=True opts out. Byte-identical when nothing is cold.
        filter_tier = is_latest_only and as_of is None and not include_cold
        needs_row_check = is_latest_only or as_of is not None or filter_tier
        try:
            rows = self._db.execute(
                """SELECT f.fact_id AS fact_id, bm25(fact_fts) AS score
                   FROM fact_fts f
                   WHERE fact_fts MATCH ?
                   ORDER BY score LIMIT ?""",
                (_fts_query(query_text), k * (2 if needs_row_check else 1)),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # The sparse arm is best-effort: _fts_query quotes every term, but if a
            # malformed MATCH ever slips through again, degrade to no sparse hits
            # rather than failing the whole search (the dense arm still serves).
            # Only syntax errors qualify — anything else ('database is locked',
            # corruption) is a real store error and must propagate.
            if "syntax error" not in str(exc):
                raise
            return []
        out: list[tuple[str, float]] = []
        for r in rows:
            if needs_row_check:
                row = self._db.execute(
                    "SELECT is_latest, valid_at, valid_to, tier FROM fact WHERE id=?",
                    (r["fact_id"],),
                ).fetchone()
                if not row:
                    continue
                if is_latest_only and not row["is_latest"]:
                    continue
                if filter_tier and row["tier"] != "hot":
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
        self._commit()

    # ── maintenance mutation surface (sleep-time job; design spec §4.0) ──
    def retire_duplicate(self, loser_id: str, survivor_id: str) -> None:
        """Retire an exact duplicate onto its survivor (two-surface, one txn).

        Flips loser is_latest=0 + superseded_by=survivor on fact AND fact_vec;
        valid_to is UNTOUCHED (verb (c) — the is_latest_only=False as-of surface is
        unchanged). Maintains the chain invariant that every OPEN retired duplicate
        (superseded_by set, valid_to NULL) points DIRECTLY at an is_latest=1 survivor:
          (i)  resolve `survivor_id` to its live canonical (depth 1) — a retired
               survivor arg would otherwise leave the loser pointing at a non-latest row;
          (ii) re-point existing open losers of `loser_id` at that canonical, so a
               transitive B→A→D retirement never resurrects B when D is later superseded
               (rev-3 blocker, §4.0 / §10.2).
        """
        if loser_id == survivor_id:
            raise ValueError("retire_duplicate: survivor must differ from loser")
        db = self._db
        # (i) resolve the survivor arg to its live canonical (depth 1).
        row = db.execute(
            "SELECT superseded_by, is_latest FROM fact WHERE id=?", (survivor_id,)
        ).fetchone()
        if row is not None and not row["is_latest"] and row["superseded_by"]:
            survivor_id = row["superseded_by"]
        if loser_id == survivor_id:  # resolution collapsed onto the loser — reject
            raise ValueError("retire_duplicate: survivor resolves to the loser")

        db.execute(
            "UPDATE fact SET superseded_by=?, is_latest=0 WHERE id=?",
            (survivor_id, loser_id),
        )
        db.execute("UPDATE fact_vec SET is_latest=0 WHERE fact_id=?", (loser_id,))
        # (ii) re-point existing OPEN losers of the loser directly at the survivor.
        db.execute(
            "UPDATE fact SET superseded_by=? WHERE superseded_by=? AND valid_to IS NULL",
            (survivor_id, loser_id),
        )
        self._commit()

    def set_tier(self, fact_id: str, tier: str) -> None:
        """Move a fact between the hot/cold tiers — fact.tier + fact_vec.tier, one txn."""
        db = self._db
        db.execute("UPDATE fact SET tier=? WHERE id=?", (tier, fact_id))
        db.execute("UPDATE fact_vec SET tier=? WHERE fact_id=?", (tier, fact_id))
        self._commit()

    def merge_usage_stats(
        self, fact_id: str, access_count: int, last_access: Optional[int]
    ) -> None:
        """DEDUP-EXACT survivor-merge write (§4.1): OVERWRITE the survivor's
        usage stats with the cluster-merged values (access_count summed over the
        cluster, last_access = max coalesce(last_access, valid_at)). Plain UPDATE
        on fact — no vec/FTS surface. Commits through _commit so it participates
        in an open batch() window."""
        self._db.execute(
            "UPDATE fact SET access_count=?, last_access=? WHERE id=?",
            (access_count, last_access, fact_id),
        )
        self._commit()

    def get_embedding(self, fact_id: str) -> Optional[np.ndarray]:
        """Read a fact's stored full-dim vector back (no re-embed). None if absent.

        Mirrors add_fact's serialization: vec0 holds float32 blobs, read via
        np.frombuffer — an exact round-trip."""
        row = self._db.execute(
            "SELECT embedding FROM fact_vec WHERE fact_id=?", (fact_id,)
        ).fetchone()
        if row is None or row["embedding"] is None:
            return None
        return np.frombuffer(row["embedding"], dtype=np.float32)

    def iter_latest_facts(self, after_id: Optional[str] = None) -> Iterator[Fact]:
        """Id high-water scan over is_latest=1 rows (evict/summarize candidates).
        Ids are time-sortable (types.new_id), so id order is ingestion order."""
        if after_id is None:
            rows = self._db.execute(
                "SELECT * FROM fact WHERE is_latest=1 ORDER BY id"
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM fact WHERE is_latest=1 AND id > ? ORDER BY id",
                (after_id,),
            ).fetchall()
        for r in rows:
            yield _row_to_fact(r)

    def iter_slots_touched_since(self, cursor_id: str) -> Iterator[tuple[str, str]]:
        """DISTINCT (subject_id, predicate) slots that GAINED a member since the cursor.

        Keyed on any fact with id > cursor — so a duplicate landing on a long-quiet
        slot resurfaces it (the verified cursor gap a bare latest-scan would miss).
        Slot transforms then read the full slot via find_latest_in_slot."""
        rows = self._db.execute(
            "SELECT DISTINCT subject_id, predicate FROM fact WHERE id > ? "
            "ORDER BY subject_id, predicate",
            (cursor_id,),
        ).fetchall()
        for r in rows:
            yield (r["subject_id"], r["predicate"])

    # ── maintenance ledger + proposal CRUD (schema v2; design spec §4.0/§5) ──
    # Pure row CRUD. No decide/apply logic lives here (that is the proposal
    # lifecycle, a later task) — these just read and write the ledger tables.
    def create_run(
        self, namespace: str, trigger: str, started_at: int, config_hash: Optional[str]
    ) -> str:
        run_id = new_id()
        # The INSERT is the atomic lease claim: ux_run_live (partial-unique on
        # namespace WHERE status='running') makes a second live run for the same
        # namespace raise sqlite3.IntegrityError, not a silent second row.
        self._db.execute(
            "INSERT INTO maintenance_run("
            "id, namespace, started_at, finished_at, heartbeat_at, trigger, "
            "cursor_id, config_hash, stats_json, status) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'running')",
            (run_id, namespace, started_at, None, started_at, trigger,
             None, config_hash, None),
        )
        self._commit()
        return run_id

    def heartbeat_run(self, run_id: str, at: int) -> None:
        self._db.execute(
            "UPDATE maintenance_run SET heartbeat_at=? WHERE id=?", (at, run_id)
        )
        self._commit()

    def finish_run(
        self,
        run_id: str,
        status: str,
        finished_at: int,
        stats_json: Optional[str],
        cursor_id: Optional[str],
    ) -> None:
        # Clearing status='running' releases the ux_run_live lease.
        self._db.execute(
            "UPDATE maintenance_run SET status=?, finished_at=?, stats_json=?, "
            "cursor_id=? WHERE id=?",
            (status, finished_at, stats_json, cursor_id, run_id),
        )
        self._commit()

    def get_live_run(self, namespace: str) -> Optional[dict]:
        row = self._db.execute(
            "SELECT * FROM maintenance_run WHERE namespace=? AND status='running'",
            (namespace,),
        ).fetchone()
        return dict(row) if row else None

    def last_finished_run(self, namespace: str) -> Optional[dict]:
        # Most recent status='ok' run — the runner's previous-cursor + last-run-age
        # source (§6.6). 'aborted'/'running' rows are excluded so a no-work run never
        # advances the cursor another run reasons from. Pure read.
        row = self._db.execute(
            "SELECT * FROM maintenance_run WHERE namespace=? AND status='ok' "
            "ORDER BY finished_at DESC, id DESC LIMIT 1",
            (namespace,),
        ).fetchone()
        return dict(row) if row else None

    def stage_proposal(
        self,
        run_id: str,
        namespace: str,
        kind: str,
        payload_json: str,
        created_at: int,
        expires_at: int,
        evidence_backend: Optional[str] = None,
    ) -> str:
        proposal_id = new_id()
        self._db.execute(
            "INSERT INTO maintenance_proposal("
            "id, run_id, namespace, kind, payload_json, status, "
            "created_at, expires_at, evidence_backend) "
            "VALUES (?,?,?,?,?, 'pending', ?,?,?)",
            (proposal_id, run_id, namespace, kind, payload_json,
             created_at, expires_at, evidence_backend),
        )
        self._commit()
        return proposal_id

    def get_proposal(self, proposal_id: str) -> Optional[dict]:
        row = self._db.execute(
            "SELECT * FROM maintenance_proposal WHERE id=?", (proposal_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── proposal decide (CAS) + apply-time stamps (design spec §5) ──
    def cas_decide_proposal(
        self,
        proposal_id: str,
        status: str,
        decided_at: int,
        decided_by: str,
        edited_text: Optional[str] = None,
    ) -> int:
        """Compare-and-swap a pending proposal into a decided state (§5).

        The EXACT §5 CAS: only flips a row still `status='pending'`, so two concurrent
        writers race for the single UPDATE and exactly one wins. Returns the rows
        updated — 1 == this caller won the decision, 0 == the proposal was already
        decided (report, never re-apply). Commits through _commit so it participates
        in an open approve-and-apply batch() window (§5)."""
        cur = self._db.execute(
            "UPDATE maintenance_proposal SET status=?, decided_at=?, decided_by=?, "
            "edited_text=? WHERE id=? AND status='pending'",
            (status, decided_at, decided_by, edited_text, proposal_id),
        )
        self._commit()
        return cur.rowcount

    def mark_proposal_applied(self, proposal_id: str, applied_at: int) -> None:
        """Stamp applied_at on a proposal after its verbs committed (§5). Plain UPDATE
        keyed on id — the apply happens inside the same batch() as the CAS, so the
        stamp co-commits with the spine writes."""
        self._db.execute(
            "UPDATE maintenance_proposal SET applied_at=? WHERE id=?",
            (applied_at, proposal_id),
        )
        self._commit()

    def expire_proposal(self, proposal_id: str, expiry_reason: str) -> int:
        """Expire a still-pending proposal (§5): status='expired' + expiry_reason
        ('timeout' | 'stale_target'), CAS on status='pending' so it can never clobber
        an already-decided row. Returns rows updated (1 == expired here, 0 == already
        decided). Commits through _commit — the stale-target expiry co-commits with an
        open batch() window so it is the write that survives while the spine stays
        untouched (§5)."""
        cur = self._db.execute(
            "UPDATE maintenance_proposal SET status='expired', expiry_reason=? "
            "WHERE id=? AND status='pending'",
            (expiry_reason, proposal_id),
        )
        self._commit()
        return cur.rowcount

    def list_proposals(
        self,
        namespace: str,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        where = ["namespace=?"]
        args: list = [namespace]
        if status is not None:
            where.append("status=?")
            args.append(status)
        if kind is not None:
            where.append("kind=?")
            args.append(kind)
        clause = " AND ".join(where)
        rows = self._db.execute(
            f"SELECT * FROM maintenance_proposal WHERE {clause} "
            f"ORDER BY created_at DESC, id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._db.close()


# ── FTS query sanitization ──
def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query of QUOTED terms.

    Quoting matters: FTS5 treats the bare uppercase tokens AND/OR/NOT/NEAR as
    operators, so 'coffee AND tea' would otherwise become the malformed
    'coffee OR AND OR tea' and raise. A quoted term is a string literal — inert
    as an operator, still matched case-insensitively by the tokenizer. Terms are
    alnum-only after the scrub, so no embedded quote can break out."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in text).split() if t]
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"' for t in terms)


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
        tier=row["tier"], record_kind=row["record_kind"],
        episode_id=row["episode_id"], created_at=row["created_at"],
    )
