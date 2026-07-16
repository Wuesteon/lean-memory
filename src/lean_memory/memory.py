"""`Memory` — the top-level facade. This is the public API for Phase 0.

    mem = Memory(root="./data")              # per-tenant files live under root/
    mem.add("ns1", "I work at Acme.", t_ref=...)
    hits = mem.search("ns1", "where does the user work?")

Per BET 4, each namespace gets its own SQLite file (write-isolation + brute-force
comfort). The Memory object owns a small cache of open per-namespace stores.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from .embed.base import Embedder
from .embed.fake import FakeEmbedder
from .extract.contradiction import SUPERSEDES, ContradictionResolver, is_multivalued
from .extract.gliner_extractor import CandidateGenerator, StubCandidateGenerator
from .extract.llm_typer import StubTyper, TypedFact, Typer, TyperError
from .extract.router import RecallBiasedRouter
from .extract.salience import score_salience
from .maintain import lifecycle
from .maintain.config import MaintenanceConfig
from .maintain.runner import MaintenanceRunner
from .maintain.summarize import Summarizer
from .retrieve.rerank import IdentityReranker, Reranker
from .retrieve.retriever import Retriever
from .store.sqlite_store import SqliteStore
from .types import Entity, Episode, Fact, RetrievedFact, new_id, now_ms

if TYPE_CHECKING:
    from .maintain.runner import RunReport

# busy_timeout for a dedicated maintenance connection (§7.1): the serving store stays
# at 1500 ms; a maintenance run (and the apply path) opens at 5000 so the 5000 budget
# reaches in-process callers without re-tuning the serving connection.
_MAINT_BUSY_TIMEOUT_MS = 5000

# domain predicate the rules/stub passes use when none is guessed
_DEFAULT_PREDICATE = "about"

# Cap on the known-entity names handed to the router/typer per add(). The set
# otherwise grows unboundedly with namespace age (conversational data creates
# entities every turn), inflating — and eventually silently truncating — the
# constrained-typing prompt. Most recent names win.
_KNOWN_ENTITIES_CAP = 100

_SAFE_NS = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_json(raw: Optional[str]) -> dict:
    """Parse a proposal payload defensively — a malformed/absent payload yields {}
    rather than crashing the review queue."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _proposal_subject_id(proposal: dict, payload: dict) -> str:
    """The subject entity a proposal groups under, kind-aware (§6.3 grouping):
      - summarize : payload['subject_id']
      - dedup_near: payload['slot']['subject_id']
      - evict     : no subject in the payload — group under '' (ungrouped bucket).
    Missing keys degrade to '' so the queue never crashes on a surprising payload."""
    kind = proposal.get("kind")
    if kind == "summarize":
        return payload.get("subject_id", "") or ""
    if kind == "dedup_near":
        slot = payload.get("slot") or {}
        return slot.get("subject_id", "") or ""
    return ""


