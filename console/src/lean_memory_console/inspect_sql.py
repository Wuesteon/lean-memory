"""Read-only enumeration SQL over the engine's per-namespace DBs (spec §7),
plus the fail-loud schema/sanitizer tripwires (spec §13).

Connections open with file:...?mode=ro ALWAYS; immutable=1 is a per-request
fallback tried ONLY after mode=ro raises OperationalError (spec §7). Column
names below are verbatim from the installed lean_memory store/schema.py.

Task 4 extends this module with list_facts/get_fact/list_episodes/
get_episode/list_entities.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import sqlite3
from pathlib import Path

from .config import sanitize_namespace


def open_ro(path: Path) -> sqlite3.Connection:
    """Read-only engine connection. mode=ro first; immutable=1 only on
    OperationalError (genuinely read-only media / error-14 path, spec §7)."""
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _fts_query(text: str) -> str:
    """OR-query of QUOTED alnum terms (mirrors engine sqlite_store._fts_query so
    the console's text filter matches the same tokens the engine indexes).

    Quoting matters: FTS5 treats bare AND/OR/NOT/NEAR as operators, so an
    unquoted query with such a token raises a syntax error. A quoted term is an
    inert string literal, still matched case-insensitively by the tokenizer;
    terms are alnum-only after the scrub, so no embedded quote can break out."""
    terms = [
        t for t in "".join(c if c.isalnum() else " " for c in text).split() if t
    ]
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"' for t in terms)


def list_namespaces(data_root: Path, event_log) -> list[dict]:
    """Discover *.db (skipping _*.db), returning per-namespace counts.
    Bare array (unpaginated), ordered by total facts DESC then name (spec §7)."""
    data_root = Path(data_root)
    out: list[dict] = []
    for db_path in sorted(data_root.glob("*.db")):
        if db_path.name.startswith("_"):
            continue
        name = db_path.stem
        conn = open_ro(db_path)
        try:
            facts_latest = conn.execute(
                "SELECT COUNT(*) FROM fact WHERE is_latest=1"
            ).fetchone()[0]
            facts_retired = conn.execute(
                "SELECT COUNT(*) FROM fact WHERE is_latest=0"
            ).fetchone()[0]
            entities = conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
            episodes = conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0]
            chains = conn.execute(
                "SELECT COUNT(*) FROM fact WHERE superseded_by IS NOT NULL"
            ).fetchone()[0]
            top_predicates = [
                {"predicate": r["predicate"], "count": r["n"]}
                for r in conn.execute(
                    "SELECT predicate, COUNT(*) AS n FROM fact "
                    "GROUP BY predicate ORDER BY n DESC, predicate LIMIT 5"
                ).fetchall()
            ]
        finally:
            conn.close()
        file_size = db_path.stat().st_size
        activity = event_log.activity_summary(name)
        out.append(
            {
                "name": name,
                "facts_latest": facts_latest,
                "facts_retired": facts_retired,
                "entities": entities,
                "episodes": episodes,
                "chains": chains,
                "file_size": file_size,
                "top_predicates": top_predicates,
                "activity": activity,
            }
        )
    out.sort(key=lambda n: (-(n["facts_latest"] + n["facts_retired"]), n["name"]))
    return out


def _digest_lines(text: str, predicate) -> str:
    lines = [ln for ln in text.splitlines() if predicate(ln)]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def compute_engine_schema_fingerprint() -> str:
    """sha256 over the CREATE lines of the INSTALLED lean_memory store/schema.py
    (importlib.resources, never a checked-in copy) — the §13 tripwire."""
    text = (
        importlib.resources.files("lean_memory.store")
        .joinpath("schema.py")
        .read_text(encoding="utf-8")
    )
    return _digest_lines(text, lambda ln: "create" in ln.lower())


def compute_sanitizer_fingerprint() -> str:
    """sha256 over memory.py's sanitizer lines (_SAFE_NS / the 'or "default"'
    fallback) — guards the config.py mirror against engine drift (§13)."""
    text = (
        importlib.resources.files("lean_memory")
        .joinpath("memory.py")
        .read_text(encoding="utf-8")
    )
    return _digest_lines(
        text, lambda ln: "_SAFE_NS" in ln or 'or "default"' in ln
    )


def _paginate(page: int, page_size: int) -> tuple[int, int, int]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    return page, page_size, (page - 1) * page_size


def list_facts(
    db_path: Path,
    latest_only: bool = True,
    predicate: str | None = None,
    entity: str | None = None,
    min_salience: float | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Filterable fact list. Rows carry subject = entity.name (joined via
    fact.subject_id). q is an FTS filter over fact_fts (distinct from search).
    Order created_at DESC, id DESC; total is post-filter (spec §7)."""
    page, page_size, offset = _paginate(page, page_size)
    where = ["1=1"]
    args: list = []
    if latest_only:
        where.append("f.is_latest = 1")
    if predicate is not None:
        where.append("f.predicate = ?")
        args.append(predicate)
    if entity is not None:
        where.append("LOWER(e.name) = LOWER(?)")
        args.append(entity)
    if min_salience is not None:
        where.append("f.salience >= ?")
        args.append(min_salience)
    if q is not None:
        where.append(
            "f.id IN (SELECT fact_id FROM fact_fts WHERE fact_fts MATCH ?)"
        )
        args.append(_fts_query(q))
    clause = " AND ".join(where)

    conn = open_ro(db_path)
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM fact f "
            f"JOIN entity e ON f.subject_id = e.id WHERE {clause}",
            args,
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT f.*, e.name AS subject FROM fact f "
            f"JOIN entity e ON f.subject_id = e.id WHERE {clause} "
            f"ORDER BY f.created_at DESC, f.id DESC LIMIT ? OFFSET ?",
            (*args, page_size, offset),
        ).fetchall()
    finally:
        conn.close()
    return {
        "items": [dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def get_fact(db_path: Path, fact_id) -> dict | None:
    """Full fact row + subject name + supersession chain (oldest->newest,
    walked both directions) + source episode (spec §7)."""
    conn = open_ro(db_path)
    try:
        row = conn.execute(
            "SELECT f.*, e.name AS subject FROM fact f "
            "JOIN entity e ON f.subject_id = e.id WHERE f.id = ?",
            (fact_id,),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)

        # Walk backward: follow superseded_by chains that point AT this fact
        # (older facts whose superseded_by == current id), oldest first.
        backward: list[dict] = []
        cur = out["id"]
        while True:
            prev = conn.execute(
                "SELECT * FROM fact WHERE superseded_by = ?", (cur,)
            ).fetchone()
            if prev is None:
                break
            backward.append(dict(prev))
            cur = prev["id"]
        backward.reverse()  # oldest -> ... -> just-before-current

        # Walk forward: follow this fact's superseded_by pointer to newer facts.
        forward: list[dict] = []
        cur_row = dict(row)
        while cur_row.get("superseded_by"):
            nxt = conn.execute(
                "SELECT * FROM fact WHERE id = ?", (cur_row["superseded_by"],)
            ).fetchone()
            if nxt is None:
                break
            forward.append(dict(nxt))
            cur_row = dict(nxt)

        chain = backward + [dict(row)] + forward
        out["chain"] = chain

        episode = conn.execute(
            "SELECT * FROM episode WHERE id = ?", (out["episode_id"],)
        ).fetchone()
        out["episode"] = dict(episode) if episode is not None else None
    finally:
        conn.close()
    return out


def list_episodes(db_path: Path, page: int = 1, page_size: int = 50) -> dict:
    """Episodes ordered t_ref DESC (spec §7)."""
    page, page_size, offset = _paginate(page, page_size)
    conn = open_ro(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM episode ORDER BY t_ref DESC, id DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
    finally:
        conn.close()
    return {
        "items": [dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def get_episode(db_path: Path, episode_id) -> dict | None:
    """One episode + its extracted facts (episode_id match)."""
    conn = open_ro(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM episode WHERE id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        facts = conn.execute(
            "SELECT f.*, e.name AS subject FROM fact f "
            "JOIN entity e ON f.subject_id = e.id WHERE f.episode_id = ? "
            "ORDER BY f.created_at, f.id",
            (episode_id,),
        ).fetchall()
        out["facts"] = [dict(r) for r in facts]
    finally:
        conn.close()
    return out


def list_entities(db_path: Path, page: int = 1, page_size: int = 50) -> dict:
    """Entity names + fact_count (as subject), ordered fact_count DESC then name."""
    page, page_size, offset = _paginate(page, page_size)
    conn = open_ro(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
        rows = conn.execute(
            "SELECT e.id AS id, e.name AS name, e.type AS type, "
            "COUNT(f.id) AS fact_count "
            "FROM entity e LEFT JOIN fact f ON f.subject_id = e.id "
            "GROUP BY e.id, e.name, e.type "
            "ORDER BY fact_count DESC, e.name LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
    finally:
        conn.close()
    return {
        "items": [dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


# Filled once from the first run's printed digests (Step 5), then a test pins
# equality so engine drift turns the suite red.
EXPECTED_SCHEMA_FINGERPRINT = (
    "a6c7f41188a196929ff4e7c257a181c100af9f0f7091aa93d428a9a7c0eec8ef"
)
EXPECTED_SANITIZER_FINGERPRINT = (
    # Bumped for WP10a Task 6: Memory._maintenance_store() reuses the identical
    # `_SAFE_NS.sub("_", namespace) or "default"` expression (memory.py:298),
    # adding a third matching line; sanitizer semantics unchanged, mirror valid.
    "973f203a76ab1b535ae8e81dcb830145c92bf481cf4c815b5fbca0044e5d044c"
)
