"""The _events.db sidecar (spec §5): schema, event recording, atomic retention,
and read helpers. Console-owned (the engine never touches this file).

All connections set busy_timeout=5000 because the engine sets none (spec §6).
record() NEVER raises — recording an event must not mask the operation's own
result; on any failure it degrades to a logged warning and returns None.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

CAP = 10_000

_log = logging.getLogger("lean_memory_console.events")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
    id          INTEGER PRIMARY KEY,
    namespace   TEXT,
    ts          INTEGER,
    kind        TEXT CHECK(kind IN ('add','search')),
    duration_ms REAL,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS ix_event_ns_ts ON event(namespace, ts);
"""

_PRUNE_SQL = (
    "DELETE FROM event WHERE namespace=? AND id NOT IN ("
    "SELECT id FROM event WHERE namespace=? ORDER BY ts DESC, id DESC LIMIT 10000)"
)


class EventLog:
    def __init__(self, data_root: Path) -> None:
        self.path = Path(data_root) / "_events.db"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def record(self, namespace: str, kind: str, duration_ms: float, payload: dict) -> None:
        """Insert one event, then prune if this namespace is over CAP. Never raises."""
        try:
            ts = int(time.time() * 1000)
            blob = json.dumps(payload)
            with self._lock:
                self._db.execute(
                    "INSERT INTO event(namespace, ts, kind, duration_ms, payload) "
                    "VALUES (?,?,?,?,?)",
                    (namespace, ts, kind, duration_ms, blob),
                )
                count = self._db.execute(
                    "SELECT COUNT(*) FROM event WHERE namespace=?", (namespace,)
                ).fetchone()[0]
                if count > CAP:
                    self._db.execute(_PRUNE_SQL, (namespace, namespace))
                self._db.commit()
        except Exception as exc:  # noqa: BLE001 — must never mask the caller's result
            _log.warning("event record failed (%s): %s", kind, exc)
            return None
        return None

    def list_events(
        self, namespace: str, kind: str | None = None, page: int = 1, page_size: int = 50
    ) -> dict:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        where = "WHERE namespace=?"
        args: list = [namespace]
        if kind is not None:
            where += " AND kind=?"
            args.append(kind)
        with self._lock:
            total = self._db.execute(
                f"SELECT COUNT(*) FROM event {where}", args
            ).fetchone()[0]
            rows = self._db.execute(
                f"SELECT id, namespace, ts, kind, duration_ms, payload FROM event "
                f"{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                (*args, page_size, (page - 1) * page_size),
            ).fetchall()
        items = []
        for r in rows:
            items.append(
                {
                    "id": r["id"],
                    "namespace": r["namespace"],
                    "ts": r["ts"],
                    "kind": r["kind"],
                    "duration_ms": r["duration_ms"],
                    "payload": json.loads(r["payload"]) if r["payload"] else {},
                }
            )
        return {"items": items, "page": page, "page_size": page_size, "total": total}

    def activity_summary(self, namespace: str, days: int = 7) -> dict:
        """Adds/searches in the window, EXCLUDING payload origin == 'ui' (spec §7).
        The window bound is applied via ts; earliest_ts is over all stored events."""
        since = int(time.time() * 1000) - days * 86_400_000
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, payload FROM event WHERE namespace=? AND ts >= ?",
                (namespace, since),
            ).fetchall()
            earliest = self._db.execute(
                "SELECT MIN(ts) FROM event WHERE namespace=?", (namespace,)
            ).fetchone()[0]
        adds = 0
        searches = 0
        for r in rows:
            payload = json.loads(r["payload"]) if r["payload"] else {}
            if payload.get("origin") == "ui":
                continue
            if r["kind"] == "add":
                adds += 1
            elif r["kind"] == "search":
                searches += 1
        return {"adds": adds, "searches": searches, "earliest_ts": earliest}

    def close(self) -> None:
        with self._lock:
            self._db.close()
