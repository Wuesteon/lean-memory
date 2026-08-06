# Deletion & Erasure (WP5) — Design

Date: 2026-08-06. Status: **proposed design, rev 2** — decision-ready, awaiting
maintainer sign-off on §14. Packet: WP5 (`docs/superpowers/workpackets.md`),
issue [#17](https://github.com/Wuesteon/lean-memory/issues/17). This document is
the **ungated** half of WP5: the design can land anytime; the implementation is
blocked by **WP4** (the read surface must be able to render a tombstone) **and**
the six-week demand read. All `file:line` references were verified against the
working tree at v0.2.3; every empirical claim in §2.3 and §6 was measured with
scratch scripts against the project venv (SQLite 3.53.4 / sqlite-vec 0.1.9 /
Python 3.13) — the runs are summarized in §13.

**What changed in rev 2** (design review, 2026-08-06 — four leaks and five
mis-statements, each re-verified against the tree before the fix landed):

| Change | Sections |
|---|---|
| **New closure: dedup-retired duplicates.** A DEDUP-EXACT loser keeps its text and both index rows and differs from the survivor only in case/spacing/Unicode form — erasing the survivor alone left a retrievable copy | §0, §1.3.11, §4.1, §5.2a, §5.6, §6.2, §9.17, §13.9 |
| **The `entity` unit cannot reach a fact-less episode**, and now says so, counts them, and offers an opt-in name sweep | §0, §1.3.12, §4.2, §4.3, §5.5b, §7.1, §7.5, §9.18, §13.12 |
| **`wal_checkpoint(TRUNCATE)` fails silently** under a concurrent reader (returns `busy=1`, does not raise) — the scrub procedure now closes the cached store first, checks the return row, and can report `reclaimed=False` | §0, §5.7, §6.3, §7.1, §7.5, §9.1, §13.5, §14.9 |
| **The console `_events.db` stores verbatim `fact_text`**, not merely search queries — re-characterized everywhere and re-rated from a follow-up to a decision required now | §1.2, §6.3, §7.5, §10, §13.6, §14.5 |
| `entity.resolved_id` is never populated: the alias closure is dead code and entity identity is exact string equality | §1.3.10, §4.2, §7.5, §12, §13.10 |
| The namespace string survives every sub-namespace erasure — in six tables, the vec0 metadata, the ledger, and the filename | §1.2, §3.1, §6.1, §6.2, §8, §10 |
| `episode.source` is caller-controlled free text, not engine provenance; redacted with `raw` | §1.2, §4.3, §5.5, §6.2, §9.20, §13.11 |
| The purge splice must resolve to the **live canonical** and close open referrers, or it breaks the invariant WP10a's duplicate-cascade depends on | §0, §3.2, §5.1, §7.2, §9.4, §9.5 |
| Maintenance proposals are **never deleted** — the LIKE scan is O(all proposals), and verbatim fact text accumulates unbounded | §0, §5.3, §13.8, §14.8 |
| Table inventory corrected (24, not 21); MCP parameter spelling reconciled with the Python API and `test_mcp_parity.py` | §13.1, §7.3, §7.4 |

Companion docs: `docs/superpowers/specs/2026-07-16-sleep-time-maintenance-design.md`
(WP10a/WP10b — its `fact_derivation` lineage and `maintenance_proposal` payloads
are the cascade's hardest cases, §5.2/§5.3); `docs/superpowers/workpackets.md`
(§WP4 read surface, §WP5 this packet, §WP6 scoping — and the global ADD-only
invariant this design is the sanctioned exception to, §9.1);
`ARCHITECTURE.md` §"ADD-only writes / supersession".

**Not legal advice.** This spec takes engineering positions that are *defensible*
under GDPR Art. 17 and its analogues, and states plainly where the guarantee
stops. Whether a given deployment discharges a given legal obligation is the
deploying controller's call, not the library's.

## 0. Decision summary

| Question | Decision | Why |
|---|---|---|
| What does `delete` mean by default? | **Redaction**: destroy every byte of content (`fact.fact_text`, `fact.object_literal`, `episode.raw`, `entity.name`/`summary`, embeddings, FTS postings); keep a content-free **tombstone** row carrying only ids, timestamps, and chain pointers | Erasure obligations attach to *personal data*, not to the existence of a record. Redaction destroys the data and keeps the graph valid — true row removal is refused outright by the engine's own FKs (§2.3.3, measured) |
| Is true row removal available at all? | **Yes, as an explicit second mode** (`mode="purge"`), chain-spliced, never the default | Some callers genuinely need the row gone. It is a *stronger physical* act and a *weaker honest* one — it forges the audit chain (§6.2) |
| Erasure units | `fact` · `entity` (subject + all its facts) · `episode` (raw text) · `namespace` (file unlink) — each defined by an exact selector in §4 | These are the four granularities the schema can express without WP6 scoping |
| Reach of the `entity` unit | **Facts, not messages.** An ingested message that produced *no* fact leaves an `episode.raw` row no fact-derived closure can reach; `forget(entity=…)` will not find it. Opt-in `sweep_episodes=True` offers a name scan; otherwise the report warns (§4.2, §5.5b) | `Memory.add` commits the episode *before* extraction and returns early on zero candidates (`memory.py:148-154`); the WP11 restatement guard skips fact creation the same way (`:201-203`). At GLiNER `DEFAULT_THRESHOLD=0.4` (~3.67 facts/turn) this is a routine outcome, not an edge case |
| Supersession chain through an erased fact | **Redact:** pointers untouched, chain walks intact through the tombstone. **Purge:** splice `A.superseded_by := B.superseded_by` before removing B, and record the splice in the erasure ledger | FK enforcement forbids removing a referenced fact (measured); splicing keeps WP4's `history()` walkable, at the cost of a chain that no longer records that B ever existed |
| `fact_derivation` lineage (WP10a summaries) | **Cascade, transitively and by default**: a summary derived from an erased fact is erased in the same mode. Erasure walks the *full* closure, including summaries already `is_latest=0` | A summary is distilled content of its sources. Leaving it is the single largest leak in the design (§5.2). Note the existing helper filters `is_latest=1` (`sqlite_store.py:295-309`) — erasure needs an unfiltered variant |
| **Dedup-retired duplicates** (WP10a DEDUP-EXACT losers) | **Cascade, to fixpoint**: every fact `WHERE superseded_by IN (:closure) AND valid_to IS NULL` joins the closure | `retire_duplicate` (`sqlite_store.py:485-520`) flips only `is_latest`; the loser's `fact_text`, `fact_fts` row and `fact_vec` row stay fully intact. Because `normalize_text` folds only case/spacing/Unicode form (`transforms.py:34-46`), a loser is a **byte-equivalent copy of the survivor's sentence**, still indexed and still retrievable under `is_latest_only=False`. Erasing the survivor alone is a direct content leak (§5.2a) |
| `maintenance_proposal.payload_json` | **Erased too** — pending/decided proposals hold *verbatim* copies of `fact_text` (`transforms.py:249, 347, 423`) and `edited_text`, **forever** (nothing in the package ever deletes a proposal row) | Second-largest leak; a content copy outside the `fact` table that no other cascade would reach, in an append-only table with no retention policy (§5.3, §14.8) |
| FTS5 / vec0 remnants | `DELETE FROM fact_fts` **is not sufficient** — the term survives in live index segments (measured: 1 → 2 blocks). Must run `INSERT INTO fact_fts(fact_fts) VALUES('optimize')`. `DELETE FROM fact_vec` does clear the vector bytes from the chunk blob (measured 1 → 0) | Measured in §13.4. This is the finding that makes the naive implementation non-compliant |
| SQLite-level residue | Scrub procedure = close every other handle on the file → `PRAGMA secure_delete=ON` → write → `optimize` → COMMIT → `VACUUM` → `PRAGMA wal_checkpoint(TRUNCATE)` → **check the pragma's return row**. Measured residue: 42 → 0 canary bytes. `secure_delete + VACUUM` **without** `optimize` still leaves 2 | Nothing else reaches all four residue classes (freelist pages, WAL frames, live FTS segments, vec chunk slots). The checkpoint **fails silently under contention** — it returns `(busy=1, …)` rather than raising, leaving a WAL full of pre-erasure images (measured, §13.5), so the return row is load-bearing and `reclaimed=False` must be reportable |
| Retrieval after erasure | The `fact_vec` and `fact_fts` rows of **every fact in the closure** are deleted, so the content can never enter a candidate pool again — not on the latest surface, not under `as_of`, not with `include_cold=True` | `Retriever` only ever ranks ids returned by the two arms and drops ids missing from `hydrate` (`retriever.py:84-88`). This is what makes "as_of never resurrects it" mechanically true, not a filter someone can forget. It holds **only over the full closure**: deleting one fact's two index rows leaves a dedup-retired duplicate of the same sentence indexed and retrievable (§5.2a) |
| Spine columns touched | `is_latest := 0`, `expired_at := <erasure time>`, `erasure_id := <ledger id>`. **`valid_at`/`valid_to` are never touched in `redact` mode**; `purge` closes `valid_to` on a *spliced-or-orphaned open referrer* only, and reports it (§5.1 rule 2) | `expired_at` is the dormant bitemporal *transaction-time* close (`schema.py:49`) — "this record left the database at T" — which is exactly what erasure is. Closing `valid_to` would assert a false claim about the world (§5.1). The purge carve-out is narrower and forced: an open retired duplicate whose splice target vanished would otherwise resurrect as a permanently open interval |
| Audit trail | Append-only `erasure` ledger table: counts, mode, timestamps, actor — **no content, no selector text** | Accountability (Art. 5(2)) without re-storing what was just erased |
| API | `Memory.forget(...) -> ErasureReport` (dry-run by default, `apply=False`, matching `Memory.maintain(apply=...)`) + `Memory.forget_namespace(...)`; `memory_forget` MCP tool on **all three** MCP surfaces; `lean-memory-forget` CLI | §7. The Python API today has *no* namespace purge at all — only the MCP tool has one (`mcp_server.py:291`) |
| Backups, snapshots, disks | **Out of scope, explicitly and loudly** | §3.4. A library that unlinks a file cannot speak for Time Machine, APFS snapshots, or SSD wear-levelling |
| Crypto-shredding | **Rejected for v1** (§11.4) | Needs a key store a local-first single-user tool has nowhere to put, and ciphertext is not indexable by FTS5/vec0 |
| Schema impact | v2 → **v3**: `ALTER TABLE fact ADD COLUMN erasure_id`, `entity`/`episode` add `erased_at` + `erasure_id`, new `erasure` table. Additive, nullable, in a `user_version < 3` branch | §8. Old files upgrade in place; the ALTERs are non-idempotent and must never enter the always-run blob (the WP10a lesson, `sqlite_store.py:125-137`) |

## 1. Map of the current implementation (what erasure must respect)

### 1.1 The spine and its sanctioned mutations

ADD-only, as it stands at v0.2.3: no row is ever deleted. The mutations that
exist are `supersede_fact` (`sqlite_store.py:234-278`), `retire_duplicate`
(`:485-520`), `invalidate_summary` (`:311-325`), `set_tier` (`:522-527`),
`merge_usage_stats` (`:529-541`), and `touch` (`:477-482`). The only deletion
anywhere in the package is the whole-namespace file unlink in `memory_clear`
(`mcp_server.py:291-334`).

The **as-of visibility predicate** reads *only* `valid_at`/`valid_to`
(`_apply_as_of`, `sqlite_store.py:402-415`; sparse mirror at `:458-462`). It
never reads `is_latest`, `tier`, `expired_at`, `invalidated_by`, `record_kind`.
WP10a's §3.1 "verb (c)" — flipping columns the predicate never reads — is
therefore as-of-invariant, and **erasure's spine writes are all verb (c)**.

### 1.2 Where user content actually lives (the erasure surface inventory)

Every content-bearing column in the namespace file, by table. This is the list
an erasure must cover; anything missing from it is a leak.

| Table (DDL) | Column | Holds |
|---|---|---|
| `episode` (`schema.py:12-19`) | `raw` | **The verbatim ingested message.** The leakiest surface: the original sentence, not just the extracted triple |
| | `source` | **Caller-controlled free text — may carry an identifier; not content-typed by the engine.** Defaulted to `'user'`, but unconstrained and plumbed through every public surface: `Memory.add(..., source=…)` (`memory.py:134,148`), `EngineGateway.add` (`console/.../engine.py:211`), `routes/data.py:32`, `routes/mcp.py:110`, `observe_mcp.py:33`. A deployment-shape-(B) host will naturally put a thread id, an email address, a Slack user, or a document URL here |
| | `t_ref`, `created_at` | timestamps (no content) |
| `entity` (`schema.py:22-30`) | `name`, `summary` | The person's/org's name; `resolved_id` links alias rows to a canonical — **but nothing ever writes it** (§1.3.10) |
| *(all six logical tables)* | `namespace` | **Caller-controlled identifier, content-adjacent.** The raw namespace string is stored in `fact`, `entity`, `episode`, `maintenance_run`, `maintenance_proposal`, in the `fact_vec` vec0 metadata column (`schema.py:73` → `fact_vec_metadatatext*`), in the proposed `erasure` table (§8), and in the **derived filename** (`memory.py:323-325`). No erasure unit below the namespace unit removes it. When callers name namespaces after people — the pattern the project's own quickstart shows (`README.md:80`, `--namespace bob`) — the subject's name survives every redaction (§3.1) |
| `fact` (`schema.py:34-60`) | `fact_text` | The standalone sentence that gets embedded and reranked |
| | `object_literal` | the literal value ("Acme", "110k") |
| | `predicate`, `subject_id`, `object_id` | slot metadata; `subject_id`/`object_id` point at `entity` |
| `fact_vec` (vec0, `schema.py:69-76`) | `embedding`, `embedding_256` | **Embeddings are personal data.** A 768-dim vector of "user has diagnosis X" is a lossy but attackable representation of that sentence |
| `fact_fts` (FTS5, `schema.py:79-82`) | `fact_text` | A **second full copy** of the text, plus its inverted index |
| `fact_derivation` (`schema.py:89-95`) | — | ids only (`summary_id`, `source_id`, `run_id`) — but the *summary fact it points at* is content (§5.2) |
| `maintenance_run` (`schema.py:99-107`) | `stats_json` | counts only (verified: `runner` writes aggregate stats) |
| `maintenance_proposal` (`schema.py:113-124`) | `payload_json` | **Verbatim `fact_text` copies**: `fact_texts` (`transforms.py:249`), `source_fact_texts` (`:347`), `fact_text` (`:423`), `summary_text` |
| | `edited_text` | human-written summary text |

Physical shadow tables created underneath the two virtual tables (enumerated
from `sqlite_master`, §13.1) — these are where bytes actually rest:

- FTS5: `fact_fts_content` (the stored text), `fact_fts_data` (the segment
  b-tree — postings), `fact_fts_docsize`, `fact_fts_idx`, `fact_fts_config`.
- vec0: `fact_vec_chunks`, `fact_vec_rowids`, `fact_vec_info`,
  `fact_vec_vector_chunks00` / `01` (the packed vectors),
  `fact_vec_metadatachunks00-02`, `fact_vec_metadatatext01` / `02`
  (the `tier` / `namespace` TEXT metadata columns).

**Outside the namespace file** (named here for honesty; see §10 non-goals):

- The console's `_events.db` sidecar holds search queries **and verbatim copies of
  every fact text returned by a console or console-MCP search**. `EngineGateway.search`
  builds `hits = [{"fact_id": …, "fact_text": rf.fact.fact_text, …}]` and passes the
  whole list into the event payload — `events.record(ns, "search", ms, {"query": query,
  …, "hits": hits})` (`console/.../engine.py:255-280`). So the exact content `forget()`
  promises to destroy has a full-text copy in a file **outside the erasure boundary**,
  once per time it was ever retrieved. Adds record only `episode_text_chars`, a length,
  not the text (`engine.py:223-232`) — the asymmetry is why this was previously
  mis-summarized as "queries only". Console-owned; the engine never touches this file.
  Because each hit carries its `fact_id`, purging the matching rows is cheap — see the
  now-load-bearing decision at §14.5.
- The MCP client's own transcript (Claude Code / Desktop conversation history).
  The sentence that produced the fact is still in the client's log. lean-memory
  cannot reach it and must not imply otherwise.

### 1.3 Operational constraints (verified, load-bearing)

1. **`PRAGMA foreign_keys=ON` on every connection** (`sqlite_store.py:78`) and
   the schema is densely self-referential: `fact.superseded_by → fact(id)`,
   `fact.invalidated_by → fact(id)`, `fact.subject_id`/`object_id → entity(id)`,
   `fact.episode_id → episode(id)`, `fact_derivation.summary_id`/`source_id →
   fact(id)`. Measured (§13.3): **all four naive `DELETE`s fail** with
   `IntegrityError: FOREIGN KEY constraint failed` — the middle of a
   supersession chain, a fact referenced by `fact_derivation`, an episode with
   live facts, an entity with live facts. Hard delete is not a one-liner; it is
   a closure problem.
2. **`PRAGMA secure_delete` defaults to 0** and `auto_vacuum` to 0 (measured).
   Deleted content stays legible in freed pages until overwritten.
3. **WAL journaling** (`sqlite_store.py:77`). Committed page images live in
   `<ns>.db-wal` until checkpoint; a clean last-connection close checkpoints and
   removes the WAL, a crash or a concurrent reader does not.
4. **Retrieval reads only what the indexes return.** Both arms produce ids;
   `Retriever` hydrates and silently drops ids with no `fact` row
   (`retriever.py:84-88`). Conversely a fact absent from `fact_vec`/`fact_fts`
   is never a candidate, on any surface, under any flag. *This is a statement
   about a fact id, not about a sentence* — a second row carrying the same text
   is a separate candidate with its own index rows, which is why the closure in
   §5.2a exists.
5. **One SQLite file per namespace** (`memory.py:123-130`, `:323-325`):
   `root/<_SAFE_NS-sanitized>.db`. Namespace erasure is a file operation.
6. **`Memory` caches open stores** (`memory.py:120`) — a file-level erase must
   pop and close the cached connection first (the pattern at
   `mcp_server.py:328-331`).
7. **Maintenance interaction.** WP10a's runner reads candidates through
   `iter_latest_facts` / `_latest_nonsummary_in_slot`, both `is_latest=1`-scoped
   (`sqlite_store.py:555-568`, `transforms.py:144`). Setting `is_latest=0` on a
   tombstone therefore removes it from every transform's candidate set for free
   — but a live maintenance run can *copy content into a proposal payload* while
   an erasure is executing (§5.3), so erasure must interlock with the lease
   exactly as `memory_clear` does (`mcp_server.py:314-326`).
8. **Console schema tripwire.** `compute_engine_schema_fingerprint`
   (`console/.../inspect_sql.py:106-114`) digests every line of `store/schema.py`
   containing "create"; the pinned value is at `:313-315` and a console test
   asserts equality. A new `CREATE TABLE erasure` trips it; bare `ALTER`s alone
   would not.
9. **`fact.object_id` is never populated today** — `_build_fact`
   (`memory.py:259-283`) sets `object_literal` only. The entity-unit selector
   must still cover `object_id` (the column is live in the schema and WP6 may
   fill it), but expect it to match nothing at v0.2.3.
10. **`entity.resolved_id` is never populated today either** — grep across `src/`
    and `console/` finds it only in the INSERT column list
    (`sqlite_store.py:191-194`), `_row_to_entity` (`:768`), the dataclass default
    (`types.py:50`) and the DDL (`schema.py:28`). `_build_fact` always constructs
    `Entity(namespace=…, name=tf.subject_name, type=None)` (`memory.py:264`), so
    the column is always NULL and the §4.2 alias closure
    `WHERE id=:eid OR resolved_id=:eid` degenerates to `id=:eid`. **Entity identity
    at v0.2.3 is exact surface-string equality**: "Alice", "alice", "Alice Chen"
    and "Dr. Chen" are four separate `entity` rows (entity-name normalization is
    an open WP4+ follow-up — "Entity case-collation policy", `workpackets.md:598-602`,
    issue [#14](https://github.com/Wuesteon/lean-memory/issues/14), which notes the
    decision is not merely code: case-insensitive lookup has real counterexamples
    like "Polish"/"polish"). This is the dormancy that most
    directly weakens the Art. 17 unit, and §4.2 mitigates it with a near-match
    warning rather than pretending the closure works.
11. **A `retire_duplicate` loser keeps its content and both index rows.**
    `retire_duplicate` (`sqlite_store.py:485-520`) flips `is_latest` on `fact` and
    `fact_vec` and re-points open sub-losers — it never touches `fact_text` and never
    deletes the `fact_fts` / `fact_vec` rows. Since DEDUP-EXACT groups by
    `normalize_text` — NFC + casefold + whitespace collapse and *nothing else*
    (`transforms.py:34-46`, deliberately value-preserving) — a loser's `fact_text`
    differs from the survivor's only in case, spacing, or Unicode form. It is the
    same sentence, still indexed, still returned by `search(is_latest_only=False)`
    and `search(as_of=T)`. Erasure must treat it as part of the content (§5.2a).
12. **An episode can exist with no facts at all.** `Memory.add` commits the episode
    carrying the verbatim message *before* extraction runs (`memory.py:148-149`) and
    returns early when the generator yields no candidates (`:152-154`); the WP11
    restatement guard likewise `continue`s past fact creation while the episode
    persists (`:201-203`). Such a row is reachable by `episode_id` and by the
    namespace unit, and by **nothing else** — no fact-derived closure can find it
    (§4.2, §5.5b).

## 2. The obligation, and the threat model

### 2.1 What Art. 17 actually asks for

The right to erasure requires the controller to **erase personal data** without
undue delay in the enumerated cases. Three properties matter for this design:

1. It attaches to *personal data* — information relating to an identified or
   identifiable natural person — not to database rows. Destroying the data while
   retaining a structural husk that identifies nobody is erasure.
2. **Anonymization is a recognized route.** Data that can no longer be
   attributed to a data subject without additional information is outside the
   regulation's scope. This is the doctrinal basis for redaction-as-erasure, and
   it is the same argument Datomic (`:db/excise`), XTDB (`ERASE`), and every
   event-sourced ledger uses.
3. It is **not absolute**: Art. 17(3) carves out legal-obligation and
   establishment-of-legal-claims grounds, which is why an *erasure ledger* that
   records "an erasure happened, of N facts, at T" is both permitted and
   required by the accountability principle (Art. 5(2)).

The pragmatic reading this design adopts: **erase the content, keep the fact
that you erased.**

### 2.2 Who erasure is promised to (two deployments, two promises)

lean-memory is a library. It has no user model, no authentication, no request
verification, and it will not acquire one in WP5. Two deployment shapes, stated
separately because they get different promises:

**(A) Local-first, single-user** — the shipped default (`lean-memory-mcp` +
Claude Code, `LM_DATA_ROOT=~/.lean_memory`). The user is simultaneously the data
subject, the controller, and the person holding the disk. "Erasure" here means:
*after this call returns, the content is not recoverable by opening the
namespace file with SQLite or a hex editor.* The realistic adversaries are
later readers of the same file — device resale, a stolen backup, a shared
laptop, a support bundle, an accidental `git add`. Against those, the §6.3
scrub procedure is a real and verifiable guarantee (§13.5 measures it).

**(B) Embedded in a host application** (multi-user server, agent platform). The
host app is the **controller**; lean-memory is a processor-shaped dependency.
Erasure is promised *to the host app*, about *the namespace file the call names*.
The host app owns: authenticating the request, mapping a data subject to
namespaces (one subject may span many; one namespace may hold facts about many
subjects), propagating erasure to its own copies, logs, and caches, and its
backups. lean-memory will not guess any of that. WP6 scoping
(`user_id`/`agent_id`/`run_id`) may later make "erase everything about subject
S across namespaces" expressible; today it is not, and the API must not pretend
it is (§10).

### 2.3 What is **not** in the threat model

- An adversary with **prior** read access, who already copied the file or the
  content. Erasure is not retroactive.
- **Backups, snapshots, replicas** — out of scope by explicit decision (§3.4).
- **The physical medium.** `secure_delete` overwrites logical pages; SSD
  wear-levelling, copy-on-write filesystems (APFS/btrfs/ZFS), and journaled
  filesystems can retain the old blocks regardless. Whole-disk encryption is the
  only real answer and is the deployer's job.
- **Process memory, swap, core dumps, the OS page cache.**
- **The LLM client's transcript**, and any model that was fine-tuned or
  cached on the data. The fact came from a conversation the client still has.
- **Re-ingestion.** Nothing stops `Memory.add()` from writing the same sentence
  again tomorrow. Preventing that would require retaining a fingerprint of the
  erased content — retaining what we promised to destroy (§11.5).

## 3. Design principles

### 3.1 Redaction is erasure; the tombstone is not personal data — *at the right granularity*

A redacted fact row retains: `id`, `namespace`, `subject_id`, `predicate`,
`valid_at`, `valid_to`, `superseded_by`, `invalidated_by`, `is_latest=0`,
`expired_at`, `episode_id`, `record_kind`, `created_at`/`ingested_at`,
`erasure_id`. It retains **no content**.

Be honest about what that husk still says, because it depends on the unit:

- **Entity unit or namespace unit** — the subject's `entity.name` is itself
  redacted (or the file is gone). What survives is an anonymous skeleton:
  timestamps and pointers among opaque ids that name nobody. This is a strong
  anonymization claim — **but it is conditional on the namespace name not being
  an identifier.** The raw namespace string survives entity-unit redaction in all
  six logical tables, in the vec0 metadata column, in the `erasure` ledger row, and
  in the filename itself (§1.2). `forget(entity="Bob Smith")` inside a namespace
  called `bob-smith` leaves a skeleton that names Bob Smith on every row, and §6.1's
  argument 1 does not hold there. Two mitigations, both specified: the API docs and
  README recommend **opaque namespace ids** (a uuid or a salted hash, with the
  human-readable mapping held by the host app, §2.2(B)); and the entity unit emits a
  warning when a resolved entity name appears — case-insensitively, after the same
  `_SAFE_NS` sanitization the path uses — inside the namespace string, pointing the
  caller at `forget_namespace` as the erasure that actually removes it. Namespace-unit
  erasure has no such caveat: the file, its name, and the ledger inside it are gone.
- **Single-fact unit** — the subject entity *survives* (other facts about them
  remain, by the caller's own intent). The tombstone therefore still discloses:
  *a record existed about this named person, in this predicate slot, over this
  time interval, and was erased at T.* `predicate` is a real disclosure —
  a `diagnosis` slot leaks a category even with the value gone.

**Position:** this is acceptable and should be documented rather than
engineered away, because single-fact erasure is almost never an Art. 17 request
— it is a *correction* ("that memory is wrong, drop it"). Subject-level erasure
is the Art. 17 shape, and at that granularity redaction genuinely anonymizes.
Callers who need even the slot metadata gone have `mode="purge"` (§6.2).

### 3.2 Erasure is a transaction-time event, not a world-time one

Erasure sets `expired_at` (the bitemporal *ingest-axis* close: "this record left
the database at T") and never touches `valid_at`/`valid_to` (the *world-axis*:
"this was true from X to Y"). Closing `valid_to` at erasure time would assert
that the person stopped working at Acme when they filed a deletion request —
a fabrication.

*One carve-out, in `purge` mode only:* when a purge splices or orphans a referrer
that was still **open** (`valid_to IS NULL` — the shape `retire_duplicate` leaves
behind), that referrer's `valid_to` is closed at the erasure time (§5.1 rule 2).
This is not a claim about the world so much as the least-bad repair of one: the row
is a dedup-retired duplicate whose survivor no longer exists, and leaving it open
resurrects it as a permanently open interval on the `is_latest_only=False` as-of
surface. Redaction never needs the carve-out, because it never removes the splice
target. The report and the ledger both name every row this touched. `expired_at` and `invalidated_by` have been declared, round-
tripped, and read by nothing since Phase 0 (`schema.py:49-50`; catalogued as a
dormant "provenance seam" in the WP10a spec §1.2). This is the use they were
reserved for.

Consequence, stated plainly: a redacted fact that was `is_latest=1` and never
superseded becomes a **permanently open interval** (`valid_to IS NULL`,
`is_latest=0`) — the same shape WP10a's duplicate-cascade was invented to
repair. It is harmless *here* because the row carries no content and has no
index rows: nothing can retrieve it, and `find_latest_in_slot` no longer returns
it, so a later `add()` on that slot simply writes a fresh fact. The only visible
trace is in WP4's `history()` / `get_all(is_latest_only=False)`, which is
exactly where it should be visible.

### 3.3 Derived data is erased, not rebuilt

vec0 and FTS5 are derived from `fact.fact_text`; `record_kind='summary'` facts
are derived from their sources via `fact_derivation`. WP10a's §3.2 declined to
delete index rows because as-of retrieval runs over them. **WP5 inverts that
for erased facts only**: the whole point is that the content stops being
retrievable, so the index rows go, and their absence *is* the enforcement
mechanism (§1.3.4). Everything WP10a said about *non-erased* facts stands
unchanged.

### 3.4 Backups are out of scope — and the doc says so in the same breath as the guarantee

Every guarantee sentence in the API docs, the README, and the MCP tool
description must be immediately followed by the backup carve-out. A guarantee
stated alone will be read as absolute. The wording is fixed in §7.5.

### 3.5 Dry-run first, everywhere

Erasure is irreversible and cascading; its blast radius (how many summaries,
which episodes, which proposals) is not obvious from the selector. Every surface
computes and returns a full `ErasureReport` **before** writing, and defaults to
not writing — matching the repo's existing convention (`Memory.maintain(apply=False)`,
`memory_maintenance_run(apply=False)`, `lean-memory-maintain` dry-run default).

## 4. Scope — the four erasure units, in schema terms

### 4.1 Unit `fact` — one fact and its derivatives

*Selector:* `fact.id = :fact_id`.

*Direct set:* that row. *Closure* (§5): the transitive `fact_derivation` summary
closure **unioned with the transitive dedup-retired-duplicate closure (§5.2a)**,
taken jointly to fixpoint; `fact_vec`/`fact_fts` rows for every fact in the
closure; `maintenance_proposal` rows referencing any of them; the backing
`episode` iff orphaned (§5.5).

*Not included:* the subject `entity` (other facts reference it), sibling facts
from the same episode, the supersession chain's other members (only the named
fact is erased; §5.1 handles the pointers).

### 4.2 Unit `entity` — a subject and everything recorded about them

*Selector:* resolve first — `SELECT id FROM entity WHERE namespace=:ns AND
name=:name AND IFNULL(type,'')=IFNULL(:type,'')` (the `ix_entity_lookup` path
`upsert_entity` already uses, `sqlite_store.py:183-188`) — then take the alias
closure `WHERE id=:eid OR resolved_id=:eid`.

**The alias closure is dormant at v0.2.3** (§1.3.10): nothing ever writes
`resolved_id`, so the clause degenerates to `id=:eid` and resolution is exact
surface-string equality. The selector keeps the clause (WP4+ entity normalization
will fill the column) but the design must not *rely* on it. Compensating
behavior, specified here rather than left to the implementer:

- The dry-run report echoes the resolved `entity` rows **with their names**, not
  just their ids, so the caller sees exactly who was matched.
- The report additionally lists, as `warnings`, every other `entity` row in the
  namespace whose `name` is a **case-insensitive equal or substring near-match**
  of the requested name — "Alice", "alice", "Alice Chen", "Dr. Alice Chen" — with
  its id and its live fact count, so the caller can re-run the unit per alias. The
  engine will not merge them on its own: a substring match is a heuristic, and
  auto-erasing "Alice Chen" because the caller asked for "Alice" would be
  over-erasure about a possibly different person.

*Direct set:* those `entity` rows (redact `name`, `summary`; keep the row as an
FK target) **plus** every fact `WHERE subject_id IN (:eids) OR object_id IN
(:eids)`. (`object_id` matches nothing at v0.2.3 — §1.3.9 — include it anyway.)

*Closure:* as §4.1, unioned over that fact set. Episodes are erased under the
orphan rule (§5.5), which for a single-subject namespace usually means all of
them, and for a shared namespace usually means few — and the report says which
(§5.5 is the honest-disclosure case).

*What this unit cannot reach:* an **episode that produced no fact** (§1.3.12).
The closure is built from facts, so a message that extracted nothing — a routine
outcome at GLiNER `DEFAULT_THRESHOLD=0.4`, and the guaranteed outcome for a WP11
restatement — is invisible to it, even when the message is entirely about the named
subject. Two responses, both specified:

- The report carries `episodes_unreachable: int` — the count of `episode` rows in
  the namespace that no surviving-or-erased fact points at — plus the
  `episodes_unreachable_note` warning whenever it is non-zero. The count is cheap:
  `SELECT COUNT(*) FROM episode e WHERE e.namespace=:ns AND NOT EXISTS
  (SELECT 1 FROM fact f WHERE f.episode_id = e.id)`.
- Opt-in `sweep_episodes=True` additionally runs a `LIKE` scan of `episode.raw`
  over the resolved entity names (and the near-matches above) across those
  fact-less episodes, and lists the hits in the report **as candidates for
  confirmation**. Under `apply=True` the swept episodes are erased; under the
  default dry run they are merely listed. It is opt-in and never the default
  because a substring scan over raw text will match episodes about other people
  who share a name fragment — over-erasure again.

*This is the Art. 17 unit* — with the two caveats above stated in the docstring,
not buried here. Callers pass `entity="Alice Chen"`; the dry-run report echoes the
resolved ids, the resolved names, the near-match aliases, the counts, and the
unreachable-episode count, so the caller can confirm the right person was resolved,
and see what the unit will *not* reach, before applying.

### 4.3 Unit `episode` — raw text only

*Selector:* `episode.id = :episode_id`.

*Action:* `raw := ''`, **`source := NULL`**, `erased_at`, `erasure_id`. The row is
kept because `fact.episode_id` is `NOT NULL REFERENCES episode(id)`
(`schema.py:58`) — every fact needs a provenance parent. Facts extracted from the
episode are **not** erased by default: one episode commonly yields facts about
several subjects, and the caller may be erasing a transcript while keeping the
extracted structure.

*Why `source` is redacted with `raw`:* it is unconstrained caller-supplied free
text, not engine-typed provenance (§1.2), and a host application in deployment
shape (B) will put a thread id, an email address, or a document URL there. The
same `source := NULL` applies to the episodes reached by the entity unit's orphan
rule (§5.5). `t_ref` and `created_at` are timestamps and are kept — they carry the
tombstone's meaning. Callers who deliberately store engine-meaningful provenance
in `source` (`'user'`, `'maintenance'` — the values the package itself writes) lose
only that label, on rows whose content is gone anyway.

*Reachability:* this unit is the **only** way to erase an episode that produced no
fact, short of the namespace unit (§1.3.12, §4.2, §5.5b). That makes `episode_id`
a first-class selector on every surface, including MCP (§7.3).

### 4.4 Unit `namespace` — file unlink

*Selector:* the namespace name → `root/<_SAFE_NS.sub("_", ns) or "default">.db`
(`memory.py:323-325`).

*Action:* pop and close the cached store (`memory.py:120`), then unlink `.db`,
`.db-wal`, `.db-shm`. This already exists behind the MCP tool
(`mcp_server.py:291-334`) with a maintenance-lease refusal and a documented
residual race (WP10a §7.3); WP5 **keeps that behavior verbatim** and adds the
missing Python-API twin — today `Memory` has no way to purge a namespace at all.

*Honest note:* unlink is the strongest erasure the engine offers and it still
does not scrub the disk. Also: the erasure ledger lives inside the file, so a
namespace purge destroys its own evidence. Deployments needing durable proof of
erasure must record it outside (host-app concern, §10).

### 4.5 Not a unit (deferred to WP6)

"Every fact matching a predicate", "every fact in a date range", "everything
about subject S across all namespaces". The first two are cheap to add later on
top of the same machinery; the third needs WP6 scoping to even be expressible.
WP5 ships four units, not a query language.

## 5. Cascade semantics

Everything below runs inside a single `store.batch()` window
(`sqlite_store.py:89-106`) except the physical reclaim (§6.3), which cannot run
in a transaction.

### 5.1 Supersession chains

Given `A --superseded_by--> B --superseded_by--> C` and an erasure of **B**:

**Redact (default).** Nothing structural changes. `A.superseded_by` still points
at B; `B.superseded_by` still points at C. WP4's `history()` walks the chain
oldest→newest and renders B as `[erased 2026-08-06]` with `erasure_id` set. The
chain is *complete and truthful*: it records that a fact stood here, when it was
believed, and that its content was erased. Nothing to cascade.

**Purge.** SQLite refuses `DELETE FROM fact WHERE id=B` while `A.superseded_by =
B` (§13.3, measured `IntegrityError`). Two options were considered:

- *Widen the delete to the referencing closure* — deleting B forces deleting A,
  and A's referrers, and any fact_derivation sources. That erases facts the
  caller did not ask to erase, possibly about other people. **Rejected**:
  over-erasure is its own compliance failure and a data-loss bug.
- *Splice, then delete* — re-point B's referrers, then delete B. **Adopted**, with
  the exact rule below. Note up front what it is **not**: it is *not* the
  re-pointing `retire_duplicate` performs. That helper re-points only rows
  `WHERE superseded_by=:loser AND valid_to IS NULL`, and re-points them to a
  survivor it has first **resolved to its live `is_latest=1` canonical**
  (`sqlite_store.py:501-519`), precisely because `supersede_fact`'s duplicate
  cascade is deliberately single-level: *"A SINGLE level suffices because
  retire_duplicate's chain invariant keeps every open duplicate pointing DIRECTLY
  at the live survivor"* (`sqlite_store.py:243-254`, cascade at `:265-276`). A naive
  `UPDATE fact SET superseded_by=:c WHERE superseded_by=:b` breaks that invariant
  two ways — C may itself be `is_latest=0`, and an open retired duplicate
  (`valid_to IS NULL`) re-pointed at a non-canonical row is one
  `supersede_fact` away from becoming a **permanently open interval on the
  `is_latest_only=False` as-of surface**: the "empirically demonstrated wrong
  answer" the WP10a duplicate-cascade (§14 of that spec) exists to prevent.

  **The purge splice rule** (three parts, all required):

  1. **Resolve the splice target to the live canonical.** Starting from
     `B.superseded_by`, walk `superseded_by` while the row is `is_latest=0` and has
     a non-NULL `superseded_by`; the terminal row is the target `C*`. This mirrors
     `retire_duplicate` step (i), generalized from depth 1 to a bounded walk with a
     visited set (the chain can be longer than one hop once purges have spliced it).
     Referrers are re-pointed to `C*`, not to `B.superseded_by` literally.
  2. **Close any referrer the splice or the purge leaves open.** For every *surviving*
     referrer with `valid_to IS NULL` that is re-pointed in (1) or orphaned by a
     head-of-chain purge, set `valid_to := <erasure time>`. Ordering: the §5.2a
     closure runs first and **removes** B's own dedup-retired duplicates, so rule (2)
     applies to what is left — an open referrer outside the closure, e.g. one whose
     `superseded_by` points at B via `invalidated_by`-adjacent history or a chain
     spliced by an earlier purge. Without it such a row outlives the cascade that
     used to reach it and resurfaces as a permanently open interval. The write is
     verb-(b)-shaped (it touches the as-of predicate's columns) and is therefore
     *reported*: the ledger's `splices_json` records it and the `ErasureReport`'s
     `intervals_closed` names every row, because it is the one place erasure changes
     a world-time interval — justified because the alternative is a row asserting an
     interval that never ended.
  3. **The same for `invalidated_by`**, which has no valid_to coupling and so needs
     only the re-point (or NULL on head-of-chain).

  Head-of-chain purge (`B.superseded_by IS NULL`): referrers get
  `superseded_by := NULL`, plus rule (2) — a referrer that was already closed keeps
  its `valid_to`; one that was open (a dedup-retired duplicate of B) is closed at
  the erasure time. `is_latest` is *not* reopened (an erasure must never resurrect a
  fact onto the current surface).

  A spliced chain reads `A --superseded_by--> C*`, which is a *false* statement:
  A was replaced by B, not C*. The erasure ledger records `(a_id, c*_id,
  splice_count)` so an auditor can see that a splice happened at this point —
  but not what was removed. This dishonesty is inherent to true removal and is
  the core reason purge is not the default (§6.2).

### 5.2 `fact_derivation` lineage and maintenance summaries

`fact_derivation(summary_id, source_id, run_id, created_at)` (`schema.py:89-95`,
index `ix_derivation_source` on `source_id`) links a WP10a summary fact to the
facts it distilled. A summary's `fact_text` is generated *from* its sources —
extractively (top-salience source texts, verbatim) by the default stub, or
abstractively by the Ollama summarizer. **Erasing a source without erasing its
summaries leaves the content in the store, sometimes literally.**

*Decision: cascade, transitively, by default* (`cascade_summaries=True`).

Closure computation: start with the erased fact ids; repeatedly
`SELECT DISTINCT summary_id FROM fact_derivation WHERE source_id IN (...)`
until fixpoint, with a visited set (a summary can itself be a source of a later
summary — WP10a §4.3 step 6 supersedes an old summary with a new one, and
nothing forbids chained derivation). Cycle-safe by construction.

**Implementation gotcha:** the existing helper `find_summaries_derived_from`
(`sqlite_store.py:295-309`) filters `AND f.is_latest = 1` — correct for the
staleness cascade, **wrong for erasure**: a summary that was already invalidated
still holds the content on disk. WP5-impl needs an unfiltered variant
(`find_all_summaries_derived_from`) or an `include_retired: bool` parameter.
Reusing the existing helper unchanged is a silent leak, and §9 pins a test on it.

*Trade-off, accepted:* erasing one source may erase a summary distilled from
twenty facts, nineteen of which the caller wanted to keep. The alternative —
re-summarizing without the erased source — requires a model call inside an
erasure path (forbidden: offline-by-default, and model work inside a batch
window is banned by WP10a §7.1). The report lists collateral summaries; the next
maintenance run naturally re-stages a fresh SUMMARIZE proposal over the
surviving sources (WP10a §12.2), so the loss is temporary and reversible by
re-derivation.

*Also cascaded:* the **maintenance episode** each applied summary inserted
(`lifecycle.py:393-395`, `source='maintenance'`). Its `raw` is an ids-only JSON
report (verified — no fact text), so it carries no content, but it is orphaned
once its summary is purged and falls under the §5.5 rule.

### 5.2a Dedup-retired duplicates — the byte-equivalent copy

The `fact_derivation` closure is the *distilled* copy of a fact's content. There is
a second, blunter copy the closure does not reach: the losers of a WP10a
**DEDUP-EXACT** merge.

`retire_duplicate` (`sqlite_store.py:485-520`) flips `is_latest=0` and sets
`superseded_by=<survivor>` on the `fact` row and `is_latest=0` on the `fact_vec`
row — nothing else. The loser keeps its `fact_text`, its `object_literal`, its
`fact_fts` row (postings and all), and its `fact_vec` row (vector bytes and all).
That is correct for dedup: WP10a §3.2 declined to delete index rows because as-of
retrieval runs over them.

It is a **content leak** for erasure, because of what "exact duplicate" means here.
`normalize_text` (`transforms.py:34-46`) is *value-preserving by design*: NFC +
casefold + whitespace collapse, "and NOTHING else. Never stemming, never synonyms."
Two facts land in the same DEDUP-EXACT group **iff they are the same sentence
written differently** — different case, different spacing, a different Unicode
normal form. So erasing only the survivor leaves, on disk and in both indexes, a
sentence that differs from the erased one by nothing a reader would notice, still
returned by `search(is_latest_only=False)` and by `search(as_of=T)` for T inside
its interval. This defeats §1.3.4 / §5.4 — deleting *the survivor's* two index rows
does not make the content unreachable when a second indexed row says the same
thing.

*Decision: cascade, to fixpoint, in both modes, **with no opt-out flag.***
`cascade_summaries` and `cascade_episodes` exist because a summary and an episode are
*related* content a caller might legitimately want to keep; a DEDUP-EXACT loser is
**the same value**, and a `cascade_duplicates=False` would be a switch whose only
effect is to make `forget()` silently not forget. It is unconditional.

Closure computation: alongside the `fact_derivation` step, repeatedly

```sql
SELECT id FROM fact WHERE superseded_by IN (:closure) AND valid_to IS NULL
```

until fixpoint, sharing the derivation closure's visited set (the two closures
interleave — a retired duplicate can be a derivation source, and a summary can have
been dedup-retired).

**Why that exact predicate, and why it is not over-erasure.** The `valid_to IS NULL`
conjunct is what distinguishes a dedup-retired duplicate from a genuinely superseded
predecessor. `supersede_fact` always sets `valid_to` when it closes a fact
(`sqlite_store.py:257-260`); `retire_duplicate` **deliberately never does** — that is
the documented "verb (c)" property that keeps the as-of surface unchanged at dedup
time, and the reason `supersede_fact` needs its duplicate-cascade at all
(`sqlite_store.py:243-254`). So `superseded_by IS NOT NULL AND valid_to IS NULL` is
precisely WP10a's "open retired duplicate" shape. A predecessor that was superseded
because the world changed — "salary 100k" replaced by "salary 110k" — has a closed
`valid_to` and is **not** selected: it is a different value, the caller did not ask
to erase it, and erasing it would be over-erasure and a bitemporal-history bug.

*Interaction with purge:* the cascade removes these rows outright, so §5.1's splice
rule and this closure agree — a retired duplicate of a purged fact is itself purged
rather than re-pointed. §5.1 rule (2) covers the residual case where a splice or a
head-of-chain purge leaves some *other* open referrer behind.

§9.17 pins the leak directly: DEDUP-EXACT-merge two casing variants of one sentence,
erase the survivor, and assert both zero canary bytes in the raw file and that
`search(is_latest_only=False)` returns nothing.

### 5.3 Maintenance proposals — the copy nobody expects

`maintenance_proposal.payload_json` stores **verbatim fact text**:
`{"fact_texts": {id: text, ...}}` for dedup_near (`transforms.py:246-263`),
`{"source_fact_texts": {...}, "summary_text": ...}` for summarize (`:344-356`),
`{"fact_text": ...}` for evict (`:421-437`); plus `edited_text` from a human
edit-approve.

**A proposal row is never deleted.** `grep -rn 'DELETE FROM' src/` returns *nothing
anywhere in the package* (verified at v0.2.3): `cas_decide_proposal`
(`sqlite_store.py:674-695`) and `expire_proposal` (`:707-720`) only flip `status`
and `expiry_reason`. "Expiry" is a status, not a reaping. So `maintenance_proposal`
is **append-only and grows monotonically with every maintenance run**, and every row
keeps its full `payload_json` and `edited_text` indefinitely. Two consequences, one
for this section and one beyond it:

- *For erasure:* the table is not small, and the detection scan below is not
  bounded by the per-run budget.
- *Independently of erasure:* verbatim `fact_text` for facts **nobody asked to
  erase** accumulates without bound and without a retention policy, in a table whose
  stated purpose is a 30-day review queue. That is a data-minimisation problem in its
  own right; it is raised as §14.8 rather than solved here, because fixing it is a
  WP10a-lane change, not a WP5 one.

*Decision:* any proposal whose payload references an erased fact id is itself
erased — `payload_json := '{}'`, `edited_text := NULL`, plus
`status := 'expired'`, `expiry_reason := 'erased'` if still pending (a new
reason value alongside `'timeout'`/`'stale_target'`; the column is free text in
the DDL, so this is additive). The row is kept as a ledger record in redact
mode, deleted in purge mode (no FK points at it).

*Ordering matters:* expire the proposals **first**, inside the same batch, so a
concurrent review-decide cannot apply a proposal that re-inserts erased content
as a new summary fact between the two writes. Combined with the lease interlock
(§5.7) this closes the race.

*Detection:* payload ids are inside JSON. Two options — scan `payload_json` with
`LIKE '%'||:fact_id||'%'` over the namespace's proposals, or add a
`proposal_fact(proposal_id, fact_id)` join table written at stage time and indexed
on `fact_id`.

The scan is **O(all proposals ever staged in this namespace)**, unindexed, with one
full-table pass per erased fact id — *not* O(`proposal_budget_per_run`). The earlier
draft of this section justified it with "bounded by 50/run, and proposals expire";
both halves were wrong, per the append-only finding above. The honest cost model:
`n_rows × |closure|` substring comparisons over a table that never shrinks.

**Adopted anyway: the LIKE scan**, with the cost accepted explicitly rather than
denied. Reasons: it needs no schema change and no v3 migration beyond the ones §8
already carries; the closure for a realistic erasure is small (single fact, or one
subject's facts); and a namespace accumulating enough proposals for the scan to hurt
has the §14.8 retention problem first, which is where the fix belongs. Two guards
are specified instead of a join table: the scan runs **once per erasure over the
whole closure** — a single pass building `id → proposals` rather than one pass per
id — and the `ErasureReport` records `proposals_scanned` so an operator watching the
number climb has the signal before the latency does. If §14.8 lands a retention
policy, revisit; if it does not and the scan becomes the bottleneck, the join table
is the fallback, additive and independently migratable.

§9.8 pins a test that a proposal referencing an erased fact is found and cleared.

### 5.4 FTS5 and vec0 — the remnants that survive a `DELETE`

*Measured (§13.4), and this is the finding that makes a naive implementation
non-compliant:*

- `DELETE FROM fact_fts WHERE fact_id=?` succeeds and removes the row from
  `fact_fts_content` — but the canary term appeared in **more**
  `fact_fts_data` blocks afterwards (1 → 2), not fewer. FTS5 deletion is
  *logical*: it appends a delete-marker segment (which itself contains the term)
  while the original posting stays in a live segment. Those postings sit in
  **live rows**, so neither `secure_delete` nor `VACUUM` touches them —
  `secure_delete + VACUUM` without a merge still left 2 canary occurrences in
  the file (§13.5). Running `INSERT INTO fact_fts(fact_fts) VALUES('optimize')`
  merges the segments and drops them: 2 → 0.
  `('rebuild')` also works (rebuilds from `fact_fts_content`) but is the
  sledgehammer; **`optimize` is adopted**. `('integrity-check')` passes after
  both.
- `DELETE FROM fact_vec WHERE fact_id=?` succeeds on sqlite-vec 0.1.9 and the
  deleted row's exact serialized vector bytes were **no longer present anywhere
  in the file**, even before VACUUM (the vector-chunk blob is rewritten in
  place). KNN and the `fact_fts` MATCH path keep working afterwards (79 rows,
  `integrity-check` clean). *We do not lean on this*: the §6.3 guarantee comes
  from `secure_delete` + `VACUUM`, not from vec0's internals, which are
  unversioned implementation detail. §9 pins a residue test so an upstream
  change turns the suite red rather than silently leaking.
- Orphaned index rows (index row present, spine row gone) are *retrieval-safe*
  — `Retriever` drops unhydratable ids (`retriever.py:87`) — but they are a
  **content leak**, so index deletion is mandatory, not optional.

### 5.5 Episode and entity orphan rules

- **Episode.** Erase `episode.raw` **and `episode.source`** (§4.3) iff no surviving
  fact still points at it:
  `SELECT COUNT(*) FROM fact WHERE episode_id=:eid AND erasure_id IS NULL` = 0.
  Default `cascade_episodes=True` for every unit. When the count is non-zero the
  raw text is **kept** and the report emits a warning naming the episode and the
  number of retained facts referencing it: *"the original message backing this
  fact also backs N retained facts and was not erased."* This is the most
  important honest disclosure in the design — a caller who erases one fact and
  assumes the sentence is gone is wrong, and the report must tell them so.
- **Entity.** Only redacted as part of the entity unit (§4.2), never as a
  side effect of fact erasure — other facts reference it. Alias rows
  (`resolved_id`) are included — though nothing populates that column at v0.2.3, so
  in practice the set is the single resolved row (§1.3.10).

### 5.5b The episode the orphan rule never visits

The orphan rule above is a *filter on episodes the closure already reached* — it is
reached through `fact.episode_id`. An episode that **produced no fact** is therefore
never enumerated by it, and the §5.5 warning, the design's most important honest
disclosure, **does not fire** for exactly the rows where the raw message survives
most completely.

This is not a rare shape (§1.3.12). `Memory.add` commits the episode with the
verbatim message *before* extraction (`memory.py:148-149`), then returns early when
the generator yields nothing (`:152-154`); the WP11 restatement guard skips fact
creation while the episode persists (`:201-203`). At the frozen GLiNER
`DEFAULT_THRESHOLD=0.4` (~3.67 facts/turn) with ordinary conversational repetition,
a substantial fraction of ingested messages leave a fact-less `episode.raw`.

Stated plainly, in the words §4.2, §7.5 and the `forget` docstring all use:

> **An episode that produced no fact can only be erased by id, or by erasing the
> namespace. `forget(entity=…)` will not find it.**

The specified behavior, rather than silence:

- Every fact-derived unit (`fact`, `entity`) computes
  `episodes_unreachable` — the count of fact-less `episode` rows in the namespace —
  and, when it is non-zero, appends the `episodes_unreachable_note` warning to
  `ErasureReport.warnings` carrying that count and the sentence above.
- The entity unit additionally offers opt-in `sweep_episodes=True` (§4.2): a `LIKE`
  scan of those episodes' `raw` over the resolved entity names and their near-match
  aliases, listed in the report as candidates. Off by default — a name-substring scan
  over raw text over-erases.
- §9.18 pins it: ingest a message that yields zero facts, run the entity unit, assert
  the raw text is still present **and** that the report warns.

### 5.6 What is deliberately *not* cascaded

- **Sibling facts from the same episode.** Erasing one fact never erases another
  just because they were extracted from the same message. **This does not extend to
  dedup-retired duplicates** (§5.2a), which are *not* siblings: a sibling is a
  different fact the extractor happened to pull from the same sentence, while a
  DEDUP-EXACT loser is a row the engine itself determined carries the *same value*,
  differing only in case, spacing, or Unicode form. Those cascade.
- **Superseded predecessors with a closed `valid_to`.** A fact that was replaced
  because the world changed is a different value; erasing it because its successor
  was erased is over-erasure and destroys bitemporal history the caller did not ask
  about. The §5.2a predicate's `valid_to IS NULL` conjunct is exactly what keeps
  these out.
- **`fact_derivation` rows themselves** in redact mode: they are ids-only and
  keep the lineage graph walkable for WP4. Purge deletes them (their FK targets
  are going away).
- **`maintenance_run` rows**: counts and hashes, no content.
- **Non-erased facts' index rows, tiers, or usage stats** — erasure touches
  nothing outside its closure. WP10a's as-of invariance argument for every other
  fact is untouched.

### 5.7 Interlocks

- **Maintenance lease.** Erasure refuses while a live-heartbeat maintenance
  lease exists for the namespace (reusing `live_lease_is_fresh`, the exact
  `memory_clear` precedent at `mcp_server.py:314-326`), and a maintenance run
  started during an erasure skips the namespace at its next batch boundary. A
  run could otherwise copy `fact_text` into a fresh proposal payload after the
  fact row was redacted.
- **Cached connections — and the reclaim requires *sole* access.** Fact / entity /
  episode erasure runs on a dedicated 5000 ms-budget store
  (`Memory._maintenance_store`, `memory.py:327-337`) so the serving connection's
  1500 ms budget is untouched, matching WP10a §7.1. But that leaves the cached
  serving connection (`Memory._stores`, `memory.py:120`) open on the same file for
  the process lifetime, and the console gateway or a `lean-memory-maintain` process
  can hold a third — and **a concurrent reader silently defeats the WAL truncation**
  (§6.3, measured §13.5). So: `Memory._stores.pop(namespace)` + `close()` runs
  **before *any* reclaim**, not only before `forget_namespace`. Out-of-process
  handles cannot be closed from here; that is what the checked return row and the
  `reclaimed=False` report are for.
- **`VACUUM` cannot run inside a transaction** (measured: `OperationalError:
  cannot VACUUM from within a transaction`) — it runs after the batch commits,
  on a connection with no open implicit transaction. Note the asymmetry that makes
  step 6 of §6.3 necessary: `VACUUM` under contention *raises*, while
  `wal_checkpoint(TRUNCATE)` under contention *returns a busy row*.

## 6. Redaction vs. true row removal — the position, defended

### 6.1 The position

**Redaction is the default and the recommended mode. True row removal exists,
is explicit, and is documented as the weaker-of-the-two-honest-options.**

Four independent arguments:

1. **Legal sufficiency.** Erasure attaches to personal data. Redaction destroys
   100% of the content and, at the entity/namespace granularity, leaves a
   skeleton that identifies nobody (§3.1). This is the same posture taken by
   Datomic excision, XTDB `ERASE`, and standard event-sourcing practice; it is
   not a novel legal theory. *The premise this argument rests on is conditional:*
   the skeleton identifies nobody **only if the namespace name is not itself an
   identifier** (§1.2, §3.1). Where it is — `--namespace bob` — entity-unit
   redaction leaves the subject named on every surviving row and this argument
   does not carry; the answer there is `forget_namespace`, not a better
   redaction. The recommendation of opaque namespace ids is therefore part of the
   legal posture, not a stylistic aside.
2. **Referential integrity is a *separate* concern from erasure, and the engine
   already enforces it.** Measured: every naive hard delete on this schema fails
   on a foreign key (§13.3). Honoring an erasure by *breaking* the database — or
   by turning FKs off and leaving pointers dangling, which
   `PRAGMA foreign_key_check` then reports as a violation (measured) — trades a
   privacy fix for a corruption bug. Redaction satisfies the legal obligation
   *without* touching integrity: the two concerns become independent instead of
   in tension. That independence is the whole design.
3. **Honesty of the audit chain.** The product's claim is *auditable* memory.
   Purge forges history: after a splice, the chain asserts A was replaced by C.
   Redaction says "a fact stood here; it was erased on 2026-08-06" — which is
   both true and more useful to the person auditing.
4. **Blast radius.** Redaction's closure is content; purge's closure is
   structure, and structure is shared. Purge either over-erases or splices; both
   are worse defaults.

The counter-argument, stated fairly: a data subject who asks for erasure may
reasonably object that a row *about them* still exists, and a regulator might
read the retained metadata (existence, timing, predicate slot, subject pointer)
as personal data at the single-fact granularity. §3.1 concedes exactly this.
The response is not to argue — it is to ship `mode="purge"` and let the
controller choose, while defaulting to the mode that does not corrupt the store.

### 6.2 What each mode actually does

| | `mode="redact"` (default) | `mode="purge"` |
|---|---|---|
| `fact` row | kept; `fact_text=''`, `object_literal=NULL`, `object_id=NULL`, `is_latest=0`, `expired_at`, `erasure_id` | `DELETE`d after chain splice (§5.1) |
| dedup-retired duplicates of it | in the closure; redacted identically (§5.2a) | in the closure; deleted |
| `episode.raw`, `episode.source` | `''` / `NULL` (orphan rule) | same; row deleted only if no fact references it |
| `entity.name`/`summary` | `''` / `NULL` | row deleted only if no fact references it |
| `fact.namespace`, `episode.namespace`, … | **kept** — including in the filename (§1.2, §3.1) | **kept** — only `forget_namespace` removes it |
| `fact_vec`, `fact_fts` | rows `DELETE`d + `optimize` | identical |
| `fact_derivation` | kept (ids only) | rows for removed facts `DELETE`d |
| `maintenance_proposal` | `payload_json='{}'`, expired | row `DELETE`d |
| supersession chain | intact through the tombstone | spliced to the live canonical; open referrers closed; ledger records both (§5.1) |
| WP4 `history()` | shows an `[erased]` entry in place | shows a chain with a gap |
| as-of predicate | unchanged (`valid_at`/`valid_to` untouched) | the interval disappears from past windows |
| retrievable? | **no** (no index rows) | **no** (no row at all) |
| content on disk after §6.3 | **none** (measured) | **none** (measured) |
| FK integrity | preserved | preserved *only* via splice |

Both modes deliver the same physical guarantee about content bytes. They differ
in what structural metadata survives — which is a policy choice, correctly the
caller's.

### 6.3 The physical scrub procedure (and its honest limits)

The measured sequence (§13.5), in order — order is load-bearing:

```
0. Memory._stores.pop(ns).close()        -- drop the cached SERVING connection first;
                                         --   a concurrent reader silently defeats step 5
1. PRAGMA secure_delete = ON             -- on the erasure connection, BEFORE any write
2. <batch(): redact/delete rows; DELETE FROM fact_fts/fact_vec;
             INSERT INTO fact_fts(fact_fts) VALUES('optimize'); ledger row>
3. COMMIT                                -- batch() exit
4. VACUUM                                -- rewrites the file; NOT inside a transaction
                                         --   raises OperationalError under contention
5. busy, log, ckpt = PRAGMA wal_checkpoint(TRUNCATE)   -- CAPTURE the return row
6. if busy != 0 or wal_size > 0:  ->  reclaimed = False + warning   (see below)
```

**Step 5 fails silently, and step 6 is the whole reason it is a step.**
`PRAGMA wal_checkpoint(TRUNCATE)` does **not** raise on contention — it returns
`(busy, log, checkpointed)` with `busy=1` and leaves the WAL in place. Reproduced
against this repo's SQLite 3.53.4 (§13.5): with one concurrent reader holding a read
transaction, `VACUUM` *succeeded*, the checkpoint returned `(1, 24, 13)` **without
raising**, and the WAL was left at 98,912 bytes containing 4,500 copies of the
canary. An implementation that ignores the return row reports a clean erasure over a
WAL full of the erased content.

This is the engine's **normal** state, not an exotic one: §5.7 runs erasure on
`Memory._maintenance_store` while the cached serving connection stays open on the
same file for the process lifetime, and the console gateway or a
`lean-memory-maintain` process can hold a third. Hence step 0, and hence the
requirement that `Memory._stores.pop(namespace)` + `close()` precede **every**
reclaim, not just `forget_namespace`.

**On residual busy** (an out-of-process handle the library cannot close), the
implementation must **not** return a clean report. It sets `reclaimed=False` and
appends a warning naming the residual WAL byte count and the remedy: *"the
write-ahead log still holds N bytes of pre-erasure page images because another
connection holds this namespace file open; close it and call `reclaim()` again."*
Whether `forget(reclaim=True, apply=True)` should go further and **raise** rather
than return a false-clean report is §14.9 — the argument for raising is that a
caller discharging a legal obligation will not read `report.reclaimed`, and the
argument against is that the logical erasure genuinely did commit. Either way the
one behavior that is ruled out is silence.

Measured canary residue in the raw file (80 facts, one canary fact carrying the
string in `fact_text`, `object_literal`, `entity.name`, and `episode.raw`):

| Procedure | Canary bytes left |
|---|---|
| nothing (plain redact/delete only) | **42** |
| `optimize` only | 41 |
| `optimize` + `secure_delete` | 21 |
| `secure_delete` + `VACUUM`, **no** `optimize` | **2** ← the FTS-segment leak |
| `optimize` + `VACUUM` | **0** |
| `optimize` + `secure_delete` + `VACUUM` | **0** |

`secure_delete` is retained in the adopted procedure even though `optimize +
VACUUM` alone measured 0, as cheap insurance for pages freed between steps and
for the `reclaim=False` path where VACUUM is deferred.

**Costs, and hence the `reclaim` flag.** `VACUUM` rewrites the entire database
(needs ~2× the file size in free space and takes an exclusive lock);
`optimize` merges the whole FTS index. Both are O(namespace), not O(erased).
So `Memory.forget(..., reclaim=True)` by default (erasure is the point), with
`reclaim=False` for bulk erasures that run one reclaim pass at the end via
`Memory.reclaim(namespace)`. A report with `reclaim=False` carries the warning
*"content is unreachable through the API but not yet scrubbed from the file;
call reclaim() to complete erasure."*

**What this procedure cannot do**, and what the docs must say in the same
paragraph: it does not touch backups, filesystem snapshots, replicas, the
physical medium (SSD wear-levelling, CoW filesystems), the OS page cache or
swap, files copied earlier, the host application's logs, the console's
`_events.db` sidecar — **which holds not just search queries but a verbatim copy of
every fact text a console or console-MCP search ever returned**
(`console/.../engine.py:255-280`; §1.2, and the decision at §14.5) — or the MCP
client's conversation transcript. It also does not remove the **namespace string**
itself, which survives in every table, in the vec0 metadata, and in the filename
(§1.2, §3.1); only `forget_namespace` reaches that.

## 7. API shape for WP5-impl

### 7.1 `Memory` verbs

```python
# src/lean_memory/erase.py — new module; Memory delegates, mirroring maintain/
ErasureMode = Literal["redact", "purge"]

@dataclass(frozen=True)
class ErasureReport:
    namespace: str
    unit: Literal["fact", "entity", "episode", "namespace"]
    mode: ErasureMode
    applied: bool                       # False for a dry run
    erasure_id: str | None              # ledger id; None unless applied
    facts: list[str]                    # directly selected fact ids
    derived_facts: list[str]            # fact_derivation closure (§5.2)
    retired_duplicates: list[str]       # dedup-retired-duplicate closure (§5.2a)
    entities: list[tuple[str, str]]     # (entity_id, name) — echoed so the caller can
                                        #   confirm WHO resolved (§4.2); names only in the
                                        #   report object, never in the ledger (§8)
    entity_near_matches: list[tuple[str, str, int]]
                                        # (id, name, live_fact_count) — case-insensitive /
                                        #   substring aliases NOT erased (§4.2, §1.3.10)
    episodes: list[str]                 # episodes whose raw text + source was/would be erased
    episodes_retained: list[str]        # orphan rule said no — see warnings (§5.5)
    episodes_unreachable: int           # fact-less episodes no fact-derived unit can reach (§5.5b)
    episodes_swept: list[str]           # sweep_episodes=True name-scan hits (§4.2)
    proposals: list[str]                # maintenance_proposal ids cleared
    proposals_scanned: int              # cost signal for the append-only LIKE scan (§5.3)
    index_rows: dict[str, int]          # {"fact_vec": n, "fact_fts": n}
    chain_splices: list[tuple[str, str, str]]   # (referrer, removed, new_canonical); purge only
    intervals_closed: list[str]         # open referrers whose valid_to erasure closed
                                        #   (§5.1 rule 2); purge only, empty in redact
    reclaimed: bool                     # False when the WAL checkpoint came back busy (§6.3)
    wal_bytes_remaining: int | None     # non-zero iff reclaimed is False
    bytes_reclaimed: int | None
    warnings: list[str]

class Memory:
    def forget(
        self,
        namespace: str,
        *,
        fact_id: str | None = None,
        entity: str | None = None,          # by name — resolved via ix_entity_lookup
        entity_id: str | None = None,
        entity_type: str | None = None,
        episode_id: str | None = None,
        mode: ErasureMode = "redact",
        cascade_summaries: bool = True,
        cascade_episodes: bool = True,
        sweep_episodes: bool = False,       # entity unit only; §4.2 / §5.5b name scan
        reclaim: bool = True,
        apply: bool = False,                # DRY RUN BY DEFAULT
        actor: str = "api",
    ) -> ErasureReport: ...

    def forget_namespace(self, namespace: str, *, apply: bool = False) -> ErasureReport: ...
    def reclaim(self, namespace: str) -> ErasureReport: ...   # optimize + VACUUM + checkpoint
```

Exactly one of `fact_id` / (`entity` | `entity_id`) / `episode_id` may be given;
zero or two raise `ValueError` (never "erase everything" by accident — that is
`forget_namespace`, a different verb).

**Naming.** `forget`, not `delete`. Every competitor ships `delete`
(`docs/competitive-landscape.md`), but our default semantics are redaction and
calling it `delete` would misdescribe it. The README parity table maps
Mem0 `delete`/`delete_all` → `forget(fact_id=…)` / `forget(entity=…)` /
`forget_namespace()`, so the parity claim survives without the misnomer. Cognee's
`forget` is the closest precedent.

**`apply=False` default.** Consistent with `Memory.maintain(apply=False)`, the
MCP maintenance tools, and the `lean-memory-maintain` CLI — one convention in
one codebase. The counter-risk is real and worth writing down: a caller who
writes `mem.forget(ns, fact_id=x)` and believes they complied has *not*.
Mitigations: `ErasureReport.applied` is a first-class field; `__repr__` leads
with `DRY RUN — nothing was erased` when false; the docstring's first line says
it; §9 pins a test. See §14.2 — this is the one API decision worth re-litigating.

### 7.2 Store verbs (new mutation surface)

```
Store ABC additions (store/base.py):
  redact_fact(fact_id, *, expired_at, erasure_id)      # fact_text='', object_literal/object_id NULL,
                                                       #   is_latest=0, expired_at, erasure_id; + DELETE the
                                                       #   fact_vec and fact_fts rows (three surfaces, one txn)
  purge_fact(fact_id, *, erasure_id, at) -> PurgeResult  # resolve the splice target to the LIVE canonical
                                                       #   (walk superseded_by while is_latest=0), re-point
                                                       #   referrers, close valid_to on any that were open,
                                                       #   DELETE fact_derivation edges, then DELETE the row.
                                                       #   Returns (splices, intervals_closed) — §5.1
  redact_episode(episode_id, *, erased_at, erasure_id)  # raw='', source=NULL (§4.3)
  redact_entity(entity_id, *, erased_at, erasure_id)
  find_all_summaries_derived_from(source_ids) -> list[str]   # UNFILTERED — includes is_latest=0 (§5.2)
  find_open_retired_duplicates(fact_ids) -> list[str]   # WHERE superseded_by IN (:ids) AND valid_to IS NULL
                                                       #   — the DEDUP-EXACT losers (§5.2a); valid_to IS NULL
                                                       #   is what excludes genuine predecessors
  find_entity_near_matches(namespace, name) -> list[tuple[str, str, int]]  # §4.2 alias warning (§1.3.10)
  count_factless_episodes(namespace) -> int             # §5.5b episodes_unreachable
  find_proposals_referencing(namespace, fact_ids) -> tuple[list[str], int]  # ids + rows scanned (§5.3)
  clear_proposal_payload(proposal_id, *, erasure_id)
  optimize_fts()                                        # INSERT INTO fact_fts(fact_fts) VALUES('optimize')
  reclaim() -> ReclaimResult                            # VACUUM + wal_checkpoint(TRUNCATE). MUST NOT run inside
                                                       #   batch() (measured). MUST capture the pragma's
                                                       #   (busy, log, checkpointed) row and re-stat the -wal:
                                                       #   busy != 0 is a FAILURE, not an exception (§6.3)
  record_erasure(...) / list_erasures(namespace)        # the append-only ledger
```

### 7.3 MCP surface

One tool, registered on **all three** MCP surfaces — core
`src/lean_memory/mcp_server.py`, the console stdio server
`console/.../observe_mcp.py` (what `plugin/.mcp.json` actually ships), and the
console HTTP mount `console/.../routes/mcp.py` — the lesson from the *sleep-time
maintenance spec's* §1.3.10 (not this doc's): registering only in core reaches no
plugin user. The console surfaces write only
through `EngineGateway`, so the gateway gains a `forget` method (per-namespace
asyncio lock + `retry_busy` + single worker thread), exactly as WP10a added four.

```python
@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
def memory_forget(
    namespace: str,
    fact_id: str | None = None,
    entity: str | None = None,      # SAME SPELLING as the Python API — not "subject"
    episode_id: str | None = None,  # the episode unit's only reachable selector (§4.3)
    mode: str = "redact",
    apply: bool = False,
) -> dict
```

**Parameter parity is a pinned contract, not a style preference.** The selector is
spelled `entity` on all four surfaces (Python, CLI `--subject` aside — see below,
MCP ×3), because `console/tests/test_mcp_parity.py` asserts per-tool parameter sets
across the three MCP registrations (`assert {"source","t_ref"} <= wp["memory_add"]`,
plus an exact tool-name set), and a `subject`/`entity` drift would land as a
cross-surface inconsistency the moment the tool is added to `_EXPECTED_TOOLS`.
`episode_id` is exposed for the same reason it is a unit at all: an episode that
produced no fact has no other selector (§1.3.12, §5.5b), so omitting it would make
one of the four units unreachable from the surface most users actually have.

*Deliberately omitted from the MCP surface, with the defaults they take:*
`entity_id` and `entity_type` (an agent has names, not ids; ambiguous resolution is
surfaced through the report's `entity_near_matches` instead), `cascade_summaries=True`,
`cascade_episodes=True`, `sweep_episodes=False`, `reclaim=True`, and `actor`, which
the tool sets to `"mcp"` itself rather than letting a caller forge it. The CLI keeps
`--subject` as a **documented alias** for `--entity` only if the parity test's scope
is MCP-only; if it ever covers the CLI, the alias goes.

Dry-run by default, symmetric with `memory_maintenance_run`. `server.json` and
`plugin/.mcp.json` are reconciled in the same change (the v0.1.3 manifest
lesson); `tests/test_mcp_tool_metadata.py` gains the tool's contract row, and
`console/tests/test_mcp_parity.py` gains `memory_forget` in `_EXPECTED_TOOLS` plus a
parameter-set assertion — both, not either.

*Security note.* An agent that can erase memory is an agent that can be
prompt-injected into erasing memory (the memory-poisoning literature the WP10a
spec cites at §2.4). The marginal risk is small because `memory_clear` already
lets an injected agent destroy an entire namespace — but the option of gating
apply behind `LM_ALLOW_MCP_ERASE=1`, or routing agent-initiated erasure through
the WP10b review queue as a proposal, is left open at §14.3.

### 7.4 CLI

`lean-memory-forget --root $LM_DATA_ROOT --namespace NS
[--fact-id ID | --entity NAME | --episode-id ID] [--mode redact|purge]
[--no-cascade-summaries] [--sweep-episodes] [--no-reclaim] [--apply] [--json]` — a
`[project.scripts]` entry beside `lean-memory-mcp` and `lean-memory-maintain`,
dry-run by default. `--entity`, matching the Python and MCP spelling (§7.3);
`--subject` may be kept as a hidden alias but is not the documented flag.
It exists because erasure requests arrive out of band and the operator wants a
shell command plus a JSON report they can archive as evidence.

### 7.5 The guarantee, as the docs may honestly state it

The canonical paragraph — reused verbatim in the `forget` docstring, the MCP
tool description, and the README:

> `forget()` destroys the stored content: the fact text, its literal value, its
> embeddings, its full-text index entries, any near-identical duplicate the
> engine had merged into it, the raw episode text and `source` behind it (when
> no retained fact still needs it), any maintenance summary derived from it, and
> any staged maintenance proposal quoting it. With `reclaim=True` (the default)
> the database file is then rewritten and its write-ahead log truncated, so the
> content is not recoverable by reading the file — verified in our test suite by
> scanning the raw bytes, not just by querying the API. **Truncating the log
> requires sole access to the file**; if another connection or process holds it
> open, `forget()` returns `reclaimed=False` and tells you, rather than reporting a
> clean erasure over a log that still holds the old page images. Call `reclaim()`
> again once the other handles are closed. What survives in `redact` mode is a
> content-free tombstone: identifiers, timestamps, and the supersession pointers
> that keep your history walkable.
>
> **This guarantee covers one namespace file on this machine.** It does not
> reach backups, filesystem or cloud snapshots, replicas or copies made earlier,
> data already retrieved by an application, your agent client's conversation
> transcript, or blocks retained by your SSD or copy-on-write filesystem.
> If you use the lean-memory console, note that its `_events.db` activity log keeps
> a verbatim copy of every fact text a search returned; that file is outside this
> guarantee. Erasure is not retroactive and does not prevent the same information
> from being added again.
>
> **Three limits worth knowing before you rely on `forget(entity=…)`:**
> (1) *Subjects are matched by exact name.* "Alice", "alice" and "Alice Chen" are
> three different subjects to the engine; the dry-run report lists near-matches so
> you can re-run per alias. (2) *A message that produced no fact is not reachable
> by subject.* It can only be erased by episode id, or by erasing the namespace —
> the report tells you how many such messages the namespace holds. (3) *The
> namespace name itself is never erased below the namespace unit* — it is stored on
> every row and is the filename. If you name namespaces after people, use
> `forget_namespace()`; better, use opaque namespace ids and keep the mapping in
> your own application.

## 8. Schema v3 (user_version-gated migration, 2 → 3)

```sql
-- Inside an `if version < 3:` branch of _init_schema (sqlite_store.py:125-137).
-- ADD COLUMN is NOT idempotent — 'duplicate column name' on reopen (WP10a §5).
ALTER TABLE fact    ADD COLUMN erasure_id TEXT;              -- NULL = live; set = tombstone
ALTER TABLE entity  ADD COLUMN erased_at  INTEGER;
ALTER TABLE entity  ADD COLUMN erasure_id TEXT;
ALTER TABLE episode ADD COLUMN erased_at  INTEGER;
ALTER TABLE episode ADD COLUMN erasure_id TEXT;
PRAGMA user_version = 3;

-- Always-run blob (SCHEMA_SQL) — CREATE ... IF NOT EXISTS only:
CREATE TABLE IF NOT EXISTS erasure (
  id           TEXT PRIMARY KEY,
  namespace    TEXT NOT NULL,
  unit         TEXT NOT NULL,        -- 'fact'|'entity'|'episode'|'namespace'
  mode         TEXT NOT NULL,        -- 'redact'|'purge'
  actor        TEXT,                 -- 'api'|'cli'|'mcp'|'console'
  requested_at INTEGER NOT NULL,
  applied_at   INTEGER,
  counts_json  TEXT NOT NULL,        -- {"facts":n,"derived":n,"episodes":n,...} — COUNTS ONLY
  splices_json TEXT,                 -- purge only: [[referrer,new_target],...] — ids only
  reclaimed    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_erasure_ns ON erasure(namespace, requested_at);
```

The ledger stores **no selector text and no content** — not the erased
`fact_text`, not the subject name, not the query that found it. `counts_json`
and ids only. In redact mode `fact.erasure_id` links a tombstone back to its
ledger row; in purge mode nothing links back, by construction.

*One qualification, because "no selector text" is only true of the columns this
table controls:* `erasure.namespace` stores the raw namespace string, exactly as the
six existing tables do (§1.2). In a deployment that names namespaces after
individuals, that **is** selector text — the ledger row then reads "an erasure
happened, about `bob-smith`, at T". This is not fixable inside the ledger (the
column is needed to scope `list_erasures`, and the file is named after the namespace
anyway); it is another reason §3.1 recommends opaque namespace ids. The
`splices_json` and `counts_json` columns remain strictly ids and counts.

*Why `fact` needs no `erased_at`:* it has `expired_at` (`schema.py:49`), the
dormant bitemporal transaction-time column, which erasure sets (§3.2).
`entity`/`episode` have no bitemporal axis, so they get an explicit `erased_at`.
The asymmetry is deliberate and documented in the DDL comment.

**ADD-only compatibility.** Every change is additive: new nullable columns, one
new table, no drops, no rewrites of existing rows at migration time. An
0.1.x/0.2.x file opens, gains the columns once, and round-trips. The
sanctioned-mutation carve-out this design needs from the global invariant in
`workpackets.md` is exactly one sentence, proposed here for the maintainer to
paste when the design is approved (this doc does not edit that file):

> ADD-only discipline: nothing mutates or deletes stored history **except**
> (a) the sanctioned supersession/tier verbs, and (b) `Memory.forget()` /
> `Memory.forget_namespace()`, whose semantics are fixed by
> `docs/superpowers/specs/2026-08-06-wp5-deletion-gdpr-design.md`.

**Migration obligations** (all in the same change, all learned from WP10a §5):
the versioned branch never lowers a newer stamp; a checked-in **v2-format
fixture DB** joins `tests/fixtures/v1_format.db` (generator alongside
`make_v1_fixture.py`) with an open→upgrade→reopen→round-trip test; the console
`EXPECTED_SCHEMA_FINGERPRINT` (`console/.../inspect_sql.py:313-315`) is updated
in the same commit because the new `CREATE TABLE erasure` line trips the digest
(which keys on lines containing "create" — the bare ALTERs alone would not).
`types.Fact` gains `erasure_id`, `_row_to_fact` (`sqlite_store.py:772-785`)
reads it, and `add_fact`'s INSERT column list grows by one.

## 9. Test plan sketch (`tests/test_deletion.py`)

1. **Raw-byte residue (the headline, and the packet's stated acceptance
   criterion).** Ingest a corpus containing a canary string in `fact_text`,
   `object_literal`, `entity.name`, and `episode.raw`; `forget(apply=True)`;
   close; then `open(path,'rb').read()` and assert **zero** canary occurrences
   in `<ns>.db` and that `<ns>.db-wal` is absent or 0 bytes. Parameterized over
   both modes. This test is what makes the guarantee sentence in §7.5 true.
   **Concurrent-reader variant (§6.3 step 6):** run the same erasure with a second
   connection holding an open read transaction on the file, and assert the report
   comes back `reclaimed=False` with a non-zero `wal_bytes_remaining` and the
   warning — *not* that the file is clean. Without this variant the headline test
   passes silently in exactly the configuration the engine actually runs in
   (a cached serving connection open alongside the maintenance store), and
   `wal_checkpoint(TRUNCATE)` returns busy rather than raising. Then close the
   reader, call `reclaim()`, and assert zero canary bytes and `reclaimed=True`.
2. **The FTS-segment regression** (§5.4): the same test with `optimize` skipped
   must FAIL (assert residue > 0) — pinning *why* the step exists, so nobody
   "simplifies" it away. Measured today at 2 occurrences.
3. **Unretrievability across every surface:** after erasure, `search()`,
   `search(as_of=T, is_latest_only=False)`, and `search(include_cold=True)` all
   return nothing for the erased content, for T inside the fact's validity
   interval. Plus a direct store assertion that no `fact_vec`/`fact_fts` row
   remains for the id.
4. **As-of invariance for everything else:** the WP10a §10.1 grid — snapshot the
   ids satisfying the visibility predicate over a T grid, erase one fact, assert
   the set differs by exactly the erased id (and its cascade closure) and by
   nothing else. `valid_at`/`valid_to` of survivors byte-identical. **Purge variant
   with an open retired duplicate present** (§5.1 rule 2): the only `valid_to` the
   run may change is that of a spliced-or-orphaned open referrer, it must appear in
   `intervals_closed`, and no survivor may end up `superseded_by`-pointing at an
   `is_latest=0` row.
5. **Chain semantics:** redact the middle of A→B→C — pointers unchanged,
   `history()` renders the tombstone, chain still walks A→B→C. Purge the middle
   — `A.superseded_by == C`, `PRAGMA foreign_key_check` returns zero rows, the
   ledger records the splice. Head-of-chain purge: referrer's `superseded_by`
   NULL, an already-closed referrer keeps its `valid_to`, `is_latest` still 0.
   **Splice-target resolution:** build A→B→C where C is itself `is_latest=0`
   pointing at a live D, purge B, assert `A.superseded_by == D` (the live
   canonical) and **not** C — the naive `superseded_by = B.superseded_by` splice
   fails this. **Open-referrer closure:** `retire_duplicate(L, B)` so L is open
   (`superseded_by=B`, `valid_to IS NULL`), then purge B; assert L's `valid_to` is
   closed at the erasure time, L is in `intervals_closed`, and a subsequent
   `supersede_fact` on the survivor does **not** leave L as a permanently open
   interval on `get_all(is_latest_only=False)` — the WP10a §14 wrong answer,
   re-pinned on the erasure path.
6. **FK integrity after every operation:** `PRAGMA foreign_key_check` empty.
   Standing assertion in a fixture teardown, not one test.
7. **Derivation cascade:** summarize a slot (WP10a apply path), then erase a
   source ⇒ the summary's text is gone. **Retired-summary variant** (the §5.2
   trap): invalidate the summary via ingest first, *then* erase the source ⇒ the
   summary is still erased. A test using `find_summaries_derived_from` unchanged
   must fail. Transitive variant: S2 derived from S1 derived from F.
8. **Proposal cascade:** stage a dedup_near/summarize/evict proposal, erase a
   referenced fact ⇒ `payload_json == '{}'`, `edited_text IS NULL`,
   `status='expired'`, `expiry_reason='erased'`; a subsequent `decide(approve)`
   reports already-decided and touches nothing.
9. **Episode orphan rule:** two facts from one episode; erase one ⇒ raw text
   retained and a warning names the episode; erase both ⇒ raw text gone.
10. **Dry-run purity:** full-DB SHA-256 before and after `apply=False` is
    identical (for both modes, all four units), and the report's counts equal
    the applied run's counts.
11. **Idempotence:** erasing an already-erased fact is a no-op that returns a
    report with zero counts and does not create a second ledger row.
12. **Maintenance interlock:** erasure refuses while a fresh maintenance lease
    is held; a tombstone is never picked up by DEDUP-EXACT (two tombstones in
    one slot must not "merge" on empty normalized text), EVICT, or SUMMARIZE.
13. **Namespace unit:** `forget_namespace` unlinks `.db`/`-wal`/`-shm`, closes
    the cached store, is idempotent on a missing namespace, leaves sibling
    namespaces byte-identical, and refuses under a live lease.
14. **Migration:** the v2-format fixture opens, upgrades once, reopens cleanly
    (the ALTER-idempotence trap), round-trips; the console fingerprint test is
    green with the updated constant.
15. **Offline + stdout hygiene:** whole suite on `FakeEmbedder`/`StubTyper`;
    the stdout-hygiene test extends to `memory_forget` (fd 1 stays the JSON-RPC
    channel).
16. **Existing pins stay green:** `test_spine.py`, `test_asof_sparse.py`,
    `test_functional_slot_supersession.py`, `test_maintenance_asof_grid.py`,
    `test_ingest_commutation.py`, `test_schema_migration.py`, and — for the §7.3
    parameter spelling — `console/tests/test_mcp_parity.py` and
    `tests/test_mcp_tool_metadata.py` updated in the same change.
17. **Dedup-retired duplicate is in the closure** (§5.2a — the byte-equivalent
    leak). Ingest one sentence twice in two casing/spacing variants, run
    DEDUP-EXACT to merge them (survivor `is_latest=1`, loser
    `superseded_by=survivor`, `valid_to IS NULL`), then `forget(fact_id=survivor,
    apply=True)`. Assert: **zero** canary bytes in the raw file; the loser's
    `fact_fts` and `fact_vec` rows are gone; `search(is_latest_only=False)` and
    `search(as_of=T)` both return nothing. A cascade built from `fact_derivation`
    alone must FAIL this test — it is the pin on why the second closure exists.
    *Negative control in the same test:* a genuinely superseded predecessor
    (`valid_to` closed, different value — "salary 100k" behind "salary 110k") is
    **not** erased when its successor is, proving the `valid_to IS NULL` conjunct
    discriminates rather than over-erasing.
18. **The fact-less episode** (§5.5b). Ingest a message the stub generator yields
    zero candidates for (and, separately, a WP11 restatement that hits the
    `_restatement_key` guard at `memory.py:201-203`), then run
    `forget(entity=…, apply=True)` for the subject the message is about. Assert
    the raw text is **still present** in the file, that `episodes_unreachable >= 1`,
    and that `episodes_unreachable_note` is in `warnings`. Then assert
    `forget(episode_id=…, apply=True)` does erase it, and that
    `sweep_episodes=True` lists it in `episodes_swept`. The failure this pins is a
    silent one: a report that says "erased" while the message survives.
19. **Entity resolution is exact, and says so** (§1.3.10, §4.2). Ingest facts under
    "Alice", "alice" and "Alice Chen"; `forget(entity="Alice")` erases only the
    exact row's facts, and the report's `entity_near_matches` names the other two
    with their live fact counts. Plus: `forget(entity=…)` in a namespace whose name
    contains the entity name emits the §3.1 namespace warning.
20. **`episode.source` is redacted with `raw`** (§4.3): add with
    `source="alice@example.com"`, erase the episode, assert the address is absent
    from the raw file bytes as well as from the column.

## 10. Non-goals (explicit)

- **Backups, snapshots, replicas, export copies.** Not detected, not enumerated,
  not erased. Named in every guarantee sentence.
- **Secure physical erasure of the storage medium.** Whole-disk encryption is
  the deployer's answer.
- **Authenticating or authorizing an erasure request.** The library has no user
  model; the host app is the controller (§2.2).
- **Cross-namespace subject erasure.** Needs WP6 scoping; today "erase
  everything about S" means "call `forget(entity=S)` per namespace", and the
  docs say so.
- **Preventing re-ingestion** of the same information (§11.5).
- **Crypto-shredding / encryption at rest** (§11.4).
- **The console's `_events.db` sidecar** — which holds search queries **and a
  verbatim copy of every fact text returned by a console or console-MCP search**
  (`console/.../engine.py:255-280`). Console-owned file, the engine never touches
  it. Listing it as a non-goal is provisional: because it is a *content* copy of the
  very rows `forget()` destroys, §14.5 asks the maintainer to decide **now** whether
  WP5-impl brings it in scope or stops logging `fact_text` altogether. If the answer
  is "in scope", this bullet moves out of §10.
- **The namespace string.** No unit below `forget_namespace` removes it, in any
  table, in the vec0 metadata, in the erasure ledger, or in the filename
  (§1.2, §3.1). Deployments that need the subject's name gone must either use
  opaque namespace ids or erase the namespace.
- **Erasing the MCP client's transcript, model caches, or anything a model was
  trained on.**
- **Data portability / export (Art. 20).** Adjacent, not this packet.
- **A query language for erasure selectors** (§4.5).
- **Any change to non-erased facts' retrieval, ranking, tiering, or as-of
  behavior.** WP10a's invariants hold unchanged.

## 11. Alternatives considered

### 11.1 Per-fact hard delete as the default (WP5 packet option (b))

Rejected as the *default*, kept as `mode="purge"`. Measured: the engine's own
FKs refuse it in all four common shapes (§13.3); making it work requires either
over-erasure or chain splicing, and splicing forges history. Defaults should not
corrupt or lie. See §6.1.

### 11.2 Tombstone-only, no physical scrub

"Set a deleted flag and filter it at read time." Rejected outright: the content
stays in the file, in the FTS index, and in the embedding — a filter is a
promise, not an erasure, and one forgotten `include_deleted` flag re-exposes
everything. The measured residue tables in §6.3 exist precisely to make this
failure mode unthinkable.

### 11.3 Rebuild-the-file (dump-and-reload without the erased rows)

Export every retained row to a new file, swap it in. Clean physical guarantee
and no FK surgery, but: O(namespace) per erasure, requires re-embedding or
copying vec0 chunks blind, has a crash window where both or neither file is
authoritative, and must reproduce the schema exactly. `VACUUM` gives ~the same
physical result for a fraction of the complexity (measured). Rejected;
reconsider only if VACUUM proves insufficient on some platform.

### 11.4 Crypto-shredding (WP5 packet option (d))

Per-namespace or per-subject key; "erase" = destroy the key. Rejected for v1:
(a) it requires encryption at rest that lean-memory does not have, and
ciphertext is not indexable — FTS5 and vec0 both need plaintext-derived
structures, so per-field encryption would break retrieval; (b) key management
has no home in a local-first single-user tool — a key stored next to the DB is
not a control, and a key in an OS keychain makes the library platform-bound;
(c) the guarantee is only as strong as the key never having been copied, which
is unverifiable. It is the *right* answer for a hosted multi-tenant service —
which is a declared anti-goal for this project. Revisit if WP6 introduces
per-subject boundaries and someone deploys server-side.

### 11.5 A hash denylist to block re-ingestion

Store a salted hash of erased text; refuse to `add()` anything matching.
Rejected: it retains a fingerprint of the very content we promised to destroy
(and an offline-guessable one for short, structured facts like a phone number),
it only catches byte-identical restatements, and it makes the erasure ledger a
re-identification surface. Re-ingestion is the host app's problem (§10).

### 11.6 Erasure as a WP10b review proposal

Route every erasure through the existing `maintenance_proposal` queue for human
approval. Elegant reuse, and genuinely attractive for *agent-initiated*
erasure — but wrong as the general shape: a proposal payload would have to quote
the content awaiting erasure (§5.3 is the whole reason that is bad), and an
erasure request that sits pending for 30 days is a compliance failure. Kept as
an option for the MCP surface only (§14.3).

## 12. Comparison to prior art

**Adopted:** logical-vs-physical deletion with an explicit excision escape hatch
(Datomic `:db/excise`, XTDB `ERASE` — both keep an append-only log and treat
erasure as an out-of-band, audited exception, which is precisely §6.1);
tombstones with reader-gated physical reclaim (Kafka log compaction, RocksDB
compaction — our `reclaim` flag is the same idea at file scale);
redaction-preserving-structure (event-sourcing practice, Kleppmann DDIA ch. 11-12:
the log is the source of truth and is not rewritten, but *personal data* may be
excised from it); dry-run-first destructive operations (this repo's own
maintenance CLI/MCP convention).

**Rejected, deliberately:** inline destructive delete with no audit trace
(Mem0's `delete`, LangMem) — it is the behavior our whole positioning argues
against; filter-only soft delete (§11.2); crypto-shredding (§11.4).

**Novel, as far as the survey reaches:** an erasure design for an embedded
agent-memory engine that (1) treats the *derived-summary closure*, the
*dedup-retired duplicate closure*, and the *staged-proposal payloads* as
first-class erasure targets — all three are places current memory products copy
content and none of them erase — and (2) states its guarantee at the level of
measured raw-file residue, with the FTS5 segment-merge requirement (§5.4) and the
silently-failing WAL checkpoint (§6.3) as explicit, test-pinned steps rather than
undiscovered leaks.

**Where this design is weaker than it looks, stated in the same breath:** its
subject resolution is exact string matching (§1.3.10), it cannot reach a message
that produced no fact (§5.5b), and it does not erase the namespace name (§1.2).
Every comparable product has the first problem; naming the second and third is the
part that is unusual.

## 13. Verification record (2026-08-06)

All probes ran against the project venv (`.venv`, Python 3.13, SQLite 3.53.4,
sqlite-vec 0.1.9) using the real `SqliteStore`, not a hand-written schema.

**13.1 Physical layout.** `PRAGMA secure_delete` = **0**, `auto_vacuum` = **0**,
`journal_mode` = **wal**. A fresh namespace file contains **24 tables**, enumerated
from `sqlite_master` (re-verified 2026-08-06; an earlier draft said 21, miscounting
both the logical tables and the vec0 shadows):

- **6 logical:** `episode`, `entity`, `fact`, `fact_derivation`, `maintenance_run`,
  `maintenance_proposal`.
- **2 virtual:** `fact_vec` (vec0), `fact_fts` (FTS5).
- **5 FTS5 shadow:** `fact_fts_content|data|docsize|idx|config`.
- **10 vec0 shadow:** `fact_vec_chunks|rowids|info|vector_chunks00|vector_chunks01|
  metadatachunks00|01|02|metadatatext01|metadatatext02`.
- **1 SQLite-internal:** `sqlite_sequence` — content-free (table names and rowid
  counters only), so it is not an erasure target.

Inventory reproduced in §1.2. The count matters only for this section's own
credibility; nothing in the residue matrix depends on it.

**13.2 Retrieval coupling.** Confirmed by reading `retrieve/retriever.py:84-88`
and exercising the store: candidate ids come only from `dense_search`/
`sparse_search`, i.e. only from `fact_vec`/`fact_fts`; unhydratable ids are
dropped. Deleting the two index rows removes a fact from every surface.

**13.3 Foreign keys refuse hard deletes** (`PRAGMA foreign_keys` = 1). All four
attempts raised `IntegrityError: FOREIGN KEY constraint failed`: deleting the
middle of a supersession chain (referenced via `superseded_by`); deleting a fact
referenced by `fact_derivation.source_id`; deleting an episode with live facts;
deleting an entity with live facts. Forcing the delete with `foreign_keys=OFF`
left `A.superseded_by` dangling and `PRAGMA foreign_key_check` reported the
violation. This is the empirical basis for §6.1's argument 2.

**13.4 Index remnants.** After `DELETE FROM fact_fts WHERE fact_id=?`, the
canary term appeared in **2** `fact_fts_data` blocks (up from 1) while
`fact_fts_content` correctly dropped to 0 — FTS5's delete is a marker, not a
scrub. `INSERT INTO fact_fts(fact_fts) VALUES('optimize')` → 0 blocks;
`('rebuild')` likewise; `('integrity-check')` clean afterwards, 79 rows intact.
After `DELETE FROM fact_vec WHERE fact_id=?`, the exact serialized vector bytes
were absent from `fact_vec_vector_chunks00` (1 → 0) and KNN kept working.

**13.5 Residue matrix, and the silent checkpoint.** The table in §6.3, measured
end-to-end on a closed file (80 facts, canary in four columns):
42 / 41 / 21 / **2** / 0 / 0. The `2` is the `secure_delete + VACUUM` **without**
`optimize` case — the finding that drove step 2 of the procedure. Also measured:
with a live connection, `VACUUM` before `wal_checkpoint(TRUNCATE)` leaves the
pre-erasure images in `<ns>.db-wal` (4.1 MB WAL still containing 254 copies of the
canary) — hence the fixed order. And `VACUUM` inside an open transaction raises
`OperationalError: cannot VACUUM from within a transaction`.

**`PRAGMA wal_checkpoint(TRUNCATE)` does not raise when it cannot truncate.**
Re-measured 2026-08-06 on SQLite 3.53.4 with one concurrent reader holding an open
read transaction on the same file: `VACUUM` **succeeded**, the checkpoint returned
the row `(busy=1, log=24, checkpointed=13)` **without raising**, and `<ns>.db-wal`
was left at **98,912 bytes containing 4,500 canary occurrences**. This is the
asymmetry §6.3 step 6 exists for — `VACUUM` signals contention by exception,
`wal_checkpoint` signals it by return value, and a procedure that checks only for
exceptions reports a clean erasure over a WAL full of the erased content. Since
`Memory` keeps a cached serving connection open for the process lifetime
(`memory.py:120`) while erasure runs on `_maintenance_store` (`:327-337`), the
contended case is the *default* case unless the cached store is popped first.

**13.6 Content-copy inventory.** Verified by reading the sources:
`maintenance_proposal.payload_json` carries verbatim `fact_text`
(`transforms.py:249, 347, 423`) and `edited_text`; the maintenance episode's
`raw` is ids-only JSON (`lifecycle.py:385-395`) — no content; `maintenance_run.
stats_json` is aggregate counts; `fact_derivation` is ids only; the console
`_events.db` stores the search query verbatim **and the full `fact_text` of every
hit** — `hits = [{"fact_id": …, "fact_text": rf.fact.fact_text, …}]` passed straight
into `events.record` (`console/.../engine.py:255-280`) — while the *add* path
records only `episode_text_chars`, a length (`engine.py:223-232`). An earlier draft
of this doc characterized the sidecar as "search queries verbatim" in four places;
that was wrong in the direction that matters, and §14.5 is re-rated accordingly.

**13.7 Maintenance visibility of tombstones.** `iter_latest_facts`
(`sqlite_store.py:555-568`) and `_latest_nonsummary_in_slot`
(`transforms.py:144`) are both `is_latest=1`-scoped, so setting `is_latest=0` on
a tombstone removes it from every transform's candidate set with no code change.
An explicit `erasure_id IS NULL` guard is still specified (§9.12) as
defense-in-depth against a future candidate query that forgets the flag.

**13.8 Nothing in the package ever deletes a row.** `grep -rn 'DELETE FROM' src/`
returns **zero matches** at v0.2.3 — confirming both that WP5's deletes are a new
mutation surface and that `maintenance_proposal` is append-only:
`cas_decide_proposal` (`sqlite_store.py:674-695`) and `expire_proposal`
(`:707-720`) only flip `status`/`expiry_reason` (§5.3, §14.8).

**13.9 A `retire_duplicate` loser keeps its content and both index rows.** Read from
the source: the helper (`sqlite_store.py:485-520`) issues exactly three UPDATEs —
`fact.superseded_by`/`is_latest`, `fact_vec.is_latest`, and the open-sub-loser
re-point — and never touches `fact_text` or deletes an index row, while
`supersede_fact` sets `valid_to` on close (`:257-260`) and `retire_duplicate`
deliberately does not. `normalize_text` (`transforms.py:34-46`) folds NFC + casefold
+ whitespace only. Together these give §5.2a its predicate
(`superseded_by IN (…) AND valid_to IS NULL`) and its severity: the retained row is
the same sentence, still indexed.

**13.10 Dormant columns.** `entity.resolved_id` is written by nothing: across `src/`
and `console/` it appears only in the INSERT column list
(`sqlite_store.py:191-194`), `_row_to_entity` (`:768`), the dataclass default
(`types.py:50`) and the DDL (`schema.py:28`); `_build_fact` always passes
`Entity(namespace=…, name=tf.subject_name, type=None)` (`memory.py:264`). Same
shape as the already-documented `fact.object_id` dormancy (§1.3.9). Both are
recorded in §1.3 so the design does not lean on machinery that does not run.

**13.11 `episode.source` is caller-controlled free text.** Reachable with an
arbitrary string from `Memory.add` (`memory.py:134,148`), `EngineGateway.add`
(`console/.../engine.py:211`), `routes/data.py:32`, `routes/mcp.py:110` and
`observe_mcp.py:33`; the engine only ever writes `'user'` / `'maintenance'` itself
and constrains nothing. Hence its reclassification in §1.2 and its redaction in
§4.3.

**13.12 Episodes without facts.** Read from the source: `Memory.add` inserts the
`Episode` carrying the verbatim message at `memory.py:148-149`, *before* the
generator runs, and returns `[]` at `:152-154` when there are no candidates; the
WP11 restatement guard `continue`s past fact creation at `:201-203`. Neither path
removes or marks the episode. This is the mechanism behind §5.5b.

## 14. Open questions for the maintainer

1. **Default mode.** §6.1 recommends `redact`. Confirm — or, if the privacy
   positioning should lead with the strongest possible claim, flip the default
   to `purge` and accept spliced chains as the norm. (Recommendation: keep
   `redact`; purge is one keyword away.)
2. **`apply=False` default on the Python API** (§7.1). Consistent with
   `maintain()`, but a caller who forgets it believes they complied when they
   have not. Alternative: `forget()` executes and a separate `plan_forget()`
   returns the dry-run report — safer to read, one more verb, one more
   convention. (Recommendation: keep `apply=False` + the loud `__repr__`;
   this is the decision most worth a second opinion.)
3. **MCP apply gating** (§7.3). Ship `memory_forget(apply=True)` freely (the
   `memory_clear` precedent already lets an injected agent destroy a namespace),
   gate it behind `LM_ALLOW_MCP_ERASE=1`, or route agent-initiated erasure
   through the WP10b review queue (§11.6)? (Recommendation: ship it, dry-run by
   default, `destructiveHint=True`; revisit if a poisoning report lands.)
4. **`reclaim=True` default** (§6.3). VACUUM + FTS optimize are O(namespace) and
   take an exclusive lock — a per-fact erasure on a large namespace could stall
   a live MCP session for seconds. Accept that for correctness-by-default, or
   default `reclaim=False` with a nagging warning and a scheduled reclaim?
   (Recommendation: `True` — an erasure that does not erase is the worse
   failure.)
5. **Console `_events.db` — a decision required now, not a follow-up** (§1.2, §6.3,
   §10, §13.6). This was previously filed as "out of WP5, document the gap", on the
   understanding that the sidecar logs *search queries*. It does not only do that: it
   stores the **full `fact_text` of every hit** any console or console-MCP search
   ever returned (`console/.../engine.py:255-280`). That is a verbatim copy of the
   exact content `forget()` promises to destroy, sitting outside the erasure
   boundary and accumulating one copy per retrieval. Shipping `forget()` with a
   §7.5 guarantee while that file exists unaddressed makes the guarantee misleading
   to precisely the users who run the console. Two viable answers, both in scope
   for WP5-impl:
   - **(a) Stop logging the content.** Log `fact_id` + scores only, drop
     `fact_text` from the search event payload. One line in `engine.py`; costs the
     console's event viewer its inline hit text (it can re-hydrate from the store,
     which is the correct source anyway); removes the problem permanently rather
     than chasing it.
   - **(b) Bring the sidecar into the cascade.** `forget()` also purges rows
     referencing the erased `fact_id`s from the events log. Cheap — every hit
     already carries its `fact_id`, so no text scanning is needed — but it couples
     the engine to a console-owned file, and it cannot help a namespace whose events
     log was already copied.

   (Recommendation: **(a)**, with (b) as a one-off cleanup verb for existing logs.
   Data minimisation beats erasure plumbing: content that was never written cannot
   leak. If the maintainer prefers to keep the gap open instead, §7.5's carve-out
   sentence naming the sidecar as a *content* copy is mandatory, not optional.)
6. **WP10b review-UI integration.** WP10a §9.2 anticipated "WP5-integrated
   deletion verbs in the review UI". Ship a per-fact *Forget* button in the
   Review page as part of WP5-impl (lane D work, needs the gateway method
   anyway), or a separate packet? (Recommendation: separate — WP5-impl is
   already lane-A-heavy and gated behind WP4.)
7. **Packet sequencing.** The packet table says impl is blocked by WP4 + the
   six-week read. WP4's `history()` is the natural place to render tombstones,
   which is why the dependency exists — confirm that erasure should not ship
   before there is any way to *see* what it left behind.
8. **Retention for decided and expired maintenance proposals** (§5.3, §13.7a).
   Independent of any erasure request: `maintenance_proposal` is append-only —
   nothing in `src/` deletes a row — so every proposal ever staged keeps its
   verbatim `payload_json` and `edited_text` **forever**, for facts nobody asked to
   erase, in a table whose stated purpose is a 30-day review queue. That is a
   data-minimisation problem WP5 inherits but did not create, and it is also what
   makes §5.3's detection scan O(all proposals in the namespace) rather than O(50).
   Options: redact `payload_json` on decide/expire once the row's audit value is
   spent; hard-delete rows past a retention horizon; or leave it and accept
   unbounded growth. (Recommendation: redact-on-terminal-status in a WP10a-lane
   follow-up, not in WP5-impl — it touches the maintenance lane's files, and WP5 is
   already lane-A-heavy. Filing it is the point; fixing it here is not.)
9. **Should a failed reclaim raise, or report?** (§6.3.) When
   `wal_checkpoint(TRUNCATE)` comes back busy, the logical erasure has genuinely
   committed but the WAL still holds pre-erasure page images, so `forget()` can
   either return `reclaimed=False` with a warning or raise. Returning keeps the API
   total and lets the caller retry `reclaim()`; raising is louder for the caller who
   is discharging a legal obligation and will not read a field. (Recommendation:
   return `reclaimed=False` from `forget()`, and make the **CLI** exit non-zero on
   it — the operator archiving a JSON report as evidence is the one who most needs
   the failure to be unmissable.)

## 15. Sources

- GDPR Art. 17 (right to erasure), Art. 5(2) (accountability), Recital 26
  (anonymized data outside scope) · ICO guidance on the right to erasure and on
  backups ("put beyond use") · EDPB Opinion 28/2024 §§ on personal data in
  model/derived artifacts (context for treating embeddings as personal data).
- Embedding-inversion literature (text reconstruction from sentence embeddings)
  — the basis for §1.2's "embeddings are personal data" line.
- SQLite docs: `PRAGMA secure_delete`, `VACUUM`, WAL & `wal_checkpoint`,
  `PRAGMA foreign_keys` / `foreign_key_check`, FTS5 `optimize` / `rebuild` /
  `integrity-check` special INSERTs · sqlite-vec 0.1.9 vec0 storage layout.
- Temporal/immutable-store prior art: Datomic `:db/excise` & `:db/noHistory` ·
  XTDB `DELETE` vs `ERASE` · Kafka log compaction & tombstones · RocksDB
  compaction/tombstone dropping · Kleppmann, *DDIA* ch. 11-12.
- Competitor deletion surfaces: Mem0 `delete`/`delete_all`/`history`,
  Cognee `forget`, Zep/Graphiti edge invalidation
  (`docs/competitive-landscape.md`).
- In-repo: `docs/superpowers/specs/2026-07-16-sleep-time-maintenance-design.md`
  (§3.1 visibility theorem, §4.0 verbs, §4.3 derivation cascade, §5 migration
  discipline, §7.3 lease-vs-unlink) · `docs/superpowers/workpackets.md` §WP4-WP6
  · `ARCHITECTURE.md` §Design Decisions.
