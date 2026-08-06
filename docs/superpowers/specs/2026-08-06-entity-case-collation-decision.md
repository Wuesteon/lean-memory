# Entity Name Collation — Decision

Date: 2026-08-06 (**rev 2**, post-review). Status: **decision doc, awaiting
maintainer sign-off** — docs-only, no code changed. Resolves the open follow-up
"Entity case-collation policy"
([#14](https://github.com/Wuesteon/lean-memory/issues/14)), recorded as WP11's
pinned known limit.

All `file:line` references verified against the working tree on `main` at
v0.2.3 (8ee6108). Every empirical claim below was measured with throwaway
scratch scripts and monkeypatched pytest plugins against the project venvs
(Python 3.13.7 / SQLite 3.53.4, plus `console/.venv` for the cross-package row);
the commands and their raw output are in §6.

Rev 2 corrects four things a review caught: the suite-scale blast radius was
measured without the schema-v3 stamp and under-reported by five tests (§1.3,
§3.4, §6.4); the abstract `Store.upsert_entity` contract was missing from the
lookup inventory (§1.4 row 2); `schema.py` was told to declare `name_key`, which
breaks fresh-store creation (§1.4 row 3, §3.3); and §5 Q1 framed an early `re.I`
ship as a release-timing choice when it is a lane-A gate waiver. Two minors
folded in: the `re.I` false-merge class (§3.5) and the app-defined-function
expression-index claim (§2d).

Companion docs: `docs/superpowers/workpackets.md` (§WP11 known limit, §WP4
read surface, §Open follow-ups), `docs/superpowers/specs/2026-07-16-sleep-time-maintenance-design.md`
(§4.1's `normalize_text` — the normalization precedent this decision reuses).

---

## 0. Decision summary

| Question | Decision | Why |
|---|---|---|
| Do case variants of an entity name resolve to one entity? | **Yes** — lookup on a stored normalized key (option **c**) | A false *split* is silent and permanent on the current surface; a false *merge* is visible in `history()` and recoverable (ADD-only deletes nothing). The asymmetry decides it (§3.2) |
| What is the key? | `NFC(name) → casefold() → collapse whitespace` — the **same function** as WP10a's `normalize_text` (`maintain/transforms.py:35-47`), promoted to a shared module | One normalization definition in the tree, not two that silently drift. Unicode-correct where SQLite `NOCASE` is not (§2.3) |
| SQLite `NOCASE` collation instead? | **No** | ASCII-only: `'Café'='CAFÉ'` → false, `'ЖУК'='жук'` → false (§6.2). Fixes the English demo, not the problem |
| Is the display form lost? | **No.** `entity.name` keeps the first-seen surface form verbatim; only the new `name_key` column is folded | The console renders `e.name`; nothing user-visible becomes lowercase |
| Schema change? | **Additive** — schema v3: `ALTER TABLE entity ADD COLUMN name_key`, backfill, new index, `PRAGMA user_version = 3`. No row deleted, no fact re-pointed | ADD-only-compatible; follows the `record_kind` precedent exactly (`sqlite_store.py:131-137`). The stamp bump is not free in tests: five stamp pins hard-code `2` and must move to `3` (§3.4, §6.4) |
| Are existing splits healed? | **No — forward-fix only.** Pre-existing `'Acme'`/`'ACME'` rows stay split; new mentions converge on a deterministic winner | Healing means re-pointing `fact.subject_id` — a *new* mutation verb plus a retroactive contradiction pass. Own decision, deferred (§3.3, §5) |
| Does this alone close #14's headline example? | **No — and this is the load-bearing finding.** `'I work at Acme.'` / `'i work at acme.'` splits on `'user'` vs `'i'`, not on `'Acme'` vs `'acme'` | The lowercase `i` misses the extractor's case-**sensitive** first-person regex (`gliner_extractor.py:287`) and falls through to the lead-token subject fallback. Verified: the collation fix alone leaves the pinned test passing (§1.3) |
| Scope of the fix, then | Store-side key **plus** a one-line `re.I` on the stub generator's first-person regex | Two independent defects wearing one symptom. Either alone leaves a live split (§1.3). The `re.I` half is **not** a pure bugfix — it also re-attributes `ME`/`Mine`/bare-`i` subjects to `'user'`; pinned, not inherited silently (§3.5) |
| Where? | Its own small lane-A packet, **WP15**, sequenced immediately **before** WP4 | WP4's `get_all(subject=...)` is the first public *name-keyed read* — it must inherit a settled policy, not invent one (§4) |
| New known limit | Genuinely case-distinct subjects in one namespace collapse (`Mercury`/`mercury`); on a functional predicate the earlier fact is superseded — verified, and recoverable via `as_of`/`history` | Pinned by a replacement test, exactly as the current limit is (§3.2, §3.4) |

---

## 1. The failure, precisely

### 1.1 Schema and code path

The entity table has no collation clause, so `name` is compared under SQLite's
default **BINARY** collation (`store/schema.py:22-31`):

```sql
CREATE TABLE IF NOT EXISTS entity (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL, name TEXT NOT NULL,
  type TEXT, summary TEXT, resolved_id TEXT, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_entity_lookup ON entity(namespace, name, type);
```

The resolve-or-create is one statement (`store/sqlite_store.py:183-197`):

```sql
SELECT * FROM entity WHERE namespace=? AND name=? AND IFNULL(type,'')=IFNULL(?,'')
```

Miss → `INSERT` a new entity with a new id. There is no normalization anywhere
between the extractor's surface span and this predicate:
`Memory._build_fact` (`memory.py:264`) passes `tf.subject_name` through
verbatim, with `type=None` on every ingest-path call (so the effective key today
is `(namespace, name)`).

Downstream, identity is *only* the id: the slot is `(subject_id, predicate)`
(`sqlite_store.py:331-336`), the WP11 restatement skip compares text **within a
slot** (`memory.py:201-203`), and `ContradictionResolver.classify` is handed
`slot_latest` from that same slot (`memory.py:192, 205`). A different
`subject_id` is therefore a different slot, and *both* guards are bypassed
structurally — neither is "wrong", they are never consulted.

### 1.2 Observable symptom

Two co-valid `is_latest=1` facts for one real-world subject, on the default
current-state surface (measured, §6.1):

```
add("ns", "Acme uses Postgres.")   → entity 'Acme'  fact is_latest=1
add("ns", "ACME uses Postgres.")   → entity 'ACME'  fact is_latest=1
entities: ['Acme', 'ACME']         ← one company, two identities
```

The read surfaces disagree with the write path about this, today: the console's
fact filter already folds case — `LOWER(e.name) = LOWER(?)`
(`console/src/lean_memory_console/inspect_sql.py:157-159`) — so filtering by
`acme` returns facts from *both* rows, while `list_entities`
(`inspect_sql.py:292-297`) lists them as two entities with split fact counts.
The product already behaves as if the fold were the intended semantics on read;
only the write path disagrees.

### 1.3 The headline example is two defects, not one

Issue #14's example is `'I work at Acme.'` vs `'i work at acme.'`. Running it
(§6.1) shows the split is **not** `Acme`/`acme`:

```
entities: ['user', 'i']     ← not ['Acme', 'acme']
```

Because `StubCandidateGenerator`'s first-person regex is case-**sensitive**
(`gliner_extractor.py:287`, no `re.I` — unlike the Phase-0 `RulesExtractor`,
`rules.py:36`, which has it), lowercase `i` is not recognized as first person
(`gliner_extractor.py:343`). Subject resolution then falls to
`_subject`'s "first Capitalized run, else lead token" fallback
(`gliner_extractor.py:384-396`), which yields the literal token `'i'`. The
object `acme` never reaches the entity table at all — objects are stored as
`object_literal`; the ingest path never sets `object_id` (`memory.py:269-283`),
so **subjects are the only entities the engine creates today**.

A four-way monkeypatch probe isolates the layers (§6.3):

| Configuration | #14's pinned example | `'Acme'` vs `'ACME'` subject |
|---|---|---|
| A — today | 2 co-valid facts, entities `['user','i']` | 2 entities |
| B — folded `upsert_entity` only | **still 2 facts** | 1 entity |
| C — `re.I` on the extractor only | 1 fact (restatement skip catches it) | **still 2 entities** |
| D — both | 1 fact | 1 entity |

Confirmed at suite scale (§6.4 — measured with the **full** recommended change
simulated, schema-v3 stamp included; an earlier revision of this doc omitted the
stamp and under-reported the blast radius): with **B alone** the offline suite is
`314 passed, 5 failed`, and
`test_entity_case_variant_splits_the_slot_known_limit` is **not** among the
failures — it still passes, i.e. the collation fix alone does not close the issue
as written. With **D**, the suite is `313 passed, 6 failed`: that pinned test
flips to 1 row (precisely the outcome its own docstring predicts), plus **five
schema-stamp pins** that hard-code the current version `2` and must be bumped to
`3` as part of the packet (enumerated in §6.4; test changes in §3.4 item 4).
Nothing else in the 319-test core suite moves, and the console package's separate
153-test suite is unchanged under the same simulation (§6.4).

The second defect also has a nastier cousin, unfixed by either half: a
sentence-initial lowercase proper noun makes `_CAP_RUN` pick the *wrong* subject
entirely — `add("acme uses Postgres.")` creates an entity named `'Postgres.'`
(trailing period included, because `_CAP_RUN` at `gliner_extractor.py:290`
admits `.` inside the run). That is an extraction bug, not a collation one; it
is out of scope here and recorded in §5.

### 1.4 Every entity-name lookup in the tree

A collation change touches all of these. Enumerated and verified:

| # | Site | What it does with a name | Effect of the change |
|---|---|---|---|
| 1 | `store/sqlite_store.py:183-197` `upsert_entity` | **The only name-keyed identity lookup.** `WHERE name=?`, BINARY | **Changes** — lookup moves to `name_key`; insert stores both columns |
| 2 | `store/base.py:33-35` `Store.upsert_entity` | The abstract **contract**: "Resolve-or-create. If an entity with the same `(namespace, name, type)` exists, return it; otherwise insert `entity` and return it." | **Changes — contract docstring must state the normalized key and the first-seen display-form guarantee.** Option (c) makes the sentence as written false (the key becomes `(namespace, name_key, type)`), and every future `Store` implementation reads this, not `sqlite_store.py`. Exactly the documented-contract drift row 8 flags |
| 3 | `store/schema.py:22-31` | `entity` DDL + `ix_entity_lookup(namespace, name, type)` | **Unchanged DDL — comment only**, mirroring `schema.py:84-88`. Both `name_key` and `ix_entity_key` live *only* in the v3 branch; putting the column in the always-run blob breaks fresh-store creation (§3.3) |
| 4 | `memory.py:264` `_build_fact` | Sole caller of (1) on the ingest path; `type=None` always | Unchanged code, new behavior |
| 5 | `store/sqlite_store.py:199-201` `get_entity` | Keyed by `id` | Unaffected |
| 6 | `memory.py:285-294` `_known_entity_names` | `SELECT name … ORDER BY created_at DESC LIMIT 100`; context only for router/typer since the `prior_entity` retirement | Unaffected mechanically; the list gets shorter (fewer duplicate rows) — strictly better context |
| 7 | `extract/llm_typer.py:414, 424-425` | `known_lookup = {e.lower(): e}` canonicalizes an LLM-resolved subject onto a known surface form | **Should adopt the shared key** — today `.lower()` (misses `ß`, `ﬁ`), cap-bounded at 100, and only on the `[llm]` path. A partial, silent duplicate of the policy |
| 8 | `extract/llm_typer.py:182-184` | `StubTyper` docstring claims `subject_name` is "matched case-insensitively against `known_entities`" — the code (`193-223`) never reads `known_entities` | **Doc/code mismatch**; fix the docstring while in there |
| 9 | `extract/router.py:223-225` `_norm` | `strip().lower()` + whitespace collapse, for pronoun/coref checks only | Unaffected (not an identity decision); worth noting as a third normalization definition in the tree |
| 10 | `extract/gliner_extractor.py:287, 343, 384-396` | Produces the subject surface form (first-person → `default_subject`, else Capitalized run, else lead token) | **Changes** — `re.I` on `_FIRST_PERSON` (§1.3), with the false-positive class in §3.5 |
| 11 | `extract/rules.py:36, 63` | Phase-0 extractor; already `re.I` | Unaffected (not in the default pipeline) |
| 12 | `maintain/transforms.py` (`:229`, `:289+`) | Groups by `subject_id`, never by name | Unaffected — but sees **larger, correctly-merged slots**, which is what its dedupe/summarize bands are for |
| 13 | `console/.../inspect_sql.py:157-159` | `LOWER(e.name) = LOWER(?)` fact filter | Should move to `name_key` for consistency (cross-package, own release). The console suite is **green unchanged** under the simulated engine change (§6.4), so this is a consistency cleanup, not a break |
| 14 | `console/.../inspect_sql.py:292-297` | `list_entities`, `ORDER BY fact_count DESC, e.name` (BINARY sort) | Unaffected under option (c). *Would* change under option (b) — a column-level `NOCASE` silently re-sorts this |
| 15 | Tests: `test_restatement_dedupe.py:50-65` and the five schema-stamp pins (`test_schema_version.py:17,26,37`; `test_schema_migration.py:83,112,148,156`), plus `test_asof_sparse.py:16`, `test_maintenance_*`, `test_tier_retrieval.py:32`, `test_known_entities_cap.py:14`, `test_dim_guard.py:23`, `test_proposal_lifecycle.py:66`, `test_ingest_commutation.py:37`, `test_store_maintenance_verbs.py:29`, `test_mcp_maintenance_tools.py:152,239`; console `test_engine.py:219`, `test_review_views.py:45`; `bench/smoke_quality.py:90` | Construct entities directly by name, or pin the schema stamp | **Six tests change** (§3.4): the pinned known limit plus the five stamp pins. The rest use distinct names and are unaffected — verified at suite scale for both packages (§6.4) |
| 16 | `mcp_server.py` (all 7 tools) | No tool takes an entity name today | Unaffected — but **WP4's `get_all(subject=...)` will be the first**, which is why the policy must settle before WP4 (§4) |

### 1.5 How often does this fire in the wild?

Drivers, in rough order of expected frequency:

1. **Sentence-initial casing.** Any subject that starts a sentence is
   capitalized; the same subject mid-sentence may not be. Symmetrically, chat
   users type `i`, `my`, `im` in lowercase constantly — the exact case that
   defect 2 mishandles.
2. **Extractor/LLM casing variance.** The GLiNER2 relation head is returned as
   the *surface span* (`gliner_extractor.py:243`, `:361`) — whatever casing the
   user typed. The Ollama typer may return a re-cased subject
   (`llm_typer.py:421-425`); its `.lower()` canonicalization only fires when the
   entity is within the most-recent-100 window, so long-lived namespaces drift.
3. **Acronym styling.** `Acme` / `ACME`, `NASA` / `Nasa`, `IBM` / `Ibm` — one
   speaker, one entity, two spellings, often in the same session.
4. **Speech-to-text and IME input.** Dictation lowercases aggressively;
   fullwidth CJK-IME Latin (`ＡＣＭＥ`) is a separate normalization class (§2.3).
5. **Multi-agent writers.** Two agents writing the same namespace with different
   prompt conventions produce different casings for the same subject.

None of these is exotic; all of them produce the §1.2 symptom, which is exactly
the failure the product's headline claim ("one current answer, auditable
history") is supposed to prevent.

---

## 2. Options

### (a) Status quo — BINARY, documented limit

Keep `name=?` under BINARY; keep the pinned test; document the limit in the
README.

- **Correctness:** never a false merge. `Polish`/`polish`, `US`/`us`,
  `Mercury`/`mercury` stay distinct — for free.
- **Migration cost:** zero.
- **Blast radius:** zero.
- **Interaction:** the split keeps bypassing restatement dedupe *and*
  contradiction resolution, structurally. WP11 and the whole supersession spine
  silently do not apply to a subject the user re-spelled.
- **Verdict:** honest but wrong. The failure is silent, the symptom (two current
  employers) is the single thing the engine is supposed to get right, and the
  console already contradicts the write path on read (§1.2). "Documented" is not
  a mitigation for a default-path correctness gap that the user cannot see.

### (b) SQLite `NOCASE` collation on `entity.name`

Column-level `name TEXT NOT NULL COLLATE NOCASE`, or an equivalent `COLLATE
NOCASE` on the lookup predicate plus a matching index.

- **Correctness:** ASCII-only, by definition. Measured (§6.2): `'Café'='CAFÉ'`
  → **false**; `'ЖУК'='жук'` → **false**; `'Weiß'='WEISS'` → **false**. It fixes
  `Acme`/`ACME` and nothing outside `[A-Za-z]`. For a local-first memory engine
  whose users write in every language, that is a fix shaped like a demo.
- **Migration cost:** the worst of the options. SQLite cannot alter a column's
  collation in place — it needs the 12-step table rebuild (new table, copy,
  drop, rename) on a table that `fact.subject_id` and `fact.object_id`
  **reference by foreign key** (`schema.py:37,39`), with `PRAGMA
  foreign_keys=ON` live (`sqlite_store.py:78`) and `ALTER TABLE … RENAME`
  rewriting FK clauses in other tables. Alternatively a `COLLATE NOCASE` on the
  predicate only — which then needs a matching expression index or the lookup
  silently drops to a scan.
- **Blast radius:** a column-level collation is not local. `ORDER BY e.name` in
  the console's `list_entities` (`inspect_sql.py:292-297`) silently changes sort
  semantics; every future comparison against `entity.name` inherits NOCASE
  whether or not the author wanted it. Collation-as-column-property is invisible
  at the call site — the opposite of this repo's explicit-normalization style.
- **Interaction:** correct direction (one slot, dedupe and contradiction both
  apply) but only for ASCII names.
- **Verdict:** rejected. Highest migration cost, most invisible blast radius,
  least correctness.

### (c) Stored normalized-name column — **RECOMMENDED**

Add `entity.name_key`; write `normalize(name)` on insert; look up on
`(namespace, name_key, type)`. `entity.name` keeps the first-seen surface form
for display.

- **Correctness:** Unicode-correct where it matters, because Python's
  `str.casefold()` is a full Unicode case fold, not an ASCII map. Measured
  (§6.2): `Café`/`CAFÉ` ✓, `ЖУК`/`жук` ✓, `Café`(NFC)/`Café`(NFD) ✓ via NFC,
  `Acme  Corp`/`Acme Corp` ✓ via whitespace collapse. Deliberate non-merges:
  `Acme`/`Acme.` ✗, `Yahoo!`/`Yahoo` ✗ (no punctuation stripping — unlike
  `_restatement_key`, `memory.py:65-66`, which strips edge punctuation on *fact
  text*; entity names must not, or `Yahoo!` dies), `Café`/`Cafe` ✗ (no diacritic
  folding — that is transliteration, not case).
  Residual folds it gets *wrong*: `Weiß`/`Weiss` merge (German ß→ss is right for
  `WEISS`, wrong for the distinct surname spelling), and Turkish `İstanbul` does
  **not** fold to `istanbul` (`'İ'.casefold()` = `i` + U+0307). Both are
  documented, both are locale problems no locale-independent fold can solve, and
  choosing a locale would break determinism.
- **Migration cost:** low and additive. Schema v3 in the existing versioned
  branch (`sqlite_store.py:131-137`): `ALTER TABLE entity ADD COLUMN name_key`,
  Python backfill (SQLite has no `casefold()`; the table is small — one row per
  distinct subject), `CREATE INDEX ix_entity_key`. **Nothing is deleted, no fact
  is re-pointed** — ADD-only holds trivially. One implementation trap, already
  paid for once by `record_kind`: **both** the new column and the new index must
  live inside the `if version < 3:` branch and **neither** may appear in the
  always-run `SCHEMA_SQL` blob — the blob executes first on every open
  (`sqlite_store.py:108-114`), so a `CREATE INDEX` there references a column a
  pre-v3 file does not have, and a `name_key` declared there collides with the
  `ALTER` on every fresh store (`duplicate column name`, reproduced §6.7). §3.3.
- **Blast radius:** exactly one lookup predicate changes (§1.4 row 1) — plus its
  abstract contract docstring (row 2) and the schema-stamp constant. Sorting,
  display, FTS, vec0, and every `subject_id`-keyed path are untouched; the
  console package's suite is green unchanged (§6.4).
  Normalization stays an explicit function call, matching `normalize_text`
  (`transforms.py:35-47`) and `_restatement_key` (`memory.py:65-66`).
- **Interaction:** case variants land in one `(subject_id, predicate)` slot, so
  the WP11 restatement skip and `ContradictionResolver` apply as designed —
  `'i work at acme.'` after `'I work at Acme.'` is skipped as a restatement;
  `'ACME lives in Berlin.'` after `'Acme lives in Boston.'` supersedes.
- **Verdict:** recommended (§3).

### (d) Exact match first, then casefold fallback with disambiguation

Try `name=?` BINARY; on a miss, retry on the folded key; if the folded retry
returns several rows, apply a disambiguation rule.

- **Correctness:** identical to (c) on a clean store — the fold key is unique in
  practice, so the "fallback" fires on every variant anyway. It differs only
  where several rows already share a key: a store that predates the change.
- **Migration cost:** same as (c) if the key is stored; worse if the fallback
  computes the fold in Python over a scan of the namespace's entities. SQLite's
  built-in `lower()` is ASCII-only, so an expression index over it does not help.
  A *deterministic app-defined* function **can** be indexed
  (`create_function(..., deterministic=True)` + `CREATE INDEX … ON
  entity(py_casefold(name))` — verified working, §6.7) — but that bakes a Python
  callable into the file's schema, and any process that has not registered the
  same function then fails on `INSERT`, `UPDATE`, `REINDEX`, `VACUUM`, `PRAGMA
  integrity_check`, and any query mentioning the expression (measured, §6.7;
  plain `SELECT *` still works, so the console's read-only paths would survive).
  For a local-first engine whose whole storage promise is "it is just a SQLite
  file any tool can open" — and which ships a *separate* package
  (`lean-memory-console`, `inspect_sql.py:22-32` `open_ro`) against the same
  namespace file — that is a worse trade than storing the key in a column.
- **Blast radius:** the disambiguation rule is new policy surface — "prefer the
  most-recently-used", "prefer the one with more facts", "escalate to the LLM"
  are all defensible and all need their own tests and their own failure modes.
- **Interaction:** same as (c).
- **Verdict:** rejected **as a separate option**, and absorbed into (c): the
  useful half is the *tie-break*, which (c) needs anyway for legacy split rows.
  (c) takes it in its deterministic, policy-free form — `ORDER BY created_at,
  id LIMIT 1`, oldest row wins — and drops the rest. A store written entirely
  after v3 never has a tie to break.

### (e) Resolution-time semantic matching (embedding / alias graph / LLM linking)

Resolve `acme` → `Acme Corporation` → `ACME Inc.` by similarity or an LLM
linking pass.

- **Out of scope, deliberately.** It is a different problem (entity *linking*,
  not *collation*): non-deterministic, model-dependent, and impossible to make
  byte-identical offline — which the repo's determinism contract requires
  (`FakeEmbedder`/`StubTyper`, workpackets "Global invariants"). It would also
  put a model call on the ingest hot path for every subject, against the
  escalation budget the launch gate just spent two rounds recalibrating
  (95.9% → 14.6%, `bench/results/calibration/README.md`). And it fails *open*:
  a wrong semantic merge is unbounded (`Apple` the company ↔ `apple` the fruit
  ↔ `Apple Records`), where a case fold's error set is small and enumerable.
  If demand ever justifies alias resolution, its natural home is the WP10a
  review queue as a **proposed** entity merge a human approves — not an ingest
  decision. Recorded, not scheduled.

### Comparison

| | (a) status quo | (b) NOCASE | **(c) name_key** | (d) exact+fallback | (e) semantic |
|---|---|---|---|---|---|
| `Acme`/`ACME` | split | merged | **merged** | merged | merged |
| `Café`/`CAFÉ`, `ЖУК`/`жук` | split | **split** | **merged** | merged | merged |
| `Polish`/`polish`, `US`/`us` | correct | merged | **merged (wrong)** | merged (wrong) | maybe correct |
| `Weiß`/`WEISS` | split | split | **merged** | merged | merged |
| `Yahoo!`/`Yahoo`, `Café`/`Cafe` | split | split | **split** | split | merged |
| Migration on existing DBs | none | table rebuild w/ FKs | **additive v3 column + backfill** | additive (if stored) | additive |
| Blast radius | none | column-wide, invisible | **one predicate** | one predicate + policy | ingest hot path |
| Determinism offline | yes | yes | **yes** | yes | **no** |
| Restatement dedupe applies | **no** | ASCII only | **yes** | yes | yes |
| Contradiction resolution applies | **no** | ASCII only | **yes** | yes | yes |

---

## 3. Recommendation

**Adopt option (c): resolve entities on a stored, Unicode-normalized name key;
preserve the surface form for display; ship it with the extractor's
first-person case-insensitivity fix, because neither half closes #14 alone.**

### 3.1 What it is, exactly

```python
# lean_memory/normalize.py (new, shared)
def normalize_text(s: str) -> str:
    """NFC + casefold + whitespace collapse. Value-preserving; nothing else."""
    return " ".join(unicodedata.normalize("NFC", s).casefold().split())

entity_key = normalize_text          # entity identity uses the SAME definition
```

This is `maintain/transforms.py:35-47` moved, not rewritten — WP10a already
argued and verified this exact normalization for DEDUP-EXACT ("two texts share a
normal form iff they are the same value written differently"). `transforms.py`
re-exports it so WP10a's import path and tests are untouched, and the tree stops
carrying two definitions that can drift. (`router.py:225`'s `_norm` stays as-is:
it is a coref heuristic, not an identity decision.)

Lookup becomes:

```sql
SELECT * FROM entity
 WHERE namespace=? AND name_key=? AND IFNULL(type,'')=IFNULL(?,'')
 ORDER BY created_at, id LIMIT 1        -- deterministic winner for legacy splits
```

with `INSERT` storing `name` (verbatim, first-seen) **and** `name_key`.
`type` stays in the key: the ingest path always passes `NULL` today, and typed
entities (WP4+) must still separate `Mercury`/person from `Mercury`/planet.

NFKC was considered and **not** taken: it would additionally merge fullwidth
`ＡＣＭＥ` with `ACME` (a genuine IME-input win) but is a compatibility fold —
`ﬁ`→`fi`, `½`→`1⁄2`, superscripts flattened — which is lossier than "the same
value written differently". Consistency with the verified `normalize_text`
precedent wins; the fullwidth case is recorded in §5 as a cheap follow-up if a
user ever reports it.

Plus the second half, one line in `extract/gliner_extractor.py:287`:

```python
_FIRST_PERSON = re.compile(r"\b(?:I|I'm|my|me|mine)\b", re.I)   # was: no flag
```

adopting the `re.I` that `rules.py:36` has had since Phase 0. Without it, #14's
own example still splits (`'user'` vs `'i'`) after any collation change (§1.3).

Two things this is **not**: it is not literally "matching `rules.py:36`" — that
pattern is `\b(I|I'm|I am|my|me|mine)\b`, carrying an `I am` alternative the
GLiNER stub's lacks. WP15 should either add `I am` to the stub for real parity or
state the divergence in a comment; leaving two near-identical first-person
patterns that differ silently is how the `_norm`/`normalize_text` drift started.
And it is not a *pure* bugfix: because `my`/`me`/`mine` are already lowercase in
the pattern, `re.I` adds not only the wanted lowercase `i` but also `ME`, `Mine`,
`MY`, and a bare `i` **anywhere** in the sentence — a false-merge class into the
busiest slot in the store, measured and pinned in §3.5.

### 3.2 How genuinely case-distinct entities behave — the new known limit

They merge, and the merge is asymmetric by predicate class. Verified (§6.5):

| Predicate class | Example | Result after the merge |
|---|---|---|
| Multi-valued (`uses`, `likes`, `has`, …; `contradiction.py:95-98`) | `"Polish uses seven cases."` + `"polish uses a mild solvent."` | Both stay `is_latest=1` under one entity. Damage: attribution only — two true facts hang off one identity, displayed under the first-seen surface form |
| Functional (`works_at`, `lives_in`, …) | `"Mercury lives in Rome."` + `"mercury lives in thermometers."` | The **earlier fact is superseded** (`is_latest=0`, `superseded_by` set). Damage: real — the current surface loses a true fact |

**The new pinned known limit** (replacing the current one) is therefore:

> Two genuinely distinct entities in the same namespace whose names differ only
> by case (or by a Unicode case fold) resolve to one entity. On a functional
> predicate this retires the earlier fact from the current surface. Nothing is
> deleted: the retired fact remains readable via `search(as_of=…,
> is_latest_only=False)` and, after WP4, via `history()`.

Why this trade is right:

1. **The errors are not symmetric.** A false split is silent, permanent on the
   current surface, and breaks the product's core promise (one current answer).
   A false merge is visible in the supersession chain and reversible in
   principle, because ADD-only never deletes.
2. **The false-merge population is small on today's ingest path.** Only
   *subjects* become entities (§1.3), and subjects come from: the default
   subject `'user'`; a Capitalized run; the lead-token fallback; or a
   model/LLM-resolved span. A genuine case-distinct collision needs one side to
   arrive as a lowercase common noun in subject position — which, when it
   happens today, is usually a *misextraction* anyway (`'Postgres.'` as a
   subject, §1.3).
3. **`Polish`/`polish`, `US`/`us` are counterexamples for a general
   case-insensitive *dictionary*, not for a per-namespace agent memory.** These
   collide only if one namespace genuinely talks about both senses *in subject
   position*. That is rarer than the same user typing their employer two ways.
4. **The alternative is not "no error", it is "the other error, always."**
   Option (a) chooses the silent error unconditionally.

### 3.3 Migration path for existing stores

Forward-fix, additive, one new versioned branch alongside `record_kind`
(`sqlite_store.py:125-137`):

```python
if version < 3:
    db.execute("ALTER TABLE entity ADD COLUMN name_key TEXT NOT NULL DEFAULT ''")
    for row in db.execute("SELECT id, name FROM entity").fetchall():   # Python: SQLite has no casefold
        db.execute("UPDATE entity SET name_key=? WHERE id=?", (normalize_text(row["name"]), row["id"]))
    db.execute("CREATE INDEX IF NOT EXISTS ix_entity_key ON entity(namespace, name_key, type)")
    db.execute("PRAGMA user_version = 3")
```

Properties and consequences, stated plainly:

- **ADD-only holds.** No `DELETE`, no `fact.subject_id` rewrite, no change to
  any fact's validity interval. The as-of surface before and after the migration
  is byte-identical.
- **The `CREATE INDEX` must be in the branch, not in `SCHEMA_SQL`** — the blob
  runs first on every open (`sqlite_store.py:108-114`) and would reference a
  column that does not yet exist on a pre-v3 file. Same trap `record_kind`
  documented at `schema.py:84-88`.
- **`name_key` must NOT be added to `SCHEMA_SQL`'s `CREATE TABLE entity`
  either.** The blob runs on every open, and a fresh DB is stamped version 1 and
  re-enters the same `< 3` branch (`sqlite_store.py:120-137` — there is no
  separate fresh path), so declaring the column in the blob *and* adding it by
  `ALTER` raises `sqlite3.OperationalError: duplicate column name: name_key` on
  **every fresh store creation** — i.e. it breaks the first-run path the launch
  gate pinned. Reproduced (§6.7). The two instructions are mutually exclusive;
  the `ALTER` wins, and `schema.py` gets a comment only, exactly as
  `fact.record_kind` is deliberately absent from the blob's `CREATE TABLE fact`
  (`schema.py:84-88`).
- **Backfill is a Python loop by necessity** (no `casefold()` in SQLite) but the
  table is one row per distinct subject — hundreds, not millions — and it runs
  inside `_init_schema`'s single transaction.
- **Pre-existing splits are NOT healed.** After migration `'Acme'` and `'ACME'`
  both carry `name_key='acme'` and keep their own facts. The next mention
  resolves to the **oldest** row (`ORDER BY created_at, id LIMIT 1`), so the
  store converges going forward with one legacy remnant.
- **Healing is deliberately deferred**, because it is a second decision, not an
  implementation detail: re-pointing `fact.subject_id` from loser to winner is a
  *new* mutation verb (WP10a's sanctioned set is append / `supersede_fact` /
  `retire_duplicate` / `set_tier` — §4.0 of that spec), and a merge can leave two
  co-valid latest facts in one functional slot that no write-time resolver ever
  adjudicated. The right shape, if it is ever wanted, is a **`merge_entity`
  proposal** in the WP10b review queue — human-approved, evidence-carrying,
  default-off — not a migration that rewrites facts while the user is opening a
  database. Recorded in §5.
- **Downgrade:** a v3 file opened by v0.2.3 code works unchanged (the extra
  column is ignored; `_init_schema` never downgrades a newer stamp,
  `sqlite_store.py:116-123`) — but the older code writes rows with an empty
  `name_key`, which the newer code would then mis-resolve. Same one-way
  expectation as schema v2; state it in CHANGELOG.

### 3.4 Test changes

1. **Delete** `tests/test_restatement_dedupe.py::test_entity_case_variant_splits_the_slot_known_limit`
   (lines 50-65) — its docstring already prescribes exactly this.
2. **Fold the variant** into
   `test_trivial_formatting_variants_are_treated_as_restatements` by adding
   `mem.add("ns", "i work at acme.", t_ref=5_000)` to the existing sequence; the
   `len(rows) == 1` assertion carries it. Verified to pass under the simulated
   fix (§6.3, config D).
3. **New `tests/test_entity_collation.py`:**
   - `Acme` / `ACME` / `acme` in subject position → one entity, one latest fact;
   - display form is the **first-seen** surface form (`'Acme'`, not `'acme'`);
   - the Unicode cases `NOCASE` would miss: `Café`/`CAFÉ`, `ЖУК`/`жук`,
     NFC-vs-NFD `Café` — one entity each;
   - deliberate non-merges: `Acme` vs `Acme.`, `Yahoo!` vs `Yahoo`, `Café` vs
     `Cafe` — two entities each;
   - **the new pinned known limit**, asserting the documented behavior rather
     than the desired one: `Mercury`/`mercury` on a functional predicate → one
     entity, earlier fact `is_latest=0` with `superseded_by` set, and still
     retrievable at `as_of` (so the doc's "recoverable" claim is executable, not
     rhetorical).
4. **The five schema-stamp pins — mandatory, and the largest single block of
   test churn in the packet.** All five fail on the v3 stamp alone (§6.4); an
   implementer who skips them ships a red suite:
   - `tests/test_schema_version.py:17` — bump `CURRENT_SCHEMA_VERSION = 2` → `3`
     (fixes `test_fresh_store_stamps_current_version` and
     `test_migrated_file_reopens_at_current_version`, lines 26 and 37);
   - `tests/test_schema_migration.py` — the four hard `== 2` assertions at lines
     **83, 112, 148, 156** across three tests (`test_v1_migrates_once_to_v2`,
     `test_v1_reopens_cleanly_after_migration`,
     `test_fresh_create_stamps_v2_and_reopens_clean`) become `== 3`: a v1 fixture
     now migrates 1→3 in a single open, and a fresh create lands on 3. Those
     three test *names* say "v2" and should be renamed or generalized, and both
     module docstrings narrate "the current version (2)" — update them too.
5. **`tests/test_schema_migration.py`, new coverage:** extend to the 2→3 upgrade
   with a checked-in `tests/fixtures/v2_format.db` (built the same hand-rolled
   way as `make_v1_fixture.py`), pinning: v2 fixture opens → migrates once →
   `name_key` backfilled for every pre-existing row → **reopens clean** (the
   ALTER idempotence trap) → a v2 file with pre-existing case-split entities
   keeps both rows and both facts after migration (the "no silent heal"
   guarantee).
6. **`tests/test_phase1_extraction.py`:** coverage for the `re.I` change, in
   both directions —
   - the fix: `"i work at acme."` → subject `user` (today: `i`);
   - the **regression class it creates** (§3.5), pinned so it is a decision and
     not an accident: `"Mine uses explosives."` and `"ME uses Postgres."` →
     subject `user` (today: `Mine`, `ME`), and a bare `i` anywhere in a sentence
     (`"The company Acme uses i as a variable."` → `user`, today `The`).
   - Note `"my budget is 40 euros."` is **not** a valid test of this change:
     `my` is already lowercase in the pattern, so it resolves to `user` today.
     The only genuinely-new lowercase form is `i`.
7. **`extract/llm_typer.py:414`:** switch `known_lookup` to the shared key and
   fix the `StubTyper` docstring (§1.4 rows 7-8); no new test needed beyond the
   existing typer suite. Same pass: update the abstract contract docstring at
   `store/base.py:33-35` to name the normalized key and the first-seen display
   form (§1.4 row 2).

Expected suite delta, measured (§6.4): the simulated change takes the core suite
from `319 passed` to `313 passed, 6 failed`. Items **1** (delete the pin) and
**4** (bump the five stamps) between them convert all six failures; items 2, 3,
5 and 6 add roughly a dozen new tests. Net: back to green, at ~330. **No other
test in either package moves** — the console suite is `153 passed` before and
after (§6.4).

### 3.5 What this explicitly does not do

Diacritic/transliteration folding (`Cafe`≠`Café`), punctuation stripping
(`Yahoo!`≠`Yahoo`), abbreviation or alias resolution (`IBM`≠`International
Business Machines`), object-side entity resolution (objects are literals today),
locale-tailored folding (Turkish dotted `İ`), and any retroactive merge of rows
already written.

**And one regression it actively introduces.** `re.I` on `_FIRST_PERSON` is not
free: since `my`/`me`/`mine` are already lowercase in the pattern, the flag also
admits their cased forms and a bare `i` anywhere in the sentence, so a sentence
whose genuine subject *is* that noun is silently re-attributed to
`default_subject`. Measured (§6.7):

```
add("Mine uses explosives.")                    → entity 'user'   (today: 'Mine')
add("ME uses Postgres.")                        → entity 'user'   (today: 'ME')
add("The company Acme uses i as a variable.")   → entity 'user'   (today: 'The')
```

This is a false merge into `'user'` — the single busiest slot in any store — and
it is created by the *extractor* half, the half §5 Q1 contemplates shipping
early. It is bounded (the offending sentence must have a bare `i`/`ME`/`Mine`
before its relation verb, and the third example was already misextracting `'The'`
anyway), it is recoverable on the same `as_of`/`history` terms as §3.2's known
limit, and it is unpinned by any test today — which is why §3.4 item 6 adds one.
Weigh it against the §3.2 trade explicitly rather than inheriting it silently.

---

## 4. Where it should be implemented

**Its own small lane-A packet — WP15 — sequenced immediately before WP4**, not
folded into WP4.

Why not inside WP4: WP4 is already an M-effort read-surface packet whose
acceptance criteria are about `history`/`get_all`/`explain`/`explain`-scores. A
persisted-format migration plus a change to *entity identity semantics* buried
inside it would (i) mix two review conversations, (ii) make WP4's "no write-path
change" scope line false, and (iii) put the schema-migration anchor obligation
(shared with WP5/WP6, workpackets §WP10a "Interactions") inside a packet that
does not otherwise need it.

Why immediately *before* WP4: `Memory.get_all(namespace, *, subject=…)` is the
first public **name-keyed read** the project will ship. If WP15 lands first,
`get_all(subject="acme")` inherits the settled key and needs no policy of its
own; if WP4 lands first, it either invents a second policy or ships a read that
disagrees with the write path (the mistake the console already made,
`inspect_sql.py:157-159`).

**Gate:** lane A is gated on the six-week demand read (window start 2026-07-29,
ends ~2026-09-09), whose recorded condition is "**no new engine work between
launch and the read**" (`workpackets.md`, WP4 row). WP15 therefore implements
after the read closes and before WP4. The one open question is whether the
maintainer wants to **grant an explicit exception to that gate** for the
extractor half alone (`re.I`, one line, no schema change, closes #14's literal
example) as a patch inside the window — `src/lean_memory/extract/` is engine
work, so this is a waiver, not a release-timing preference. See §5, Q1.

### Packet sketch

> ## WP15 — Entity name collation (`name_key`)
>
> **Branch:** `wp15-entity-collation` · **Blocked by:** — (design: this doc)
> · **Gate:** six-week read (lane A); sequence **before WP4** · **Effort:** S
> · **Lane A — claims `memory.py` + `store/` + `extract/`**
>
> **Goal:** One real-world subject resolves to one entity regardless of the
> casing the user typed, so the WP11 restatement skip and contradiction
> resolution actually apply to it. Closes #14.
>
> **Scope (in):** shared `normalize_text` in a neutral module (moved from
> `maintain/transforms.py`, re-exported there); `entity.name_key` + schema v3
> versioned migration + backfill + `ix_entity_key`; `upsert_entity` lookup on
> `(namespace, name_key, type)` with `ORDER BY created_at, id LIMIT 1`;
> `re.I` on `StubCandidateGenerator._FIRST_PERSON` **plus a pin for the
> `ME`/`Mine`/bare-`i` false-merge class it creates** (§3.5); the abstract
> `Store.upsert_entity` contract docstring (`base.py:33-35`); `llm_typer`
> known-entity canonicalization on the shared key + `StubTyper` docstring fix;
> `tests/test_entity_collation.py`; **the five schema-stamp pins bumped 2 → 3**
> (§3.4 item 4 — non-optional, they fail on the stamp alone); v2 fixture + 2→3
> migration test; the replacement pinned known limit; README/CHANGELOG note on
> the fold and its limit.
>
> **Scope (out):** healing pre-existing splits (needs a `merge_entity`
> proposal — WP10b surface, own decision); object-side entity resolution;
> alias/semantic linking (anti-goal-adjacent, §2e); the `_CAP_RUN`
> lowercase-proper-noun misextraction (§5 Q3); console `name_key` adoption
> (separate package/release — follow-up).
>
> **Files:** `src/lean_memory/normalize.py` (new),
> `src/lean_memory/store/base.py` (contract docstring — the abstract
> `upsert_entity` still promises a `(namespace, name, type)` key),
> `src/lean_memory/store/schema.py` (**comment only — no DDL change**; the
> column and the index both live in the v3 branch, §3.3),
> `src/lean_memory/store/sqlite_store.py`,
> `src/lean_memory/maintain/transforms.py` (re-export only),
> `src/lean_memory/extract/gliner_extractor.py`,
> `src/lean_memory/extract/llm_typer.py`, `tests/test_entity_collation.py`
> (new), `tests/test_restatement_dedupe.py`,
> `tests/test_schema_version.py` (**bump `CURRENT_SCHEMA_VERSION` 2 → 3**),
> `tests/test_schema_migration.py` (four `== 2` assertions → `== 3`, lines
> 83/112/148/156, **plus** the new 2→3 case),
> `tests/fixtures/make_v2_fixture.py` + `v2_format.db` (new),
> `tests/test_phase1_extraction.py`, `README.md`, `CHANGELOG.md`.
>
> **Acceptance criteria:** `Acme`/`ACME`/`acme` → one entity and one latest
> fact; display form is first-seen; `Café`/`CAFÉ` and `ЖУК`/`жук` merge (the
> cases `NOCASE` cannot); `Yahoo!`/`Yahoo` and `Café`/`Cafe` stay distinct;
> the v2 fixture migrates once, reopens clean, backfills every row, and leaves
> pre-existing split rows and their facts untouched; the new known limit is
> pinned *with* its as-of recoverability; **both** suites green (core 319+ and
> console 153); first-run path unchanged for a fresh store beyond the intended
> resolution behavior — specifically, creating a fresh store must not raise
> `duplicate column name: name_key` (§3.3).
>
> **Verification:** `.venv/bin/python -m pytest tests/ -q` **and**
> `console/.venv/bin/python -m pytest tests/ -q` from `console/` (it resolves
> `lean_memory` from this working tree, so an engine schema change reaches it);
> a 6-line REPL walkthrough (mixed-case employer → one current fact) in the PR
> description; confirm `PRAGMA user_version` = 3 on a fresh and on a migrated
> file.

---

## 5. Open questions (for the maintainer)

1. **Ship the extractor half early — i.e. waive the lane-A gate?** State it
   plainly, because it is not a release-timing preference: the lane-A gate
   condition recorded in `docs/superpowers/workpackets.md` (WP4 row) is
   literally **"no new engine work between launch and the read"**, and
   `src/lean_memory/extract/gliner_extractor.py` is engine code. Shipping the
   `re.I` one-liner inside the six-week window therefore requires the maintainer
   to **grant an explicit exception to that gate**, not merely to pick a release
   train.

   What is on offer: one line, no schema impact, closes #14's literal example on
   its own (§6.3, config C). Three costs to weigh against it:

   - **It is not a pure bugfix.** It also creates the `ME`/`Mine`/bare-`i`
     false-merge class measured in §3.5 — sentences whose genuine subject is
     that noun get re-attributed to `'user'`. That regression is unpinned by any
     test today, so an early ship should carry §3.4 item 6's pins with it.
   - **It changes offline-default *routing*** —
     `explicit = first_person and relation in _EXPLICIT_RELATIONS`
     (`gliner_extractor.py:356-357`) — so lowercase-`i` sentences become
     high-confidence and route `direct` instead of escalating. No published
     number moves (the frozen calibration was measured on the real GLiNER2
     backbone, not the stub), but any *future* offline escalation measurement
     shifts, and frozen-config discipline says say so out loud.
   - **It segments the demand read.** The window (start 2026-07-29) is supposed
     to measure demand against a fixed engine; a mid-window engine release means
     the read must be reported as two segments with the release date noted, the
     same discipline CLAUDE.md already prescribes for the optional channel posts.

   **Recommendation: defer to WP15 unless the maintainer waives the gate.** The
   fix is one line and loses nothing by waiting five weeks; the gate exists so
   the read means something. If the waiver *is* granted, ship it with §3.4 item
   6's regression pins, a CHANGELOG note naming the routing shift, and the
   release date recorded for demand-read segmentation.
2. **Heal pre-existing splits later, or never?** §3.3 defers this to a possible
   `merge_entity` proposal in the WP10b review queue. Confirm "defer" — or say
   now if the answer should be "never", so the doc can close it.
3. **The `_CAP_RUN` misextraction** (`add("acme uses Postgres.")` → entity
   `'Postgres.'`, §1.3) is a *different* bug that this decision leaves standing.
   Own issue, or fold into WP15's scope as a second one-liner (require the
   capitalized run to start at the sentence head, or strip trailing `.` from the
   run)? **Recommendation: own issue** — it is an extraction-quality question
   with its own counterexamples, not a collation one.

---

## 6. Verification record (2026-08-06)

Every number above came from one of these. All run against the working tree on
`main` at 8ee6108 with `.venv` (Python 3.13.7, SQLite 3.53.4) — plus, for the
cross-package row, `console/.venv` (Python 3.14), which resolves `lean_memory`
from this same working tree. Scratch scripts and pytest plugins lived outside the
repo and **nothing in the tree was modified**.

Rev 2 (2026-08-06, post-review) corrects §6.4: the rev-1 simulation omitted the
`PRAGMA user_version = 3` stamp and therefore under-reported the blast radius by
five tests, and never ran the console suite at all. §6.7 is new.

**6.1 — Baseline behavior** (`Memory.add` on a temp root, dumping `entity` and
`fact`):

```
pinned known limit          entities: ['user', 'i']       2 facts, both is_latest=1
object-only case variant    entities: ['user']            1 fact  (WP11 restatement key already folds fact text)
proper-subject case variant entities: ['Acme', 'ACME']    2 facts, both is_latest=1
lowercase-initial subject   entities: ['Acme', 'Postgres.']  ← the §1.3 misextraction
```

**6.2 — Folding comparison** (SQLite `COLLATE NOCASE` vs `NFC+casefold` vs
`NFKC+casefold`):

```
a            b            NOCASE  NFC+cf  NFKC+cf
'Acme'       'ACME'       True    True    True
'Café'(NFD)  'Café'(NFC)  False   True    True
'Café'       'CAFÉ'       False   True    True
'ЖУК'        'жук'        False   True    True
'Weiß'       'WEISS'      False   True    True
'Weiß'       'Weiss'      False   True    True
'İstanbul'   'istanbul'   False   False   False
'ＡＣＭＥ'   'ACME'       False   False   True
'Acme'       'Acme.'      False   False   False
'Yahoo!'     'Yahoo'      False   False   False
'Acme  Corp' 'Acme Corp'  False   True    True
'Café'       'Cafe'       False   False   False
```

**6.3 — Layer isolation** (monkeypatched `upsert_entity` and/or
`_FIRST_PERSON`; §1.3 table):

```
A baseline                    pinned: entities=['i','user'] rows=2
B store-side fold only        pinned: entities=['i','user'] rows=2   ← fix does NOT close #14 alone
                              'Acme'/'ACME': entities=['Acme']
C extractor re.I only         pinned: entities=['user']     rows=1
                              'Acme'/'ACME': entities=['ACME','Acme']
D both                        pinned: entities=['user']     rows=1
                              'Acme'/'ACME': entities=['Acme']
```

**6.4 — Suite-scale blast radius** (pytest plugin injected via `PYTHONPATH`,
repo untouched). The plugin simulates the **full** recommended change: `ALTER
TABLE entity ADD COLUMN name_key` + Python backfill + `CREATE INDEX
ix_entity_key` + **`PRAGMA user_version = 3`** + the folded `upsert_entity`
lookup + `re.I`. (Rev 1 of this doc monkeypatched only `upsert_entity` and
`_FIRST_PERSON`, omitting the stamp, and consequently reported `319 passed` /
`318 passed, 1 failed` — those numbers were wrong and are superseded here.)

```
core suite  (.venv/bin/python -m pytest tests/ -q)
  baseline                             319 passed
  store-side fold + v3 stamp (B)       314 passed, 5 failed
    └ the known-limit pin is NOT among them — it still passes, i.e. the
      collation fix alone does not close #14
  full change (D)                      313 passed, 6 failed

the six failures under D:
  tests/test_restatement_dedupe.py::test_entity_case_variant_splits_the_slot_known_limit
    assert 1 == 2  — the pin flipped to one row, exactly as its docstring predicts
  tests/test_schema_version.py::test_fresh_store_stamps_current_version        assert 3 == 2
  tests/test_schema_version.py::test_migrated_file_reopens_at_current_version  assert 3 == 2
  tests/test_schema_migration.py::test_v1_migrates_once_to_v2                  assert 3 == 2
  tests/test_schema_migration.py::test_v1_reopens_cleanly_after_migration      assert 3 == 2
  tests/test_schema_migration.py::test_fresh_create_stamps_v2_and_reopens_clean assert 3 == 2

console suite (console/.venv/bin/python -m pytest tests/ -q, run from console/;
that venv resolves lean_memory from THIS working tree, so the simulation reaches it)
  baseline                             153 passed
  full change (D)                      153 passed   ← cross-package blast radius: none
```

Five of the six are pure schema-stamp arithmetic (`CURRENT_SCHEMA_VERSION = 2`
at `test_schema_version.py:17`; hard `== 2` at `test_schema_migration.py:83,
112, 148, 156`) and are mandatory packet work, not surprises — §3.4 item 4.

**6.5 — False-merge consequences** (fold enabled, distinct-by-case subjects):

```
multi-valued  'Polish uses seven cases.' / 'polish uses a mild solvent.'
              → entities=['Polish']; BOTH facts is_latest=1 (attribution only)
functional    'Mercury lives in Rome.'  / 'mercury lives in thermometers.'
              → entities=['Mercury']; first fact is_latest=0, superseded_by set
```

**6.6 — Code reading** (no execution): `upsert_entity` is the only name-keyed
identity lookup in `src/` (`grep -rn "FROM entity" src/` → `sqlite_store.py:185`,
`sqlite_store.py:200`, `memory.py:291`; `grep -rn "JOIN entity" src/` → no hits);
its abstract contract lives at `store/base.py:33-35`; the ingest path never sets
`fact.object_id` (`memory.py:269-283`); no MCP tool takes an entity name
(`mcp_server.py`, 7 tools); the console's `list_facts` already folds case on read
(`inspect_sql.py:157-159`).

**6.7 — Traps and regressions reproduced** (three throwaway scripts):

```
(a) name_key in SCHEMA_SQL's CREATE TABLE entity *and* ALTERed in the < 3 branch,
    fresh store creation:
      sqlite3.OperationalError: duplicate column name: name_key
    (a fresh DB is stamped 1 and re-enters the < 3 branch — sqlite_store.py:120-137)

(b) re.I on _FIRST_PERSON, subject of the resulting entity:
                                              baseline      with re.I
      "Mine uses explosives."                 ['Mine']      ['user']
      "ME uses Postgres."                     ['ME']        ['user']
      "The company Acme uses i as a variable" ['The']       ['user']
      "i work at acme."                       ['i']         ['user']   ← the intended fix
      "my budget is 40 euros."                ['user']      ['user']   ← unchanged; `my`
                                                                         is already lowercase
                                                                         in the pattern

(c) expression index over a deterministic app-defined function (SQLite 3.53.4):
      create_function("py_casefold", 1, ..., deterministic=True)
      CREATE INDEX ix ON entity(py_casefold(name))            → OK, and the index is used
    then reopening WITHOUT registering the function:
      SELECT *                    OK
      SELECT ... py_casefold(...)  OperationalError: no such function: py_casefold
      PRAGMA integrity_check       OperationalError: unknown function: py_casefold()
      REINDEX / VACUUM / UPDATE / INSERT
                                   OperationalError: unknown function: py_casefold()
      DELETE                       OK
```
