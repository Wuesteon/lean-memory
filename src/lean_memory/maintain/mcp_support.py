"""Model-free helpers for the MCP maintenance surfaces (design spec §6.3, §6.5).

Two things the MCP tools need that must NOT drag in the lazy model build:

  * ``read_status(root, namespace)`` — the ledger-only status read behind
    ``memory_maintenance_status``. It opens a raw read-only SQLite connection to the
    namespace file and counts runs + pending proposals; it never constructs a
    ``Memory`` or a ``SqliteStore`` (both of which want an embedder and its dims).
    Forcing the ~2 GB model download to answer "when did maintenance last run?" is
    exactly the v0.1.3-class mistake; the status tool is pinned model-free by
    ``tests/test_mcp_maintenance_tools.py``.

  * ``is_stale(root, namespace, config)`` + ``spawn_maintenance(...)`` — the opt-in
    auto-spawn primitives behind ``LM_MAINT_AUTO`` (§6.5). ``is_stale`` reuses the same
    cheap ledger read; ``spawn_maintenance`` fires the CLI detached with the EXACT
    Popen primitives the spec pins — fd 1 (the JSON-RPC channel) is NEVER inherited.

All raw SQL for these read paths lives HERE, keeping ``mcp_server.py`` SQL-free.
The namespace→file mapping mirrors ``Memory._namespace_path`` (BET 4: one file per
namespace); the sanitizer is the same ``_SAFE_NS`` regex.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .config import MS_PER_DAY, MaintenanceConfig

# Mirror of memory._SAFE_NS / console config.SAFE_NS_RE — the on-disk namespace name.
_SAFE_NS = re.compile(r"[^A-Za-z0-9_.-]")


def namespace_path(root: Path, namespace: str) -> Path:
    """The SQLite file backing a namespace under ``root`` (mirrors Memory's naming)."""
    safe = _SAFE_NS.sub("_", namespace) or "default"
    return root / f"{safe}.db"


def _ro_connect(path: Path) -> Optional[sqlite3.Connection]:
    """Open a read-only connection to an EXISTING namespace file, or None.

    A namespace with no DB file yet (never written) is not an error for a status
    read — it just has zero runs and zero proposals. `mode=ro` refuses to create the
    file, and a missing file surfaces as OperationalError, which we map to None.
    """
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    con.row_factory = sqlite3.Row
    return con


def read_status(root: Path, namespace: str) -> dict:
    """Ledger-only maintenance status for a namespace — NO model build (§6.3).

    Returns a JSON-friendly dict:
        {
          "namespace": <ns>,
          "runs": <total maintenance_run rows>,
          "pending_proposals": <status='pending' count>,
          "last_run": {id, status, started_at, finished_at, trigger} | None,
        }
    A never-written namespace (no file) or a v1-schema DB predating the ledger tables
    reports zeros and last_run=None rather than raising — the honest empty answer.
    """
    empty = {
        "namespace": namespace,
        "runs": 0,
        "pending_proposals": 0,
        "last_run": None,
    }
    con = _ro_connect(namespace_path(root, namespace))
    if con is None:
        return empty
    try:
        # A pre-v2 DB has no ledger tables; treat a missing table as "no maintenance".
        try:
            runs = con.execute("SELECT COUNT(*) FROM maintenance_run").fetchone()[0]
            pending = con.execute(
                "SELECT COUNT(*) FROM maintenance_proposal WHERE status='pending'"
            ).fetchone()[0]
            last = con.execute(
                "SELECT id, status, started_at, finished_at, trigger "
                "FROM maintenance_run "
                "ORDER BY COALESCE(finished_at, started_at) DESC, id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return empty
        return {
            "namespace": namespace,
            "runs": runs,
            "pending_proposals": pending,
            "last_run": dict(last) if last else None,
        }
    finally:
        con.close()


def is_stale(root: Path, namespace: str, config: MaintenanceConfig) -> bool:
    """A single cheap ledger read: is this namespace due for a maintenance run? (§6.5)

    Stale iff there is NO finished (status='ok') run ever, OR the last one finished
    more than ``config.max_days_between_runs`` days ago. This is the same staleness
    signal the runner's age threshold uses, read here without any model build so
    auto-spawn can decide in microseconds on the first tool call.
    """
    con = _ro_connect(namespace_path(root, namespace))
    if con is None:
        return True  # never written / no DB → a first run is due if there's anything
    try:
        try:
            row = con.execute(
                "SELECT finished_at FROM maintenance_run WHERE status='ok' "
                "ORDER BY finished_at DESC, id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return True  # pre-v2 DB with no ledger table → never maintained
    finally:
        con.close()
    if row is None or row["finished_at"] is None:
        return True
    import time

    now_ms = int(time.time() * 1000)
    age_days = (now_ms - row["finished_at"]) / MS_PER_DAY
    return age_days >= config.max_days_between_runs


def spawn_maintenance(root: Path, namespace: str) -> subprocess.Popen:
    """Fire the maintenance CLI detached, apply+auto-only, with the EXACT §6.5
    Popen primitives — fd 1 (the JSON-RPC channel) is NEVER inherited.

    ASYMMETRY (deliberate, §6.3/§6.5): the interactive `memory_maintenance_run` tool
    with apply=True runs the auto band AND stages judgment-call proposals for review.
    This auto-spawn path fires `--apply --auto-only` instead — the auto band ONLY, no
    proposals — because it triggers unattended (on a stale-namespace tool call) and
    must never accumulate a review queue nobody asked for.

    ``stdin=DEVNULL, stdout=DEVNULL`` (the v0.1.3 stdout-hygiene rule: the child must
    not write to the parent's fd 1), ``stderr`` to a per-root log file if one can be
    opened else DEVNULL, ``start_new_session=True`` (own session — reparented on parent
    exit, no zombies since the parent never waits), ``close_fds=True`` (no inherited
    descriptors). The parent NEVER waits on the returned handle.
    """
    log_path = root / "maintenance-autospawn.log"
    try:
        stderr = open(log_path, "ab")  # noqa: SIM115 — handle owned by the child
    except OSError:
        stderr = subprocess.DEVNULL
    return subprocess.Popen(
        [
            sys.executable, "-m", "lean_memory.maintain.cli",
            "--root", str(root),
            "--namespace", namespace,
            "--apply", "--auto-only",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )
