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


# Filled once from the first run's printed digests (Step 5), then a test pins
# equality so engine drift turns the suite red.
EXPECTED_SCHEMA_FINGERPRINT = (
    "c12a9560c065a6cc9be19b91b71b4ebee1b74eceff477040073f85943c43742f"
)
EXPECTED_SANITIZER_FINGERPRINT = (
    "dfef8699dd9519f2ef6592be577d25fdb2ae761e298d7063ced3323a14e2cf77"
)