class Memory:
    def __init__(
        self,
        root: str | Path = "./lm_data",
        *,
        embedder: Optional[Embedder] = None,
        reranker: Optional[Reranker] = None,
        generator: Optional[CandidateGenerator] = None,
        router: Optional[RecallBiasedRouter] = None,
        typer: Optional[Typer] = None,
        contradiction: Optional[ContradictionResolver] = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Every backend defaults to the OFFLINE stub so the engine runs with zero
        # downloads/servers. Swap in the real ones (SentenceTransformerEmbedder,
        # CrossEncoderReranker, Gliner2Generator, OllamaTyper) for production quality.
        self.embedder = embedder or FakeEmbedder()
        self.reranker = reranker or IdentityReranker()
        # Phase 1 hybrid-extraction pipeline (Pass 2 → 3 → 4 + contradiction):
        self.generator = generator or StubCandidateGenerator()
        self.router = router or RecallBiasedRouter()
        self.typer = typer or StubTyper()
        self.contradiction = contradiction or ContradictionResolver()
        self._stores: dict[str, SqliteStore] = {}

    # ── per-tenant store management (BET 4: one file per namespace) ──
    def _store(self, namespace: str) -> SqliteStore:
        if namespace not in self._stores:
            safe = _SAFE_NS.sub("_", namespace) or "default"
            path = self.root / f"{safe}.db"
            self._stores[namespace] = SqliteStore(
                path, dim=self.embedder.dim, coarse_dim=self.embedder.coarse_dim
            )
        return self._stores[namespace]

    # ── ingest (the Phase 1 hybrid pipeline) ──
    def add(
        self, namespace: str, text: str, *, t_ref: Optional[int] = None, source: str = "user"
    ) -> list[str]:
        """Ingest one message through the full hybrid pipeline (spec §5):

          Pass 2  generate over-generated candidates (rules/GLiNER2, high recall)
          Pass 3  recall-biased router → escalate the hard ones (logs escalation rate)
          Pass 4  LLM constrained typing of the residual; cheap-type the rest
          Pass 5  per fact: resolve entity → contradiction check → ADD-only persist

        Returns the ids of the facts written. Nothing is ever deleted.
        """
        t_ref = t_ref if t_ref is not None else now_ms()
        store = self._store(namespace)

        episode = Episode(namespace=namespace, raw=text, t_ref=t_ref, source=source)
        store.add_episode(episode)

        # Pass 2 — candidate generation (offline default: StubCandidateGenerator).
        candidates = self.generator.generate(episode)
        if not candidates:
            return []

        # Pass 3 — recall-biased router. known_entities is passed to the router/typer
        # as context only; it no longer drives escalation (the prior_entity trigger was
        # retired 2026-07 — see bench/results/calibration/README.md).
        known = self._known_entity_names(store, namespace)
        to_type, direct = self.router.route(candidates, known_entities=known)

        # Pass 4 — type the escalated residual with the LLM (stub offline); the direct
        # set is trivially explicit and typed cheaply (asserts, unless an inference cue).
        typed: list[TypedFact] = []
        if to_type:
            try:
                typed += self.typer.type_candidates(
                    episode.raw, to_type, known_entities=list(known)
                )
            except TyperError as exc:
                # TyperError == the real backend is unavailable (Ollama down /
                # package missing), the contract llm_typer documents for exactly
                # this fallback. Crashing would fail every add() for [llm] users
                # whose server isn't running; stub-type the batch instead.
                print(
                    f"[lean-memory] LLM typer unavailable ({exc}); "
                    "falling back to stub typing for this batch",
                    file=sys.stderr,
                )
                typed += StubTyper().type_candidates(
                    episode.raw, to_type, known_entities=list(known)
                )
        if direct:
            typed += StubTyper().type_candidates(episode.raw, direct, known_entities=list(known))

        # Pass 5 — resolve, contradiction-check, persist (ADD-only).
        written: list[str] = []
        for tf in typed:
            fact = self._build_fact(tf, namespace=namespace, episode_id=episode.id, store=store)

            # Contradiction → supersession over the (subject, predicate) slot.
            slot_latest = store.find_latest_in_slot(fact.subject_id, fact.predicate)
            decision = self.contradiction.classify(
                fact, slot_latest, self.embedder,
                # ambiguous cases can escalate to the same typer; offline stub is fine.
                llm_typer=None,
            )

            full, coarse = self.embedder.embed_with_coarse(fact.fact_text)
            store.add_fact(fact, full, coarse)
            # SUPERSEDES retires the slot per _apply_supersession; EXTENDS keeps both
            # co-valid; ASSERTS touches nothing else. Insert-new-first so the FK
            # target exists.
            self._apply_supersession(store, decision, fact, slot_latest)
            written.append(fact.id)
        return written

    @staticmethod
    def _apply_supersession(store, decision, fact, slot_latest) -> None:
        """Retire what a SUPERSEDES decision replaces (no-op for other labels).

        The resolver returns a single most-similar target, but a FUNCTIONAL slot
        (one current value) can hold N>1 co-valid latest facts when an earlier
        additive cue extended it — retiring only the target would leave stale
        contradictory facts is_latest=1, and current-state reads would return two
        employers. A replacement on a functional slot therefore retires EVERY
        latest fact in the slot. Multi-valued slots (likes/uses/...) keep the
        single-target behavior: the user's other co-valid values survive.

        Ingest hook 2 — SUMMARY-STALENESS CASCADE (§4.3): every supersede_fact
        returns the FULL closed set — its explicit target PLUS any duplicate-cascade-
        closed rows (§4.0). Any summary still is_latest=1 derived from ANY closed
        source is stale and must leave the default surface (is_latest=0,
        valid_to=new.valid_at, invalidated_by=new.id) — else the design ships a live
        contradiction (empirically demonstrated, §14). Feeding the RETURNED ids
        (not merely the loop's own targets) is load-bearing: a summary derived from
        a retired duplicate would otherwise never flip (rev-3 seam fix). No-op until
        fact_derivation has rows — the first-run path is unchanged.
        """
        if decision.label != SUPERSEDES or decision.target is None:
            return
        if is_multivalued(fact.predicate):
            targets = [decision.target]
        else:
            targets = [f for f in slot_latest if f.id != fact.id]

        closed_ids: list[str] = []
        for old in targets:
            closed_ids += store.supersede_fact(old.id, fact.id, valid_to=fact.valid_at)

        # Staleness cascade over the full closed set (explicit + cascade-closed).
        for summary_id in store.find_summaries_derived_from(closed_ids):
            store.invalidate_summary(
                summary_id, valid_to=fact.valid_at, invalidated_by=fact.id
            )

    def _build_fact(
        self, tf: TypedFact, *, namespace: str, episode_id: str, store: SqliteStore
    ) -> Fact:
        """Bind a TypedFact → a persistable Fact: resolve the subject entity, rate
        salience once (cached), carry the relation's is_inference flag."""
        subject = store.upsert_entity(Entity(namespace=namespace, name=tf.subject_name, type=None))
        salience = score_salience(
            tf.fact_text, source="extract", is_inference=bool(tf.is_inference)
        )
        ts = now_ms()
        return Fact(
            id=new_id(),
            namespace=namespace,
            subject_id=subject.id,
            predicate=tf.predicate or _DEFAULT_PREDICATE,
            object_literal=tf.object_literal,
            fact_text=tf.fact_text,
            valid_at=tf.valid_at,
            episode_id=episode_id,
            confidence=tf.confidence,
            salience=salience,
            is_inference=int(tf.is_inference),
            ingested_at=ts,
            created_at=ts,
        )

    def _known_entity_names(self, store: SqliteStore, namespace: str) -> set[str]:
        """Names of entities already seen in this namespace — passed to the router/typer
        as context only; they no longer drive escalation (the prior_entity trigger was
        retired 2026-07 — see bench/results/calibration/README.md).
        Capped to the most recent _KNOWN_ENTITIES_CAP names (ids are time-sortable)."""
        rows = store._db.execute(
            "SELECT name FROM entity WHERE namespace=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (namespace, _KNOWN_ENTITIES_CAP),
        ).fetchall()
        return {r["name"] for r in rows}

    # ── retrieve ──
    def search(
        self,
        namespace: str,
        query: str,
        k: int = 5,
        *,
        as_of: Optional[int] = None,
        is_latest_only: bool = True,
        now: Optional[int] = None,
        include_cold: bool = False,
    ) -> list[RetrievedFact]:
        """`now` (epoch ms) anchors the recency-decay term — pass the corpus's
        present when querying historical data, else the wall clock is used and
        recency is ≈0 for everything old (the term de-ranks nothing).

        `include_cold=True` opts a default latest-mode search out of the tier filter,
        so tier='cold' (maintenance-demoted) facts are searchable again (§8). Has no
        effect on as_of queries, which never filter tier."""
        store = self._store(namespace)
        retriever = Retriever(store, self.embedder, self.reranker)
        return retriever.retrieve(
            query, k, as_of=as_of, is_latest_only=is_latest_only, now=now,
            include_cold=include_cold,
        )

    # ── sleep-time maintenance façade (design spec §7.1, §8) ──
    def _namespace_path(self, namespace: str) -> Path:
        safe = _SAFE_NS.sub("_", namespace) or "default"
        return self.root / f"{safe}.db"

    def _maintenance_store(self, namespace: str) -> SqliteStore:
        """Open a DEDICATED maintenance SqliteStore on the namespace file with the
        5000 ms busy_timeout (§7.1) — this is how the 5000 budget reaches in-process
        callers WITHOUT re-tuning the serving store's connection (which stays at 1500).
        Caller MUST close it when the run/apply finishes."""
        return SqliteStore(
            self._namespace_path(namespace),
            dim=self.embedder.dim,
            coarse_dim=self.embedder.coarse_dim,
            busy_timeout_ms=_MAINT_BUSY_TIMEOUT_MS,
        )

    def maintain(
        self,
        namespace: str,
        *,
        config: Optional[MaintenanceConfig] = None,
        apply: bool = False,
        auto_only: bool = False,
        trigger: str = "cli",
        summarizer: Optional[Summarizer] = None,
    ) -> "RunReport":
        """Run one sleep-time maintenance pass on `namespace` (§7.1).

        Opens a dedicated maintenance store (5000 ms budget), drives the
        MaintenanceRunner, closes the store, and returns its RunReport. DRY-RUN by
        default: `apply=False` stages NOTHING and mutates nothing — it computes the
        full would-do report with zero writes and takes no lease (a dry-run writes
        nothing, so it needs no lease). `apply=True` claims the lease, runs the auto
        band, and stages proposals.

        `auto_only=True` (only meaningful with apply=True) runs ONLY the auto band and
        stages NOTHING — the `--auto-only` CLI/auto-spawn switch (§6.1). Default False
        preserves the full apply behavior (autos + proposals).
        """
        store = self._maintenance_store(namespace)
        try:
            runner = MaintenanceRunner(
                store, namespace, config=config, trigger=trigger,
                summarizer=summarizer,
            )
            return runner.run(dry_run=not apply, auto_only=auto_only)
        finally:
            store.close()

    def review_queue(
        self, namespace: str, *, kind: Optional[str] = None, limit: int = 20
    ) -> list[dict]:
        """Pending proposals with their evidence payloads, grouped by subject entity
        (§6.3). Lazily expires any pending proposal past its expires_at (status
        'expired', reason 'timeout') before returning — silence must not surface as a
        live proposal.

        Returns a list of {entity_id, entity_name, proposals: [...]} groups; each
        proposal carries its parsed `payload` alongside the row fields, so a caller
        (console / MCP) renders evidence without re-parsing JSON."""
        store = self._maintenance_store(namespace)
        try:
            now = now_ms()
            pending = store.list_proposals(
                namespace, status="pending", kind=kind, limit=limit * 4
            )
            live: list[dict] = []
            for p in pending:
                if p["expires_at"] < now:
                    store.expire_proposal(p["id"], "timeout")  # lazy timeout expiry
                    continue
                live.append(p)
                if len(live) >= limit:
                    break

            groups: dict[str, dict] = {}
            order: list[str] = []
            for p in live:
                payload = _safe_json(p.get("payload_json"))
                subject_id = _proposal_subject_id(p, payload)
                if subject_id not in groups:
                    entity = store.get_entity(subject_id) if subject_id else None
                    groups[subject_id] = {
                        "entity_id": subject_id,
                        "entity_name": entity.name if entity else None,
                        "proposals": [],
                    }
                    order.append(subject_id)
                item = dict(p)
                item["payload"] = payload
                groups[subject_id]["proposals"].append(item)
            return [groups[s] for s in order]
        finally:
            store.close()

    def decide(
        self,
        proposal_id: str,
        decision: str,
        *,
        namespace: str,
        edited_text: Optional[str] = None,
        decided_by: str = "console",
    ) -> dict:
        """Decide a proposal (approve | reject | edit | promote), applying on approval
        through the lifecycle (§5). Uses a dedicated 5000 ms store for the apply — the
        same rule as maintain() — and the server's embedder for the summary vector."""
        store = self._maintenance_store(namespace)
        try:
            return lifecycle.decide(
                store, self.embedder, proposal_id, decision,
                now=now_ms(), decided_by=decided_by, edited_text=edited_text,
            )
        finally:
            store.close()

    def promote(self, fact_id: str, *, namespace: str) -> dict:
        """Explicit promotion of a fact to the hot tier (§4.4). Explicit-only — there
        is no automatic promotion anywhere. Uses a dedicated 5000 ms store."""
        store = self._maintenance_store(namespace)
        try:
            return lifecycle.promote_fact(store, fact_id, now=now_ms())
        finally:
            store.close()

    def close(self) -> None:
        for s in self._stores.values():
            s.close()
        self._stores.clear()

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
