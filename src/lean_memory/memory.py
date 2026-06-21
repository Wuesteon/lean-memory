"""`Memory` — the top-level facade. This is the public API for Phase 0.

    mem = Memory(root="./data")              # per-tenant files live under root/
    mem.add("ns1", "I work at Acme.", t_ref=...)
    hits = mem.search("ns1", "where does the user work?")

Per BET 4, each namespace gets its own SQLite file (write-isolation + brute-force
comfort). The Memory object owns a small cache of open per-namespace stores.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .embed.base import Embedder
from .embed.fake import FakeEmbedder
from .extract.contradiction import EXTENDS, SUPERSEDES, ContradictionResolver
from .extract.gliner_extractor import CandidateGenerator, StubCandidateGenerator
from .extract.llm_typer import StubTyper, TypedFact, Typer
from .extract.router import RecallBiasedRouter
from .extract.salience import score_salience
from .retrieve.rerank import IdentityReranker, Reranker
from .retrieve.retriever import Retriever
from .store.sqlite_store import SqliteStore
from .types import Entity, Episode, Fact, RetrievedFact, new_id, now_ms

# domain predicate the rules/stub passes use when none is guessed
_DEFAULT_PREDICATE = "about"

_SAFE_NS = re.compile(r"[^A-Za-z0-9_.-]")


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

        # Pass 3 — recall-biased router. known_entities lets it escalate cross-turn
        # references (an entity seen before but not introduced in this episode).
        known = self._known_entity_names(store, namespace)
        to_type, direct = self.router.route(candidates, known_entities=known)

        # Pass 4 — type the escalated residual with the LLM (stub offline); the direct
        # set is trivially explicit and typed cheaply (asserts, unless an inference cue).
        typed: list[TypedFact] = []
        if to_type:
            typed += self.typer.type_candidates(episode.raw, to_type, known_entities=list(known))
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
            # SUPERSEDES retires the matched fact; EXTENDS keeps both co-valid; ASSERTS
            # touches nothing else. Insert-new-first so the FK target exists.
            if decision.label == SUPERSEDES and decision.target is not None:
                store.supersede_fact(decision.target.id, fact.id, valid_to=fact.valid_at)
            written.append(fact.id)
        return written

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
        """Names of entities already seen in this namespace — the router uses these to
        escalate cross-turn references (the spec's hardest escalation signal)."""
        rows = store._db.execute(
            "SELECT name FROM entity WHERE namespace=?", (namespace,)
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
    ) -> list[RetrievedFact]:
        store = self._store(namespace)
        retriever = Retriever(store, self.embedder, self.reranker)
        return retriever.retrieve(
            query, k, as_of=as_of, is_latest_only=is_latest_only
        )

    def close(self) -> None:
        for s in self._stores.values():
            s.close()
        self._stores.clear()

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
