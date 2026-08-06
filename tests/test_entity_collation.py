"""Entity name collation — one real-world subject, one entity (WP15, issue #14).

`upsert_entity` resolves on a stored, Unicode-normalized key
(`entity.name_key` = NFC → casefold → whitespace collapse) instead of the raw
surface form under SQLite's BINARY collation. Two halves are pinned here:

  - the FOLD: case/whitespace/Unicode-form variants of a name land on ONE
    entity, so the WP11 restatement skip and contradiction resolution (both
    keyed on `(subject_id, predicate)`) actually apply to it;
  - the NON-FOLDS: punctuation and diacritics are NOT folded — `Yahoo!` is not
    `Yahoo` and `Café` is not `Cafe`. Folding those is transliteration, not
    case, and would kill genuinely distinct names.

`entity.name` keeps the FIRST-SEEN surface form verbatim — the fold lives only
in `name_key`, so nothing user-visible becomes lowercase.

The deliberate cost of the fold — genuinely case-distinct subjects merging — is
pinned at the bottom as the replacement known limit (it replaces WP11's
`test_entity_case_variant_splits_the_slot_known_limit`, deleted with this
change). All offline: stub embedder/extractor, no downloads.
"""

from __future__ import annotations

import pytest

from lean_memory import Memory
from lean_memory.normalize import normalize_text
from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Entity


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "ns.db", dim=768)
    yield s
    s.close()


@pytest.fixture
def mem(tmp_path):
    m = Memory(root=tmp_path)
    yield m
    m.close()


def _entity_names(mem: Memory, ns: str) -> list[str]:
    store = mem._store(ns)
    return [
        r["name"]
        for r in store._db.execute(
            "SELECT name FROM entity ORDER BY created_at, id"
        ).fetchall()
    ]


def _upsert(store: SqliteStore, name: str) -> Entity:
    return store.upsert_entity(Entity(namespace="ns", name=name, type=None))


# ── the key itself ──
def test_name_key_is_stored_and_folded(store):
    got = _upsert(store, "  Acme   Corp  ")
    row = store._db.execute(
        "SELECT name, name_key FROM entity WHERE id=?", (got.id,)
    ).fetchone()
    assert row["name"] == "  Acme   Corp  ", "surface form stored verbatim"
    assert row["name_key"] == "acme corp" == normalize_text("  Acme   Corp  ")


# ── the fold: variants resolve to ONE entity ──
@pytest.mark.parametrize(
    "first,second",
    [
        ("Acme", "ACME"),          # acronym styling
        ("Acme", "acme"),          # sentence-initial casing
        ("Café", "CAFÉ"),          # non-ASCII case — NOCASE misses this
        ("ЖУК", "жук"),            # Cyrillic case — NOCASE misses this
        ("Weiß", "WEISS"),         # full case fold (ß → ss)
        ("Acme  Corp", "Acme Corp"),  # whitespace collapse
        ("Caf\u00e9", "Cafe\u0301"),  # NFC vs NFD — same value, two encodings
    ],
)
def test_variants_resolve_to_one_entity(store, first, second):
    a = _upsert(store, first)
    b = _upsert(store, second)
    assert b.id == a.id, f"{second!r} must resolve to the {first!r} entity"
    assert b.name == first, "display form is the FIRST-seen surface form"
    assert store._db.execute("SELECT COUNT(*) c FROM entity").fetchone()["c"] == 1


# ── the non-folds: value-preserving, not lossy ──
@pytest.mark.parametrize(
    "first,second",
    [
        ("Acme", "Acme."),   # trailing punctuation is part of the name
        ("Yahoo!", "Yahoo"),  # ...and stripping it would kill 'Yahoo!'
        ("Café", "Cafe"),     # diacritic folding is transliteration, not case
    ],
)
def test_deliberate_non_merges_stay_distinct(store, first, second):
    a = _upsert(store, first)
    b = _upsert(store, second)
    assert b.id != a.id
    assert store._db.execute("SELECT COUNT(*) c FROM entity").fetchone()["c"] == 2


def test_type_still_separates_entities(store):
    """`type` stays in the key: the ingest path passes NULL today, but typed
    entities (WP4+) must keep Mercury/person and Mercury/planet apart."""
    person = store.upsert_entity(Entity(namespace="ns", name="Mercury", type="person"))
    planet = store.upsert_entity(Entity(namespace="ns", name="mercury", type="planet"))
    assert person.id != planet.id


def test_namespace_still_separates_entities(store):
    a = store.upsert_entity(Entity(namespace="ns", name="Acme", type=None))
    b = store.upsert_entity(Entity(namespace="other", name="acme", type=None))
    assert a.id != b.id


def test_legacy_split_rows_resolve_to_the_oldest_row(store):
    """The tie-break. A store written BEFORE v3 can hold two rows sharing one
    name_key (the migration backfills, it never heals — see
    test_schema_migration.py). The lookup's `ORDER BY created_at, id LIMIT 1`
    makes the winner deterministic: oldest row, so the store converges forward
    on the identity that already owns the most history."""
    rows = [
        ("ent-newer", "ACME", 1_700_000_005_000),
        ("ent-older", "Acme", 1_700_000_000_000),
    ]
    for eid, name, created in rows:
        store._db.execute(
            "INSERT INTO entity(id, namespace, name, name_key, type, summary, "
            "resolved_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (eid, "ns", name, normalize_text(name), None, None, None, created),
        )
    store._db.commit()

    got = _upsert(store, "AcMe")
    assert got.id == "ent-older"
    assert got.name == "Acme"
    assert store._db.execute("SELECT COUNT(*) c FROM entity").fetchone()["c"] == 2, (
        "resolving must not create a third row — and must not heal the split either"
    )


# ── end-to-end: the failure #14 reported ──
def test_case_variants_land_in_one_slot_end_to_end(mem):
    """The observable symptom: 'Acme'/'ACME'/'acme' in subject position used to
    make three entities and three co-valid latest facts (one company, three
    identities). Now: one entity, one fact — the restatement skip finally sees
    them as the same slot."""
    mem.add("ns", "Acme likes coffee.", t_ref=1_000)
    mem.add("ns", "ACME likes coffee.", t_ref=2_000)
    mem.add("ns", "acme likes coffee.", t_ref=3_000)

    assert _entity_names(mem, "ns") == ["Acme"], "one entity, first-seen display form"
    rows = mem._store("ns")._db.execute(
        "SELECT fact_text, is_latest FROM fact"
    ).fetchall()
    assert len(rows) == 1, [r["fact_text"] for r in rows]
    assert rows[0]["is_latest"] == 1


def test_unicode_variants_merge_end_to_end(mem):
    """The cases a SQLite NOCASE collation cannot reach — it is ASCII-only."""
    mem.add("cafe", "Café likes coffee.", t_ref=1_000)
    mem.add("cafe", "CAFÉ likes coffee.", t_ref=2_000)
    assert _entity_names(mem, "cafe") == ["Café"]

    mem.add("bug", "ЖУК likes coffee.", t_ref=1_000)
    mem.add("bug", "жук likes coffee.", t_ref=2_000)
    assert _entity_names(mem, "bug") == ["ЖУК"]


def test_first_person_case_variant_lands_in_the_user_slot(mem):
    """#14's literal example. Both halves of the fix are needed: the store-side
    key alone leaves 'i' as its own entity, because the extractor's first-person
    regex used to be case-SENSITIVE (see test_phase1_extraction.py)."""
    mem.add("ns", "I work at Acme.", t_ref=1_000)
    mem.add("ns", "i work at acme.", t_ref=2_000)

    assert _entity_names(mem, "ns") == ["user"]
    rows = mem._store("ns")._db.execute("SELECT fact_text FROM fact").fetchall()
    assert len(rows) == 1, [r["fact_text"] for r in rows]


# ── the replacement known limit ──
def test_case_distinct_subjects_merge_known_limit(mem):
    """KNOWN LIMIT, pinned (replaces WP11's
    `test_entity_case_variant_splits_the_slot_known_limit`, which pinned the
    opposite behavior and was deleted when this landed):

    Two GENUINELY distinct entities in one namespace whose names differ only by
    case fold to one entity. On a FUNCTIONAL predicate that retires the earlier
    fact from the current surface. This is the deliberate cost of the fold — a
    false SPLIT is silent and permanent, a false MERGE is visible in the
    supersession chain and nothing is deleted, so the asymmetry decides it.

    The recoverability half of that argument is executable, not rhetorical: the
    retired fact is still readable at `as_of`."""
    mem.add("ns", "Mercury lives in Rome.", t_ref=1_000)
    mem.add("ns", "mercury lives in thermometers.", t_ref=2_000)

    assert _entity_names(mem, "ns") == ["Mercury"], "one entity — the merge"

    rows = mem._store("ns")._db.execute(
        "SELECT fact_text, is_latest, valid_to, superseded_by FROM fact "
        "ORDER BY valid_at"
    ).fetchall()
    assert len(rows) == 2, "ADD-only: nothing is deleted by the merge"
    old, new = rows
    assert "Rome" in old["fact_text"]
    assert old["is_latest"] == 0, "the earlier fact left the current surface"
    assert old["valid_to"] == 2_000
    assert old["superseded_by"] is not None, "supersession chain records the merge"
    assert new["is_latest"] == 1

    # ...and it is still retrievable as-of the interval in which it was true.
    hits = mem.search(
        "ns", "Mercury lives in Rome", k=5, as_of=1_500, is_latest_only=False
    )
    assert any("Rome" in h.fact.fact_text for h in hits), (
        "the retired fact must stay readable via as_of — 'recoverable' is the "
        "whole reason this trade is acceptable"
    )
