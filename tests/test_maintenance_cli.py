"""Task 7 — `lean-memory-maintain` CLI + `memory_clear` lease-refusal (spec §6.1, §7.3).

All offline (FakeEmbedder, deterministic stub summarizer) against real SqliteStores.
Pins:

  - CLI dry-run IS the default: no --apply ⇒ ZERO spine writes and NO lease row
    (full-DB dump byte-identical, no maintenance_run row).
  - --apply performs the auto band AND stages proposals; --apply --auto-only performs
    the auto band but stages NOTHING (the --auto-only switch threads down to
    run_transforms and skips the propose phases).
  - --json emits one parseable object with per-namespace reports carrying stable keys.
  - A missing root (no --root, no $LM_DATA_ROOT) exits nonzero with a message on stderr.
  - Without --namespace, every *.db under the root is processed.
  - CROSS-PROCESS (the headline): the CLI runs as a REAL subprocess doing --apply while
    the parent process holds a live store writer doing bounded adds — they interleave
    without an unhandled `database is locked` (busy_timeout 5000 + short batches absorb
    the contention). Both complete; CLI exits 0 with a valid report.
  - memory_clear refuses while a LIVE fresh maintenance lease is held (file survives);
    a STALE lease lets the clear proceed (file removed).
  - The runner stops cleanly at a batch boundary when the namespace file is unlinked
    mid-run (no exception escapes; the report notes aborted_file_gone).

Time is injected where staleness matters so no test sleeps on real wall-clock.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

import pytest

from lean_memory.embed.fake import FakeEmbedder
from lean_memory.maintain import MaintenanceConfig, MaintenanceRunner, live_lease_is_fresh
from lean_memory.maintain.cli import main as cli_main
from lean_memory.store.sqlite_store import SqliteStore
from lean_memory.types import Entity, Episode, Fact

NOW = 2_000_000_000_000  # fixed wall-clock (epoch ms)


# ── seeding helpers ────────────────────────────────────────────────────────────
def _open(path):
    emb = FakeEmbedder()
    return SqliteStore(path, dim=emb.dim, coarse_dim=emb.coarse_dim), emb


def _seed_namespace(path, *, with_near_dup=True):
    """Create a namespace DB with an exact-dup pair (auto-merge target) and, optionally,
    a near-dup pair (propose target). Returns the loser id of the exact-dup pair so
    tests can assert it was retired. A fresh DB has NO prior finished run, so the CLI's
    first run is unconditionally over threshold (§6.6) even at the default config."""
    store, emb = _open(path)
    ep = Episode(namespace="ns", raw="seed", t_ref=1_000)
    store.add_episode(ep)
    subj = store.upsert_entity(Entity(namespace="ns", name="user", type="person"))

    def add(text, *, predicate, valid_at, embed_text=None):
        f = Fact(
            namespace="ns", subject_id=subj.id, predicate=predicate, fact_text=text,
            valid_at=valid_at, episode_id=ep.id, salience=5.0,
        )
        full, coarse = emb.embed_with_coarse(embed_text if embed_text is not None else text)
        store.add_fact(f, full, coarse)
        return f

    # Exact-dup pair (same normalized value, differing case/whitespace) → AUTO merge.
    survivor = add("user works at acme", predicate="works_at", valid_at=1_000)
    loser = add("USER  works at ACME", predicate="works_at", valid_at=2_000)

    if with_near_dup:
        # Near-dup pair (distinct texts, identical embed vector ⇒ cosine 1.0 >= tau_near)
        # → a DEDUP-NEAR *proposal* (never auto-applied).
        add("likes jazz music", predicate="likes", valid_at=1_000, embed_text="MUSICVEC")
        add("likes jazz tunes", predicate="likes", valid_at=2_000, embed_text="MUSICVEC")

    store.close()
    return survivor.id, loser.id


def _fact_dump(path):
    store, _ = _open(path)
    try:
        rows = store._db.execute(
            "SELECT id, is_latest, valid_at, valid_to, superseded_by, tier, "
            "access_count, last_access, record_kind FROM fact ORDER BY id"
        ).fetchall()
        return [tuple(r) for r in rows]
    finally:
        store.close()


def _run_count(path):
    store, _ = _open(path)
    try:
        return store._db.execute("SELECT COUNT(*) AS n FROM maintenance_run").fetchone()["n"]
    finally:
        store.close()


def _proposal_count(path):
    store, _ = _open(path)
    try:
        return store._db.execute(
            "SELECT COUNT(*) AS n FROM maintenance_proposal"
        ).fetchone()["n"]
    finally:
        store.close()


def _loser_row(path, loser_id):
    store, _ = _open(path)
    try:
        return store._db.execute(
            "SELECT is_latest, superseded_by FROM fact WHERE id=?", (loser_id,)
        ).fetchone()
    finally:
        store.close()


# ── dry-run IS the default (zero writes, no lease) ─────────────────────────────
def test_dry_run_default_writes_nothing(tmp_path, capsys):
    path = tmp_path / "ns.db"
    _seed_namespace(path)
    before = _fact_dump(path)

    rc = cli_main(["--root", str(tmp_path), "--namespace", "ns"])  # no --apply

    assert rc == 0
    # ZERO spine delta: the fact table is byte-identical.
    assert _fact_dump(path) == before
    # And NO lease row — a dry-run writes nothing, so it takes no lease.
    assert _run_count(path) == 0
    assert _proposal_count(path) == 0
    out = capsys.readouterr().out
    assert "ns:" in out
    assert "dry-run" in out


# ── --apply performs autos AND stages proposals ────────────────────────────────
def test_apply_merges_and_stages(tmp_path):
    path = tmp_path / "ns.db"
    _survivor, loser = _seed_namespace(path)

    rc = cli_main(["--root", str(tmp_path), "--namespace", "ns", "--apply"])

    assert rc == 0
    # The exact-dup loser was auto-retired (verb (c)).
    row = _loser_row(path, loser)
    assert row["is_latest"] == 0
    assert row["superseded_by"] is not None
    # And the near-dup pair was STAGED as a proposal.
    assert _proposal_count(path) >= 1
    # A lease row exists and finished 'ok'.
    assert _run_count(path) == 1


# ── --apply --auto-only performs autos but stages NOTHING ──────────────────────
def test_apply_auto_only_stages_nothing_but_merges(tmp_path):
    path = tmp_path / "ns.db"
    _survivor, loser = _seed_namespace(path)

    rc = cli_main(["--root", str(tmp_path), "--namespace", "ns", "--apply", "--auto-only"])

    assert rc == 0
    # Auto merge still happened.
    row = _loser_row(path, loser)
    assert row["is_latest"] == 0
    assert row["superseded_by"] is not None
    # But NOTHING was staged — the propose phases were skipped.
    assert _proposal_count(path) == 0


# ── --json parses with stable per-namespace fields ─────────────────────────────
def test_json_output_parses(tmp_path, capsys):
    _seed_namespace(tmp_path / "ns.db")

    rc = cli_main(["--root", str(tmp_path), "--namespace", "ns", "--apply", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["root"] == str(tmp_path)
    assert isinstance(payload["namespaces"], list)
    rep = payload["namespaces"][0]
    for key in (
        "namespace", "status", "mode", "below_threshold", "aborted_file_gone",
        "merges", "demoted", "staged", "dropped_proposals", "threshold_stats",
    ):
        assert key in rep, key
    assert rep["namespace"] == "ns"
    assert rep["status"] == "ok"
    assert rep["mode"] == "apply"


# ── all namespaces under the root when --namespace is omitted ──────────────────
def test_all_namespaces_processed(tmp_path, capsys):
    _seed_namespace(tmp_path / "alpha.db")
    _seed_namespace(tmp_path / "beta.db")

    rc = cli_main(["--root", str(tmp_path), "--json"])  # dry-run, all namespaces

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    names = {r["namespace"] for r in payload["namespaces"]}
    assert names == {"alpha", "beta"}


# ── root resolution errors ─────────────────────────────────────────────────────
def test_missing_root_errors_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LM_DATA_ROOT", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli_main([])  # no --root, no env
    # SystemExit with a nonzero/message payload (argparse-style).
    assert exc.value.code != 0
    # The message is the SystemExit payload (a string) — surfaced on stderr by the
    # console-script wrapper. Assert it mentions the root requirement.
    assert "root" in str(exc.value.code).lower()


def test_missing_namespace_errors_without_creating_file(tmp_path, capsys):
    """An EXPLICIT --namespace whose .db does not exist exits 2 with a message and
    creates NO file — maintaining a never-written namespace must not conjure an empty DB."""
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(SystemExit) as exc:
        cli_main(["--root", str(tmp_path), "--namespace", "ghost"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "ghost" in err
    # No empty DB (or sidecars) left behind — the root directory is unchanged.
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (tmp_path / "ghost.db").exists()


def test_root_from_env(tmp_path, monkeypatch, capsys):
    _seed_namespace(tmp_path / "ns.db")
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    rc = cli_main(["--namespace", "ns", "--json"])  # --root omitted; env supplies it
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["root"] == str(tmp_path)


# ── CROSS-PROCESS: subprocess CLI --apply vs a live in-process writer ──────────
def test_cross_process_cli_vs_live_writer(tmp_path):
    """The headline (§7.1): a REAL subprocess runs the CLI --apply while THIS process
    holds a live store writer doing bounded adds. They interleave without an unhandled
    `database is locked` — busy_timeout 5000 + short batches absorb the contention.
    Both complete; the CLI exits 0 with a valid report."""
    from lean_memory import Memory

    path = tmp_path / "live.db"
    _seed_namespace(path)

    # Launch the CLI --apply as a non-blocking subprocess, THEN drive live adds for as
    # long as it runs (plus a bounded floor) so the two genuinely overlap on the one
    # file. The whole Memory lifecycle (open → add → close) lives ON the writer thread —
    # SQLite pins a connection to its creating thread. A `database is locked` on either
    # side surfaces as an add error or a nonzero subprocess exit.
    add_errors: list[BaseException] = []

    proc = subprocess.Popen(
        [sys.executable, "-m", "lean_memory.maintain.cli",
         "--root", str(tmp_path), "--namespace", "live", "--apply", "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    def _writer():
        mem = Memory(root=tmp_path)
        try:
            i = 0
            # Bounded: at least 20 adds, and keep going while the subprocess is alive
            # (cap at 200 so a hung child can never make this unbounded).
            while i < 200 and (i < 20 or proc.poll() is None):
                mem.add("live", f"I use widget number {i}.")
                i += 1
        except BaseException as exc:
            add_errors.append(exc)
        finally:
            mem.close()

    t = threading.Thread(target=_writer)
    t.start()
    try:
        out, err = proc.communicate(timeout=60)
    finally:
        t.join(timeout=30)

    # No unhandled lock error on either side.
    assert not add_errors, add_errors
    assert proc.returncode == 0, (out, err)
    assert "database is locked" not in (out + err).lower()
    # Valid JSON report from the subprocess.
    payload = json.loads(out)
    rep = payload["namespaces"][0]
    assert rep["namespace"] == "live"
    assert rep["status"] == "ok"


# ── memory_clear lease-refusal (spec §7.3) ─────────────────────────────────────
def _stamp_live_lease(path, heartbeat_ms):
    """Insert a live (status='running') maintenance_run row with the given heartbeat."""
    store, _ = _open(path)
    try:
        run_id = store.create_run("ns", "cli", heartbeat_ms, MaintenanceConfig().config_hash())
        store.heartbeat_run(run_id, heartbeat_ms)
        return run_id
    finally:
        store.close()


def test_live_lease_helper_freshness(tmp_path):
    """The shared staleness helper: a fresh heartbeat is live; a >5min-old one is not."""
    path = tmp_path / "ns.db"
    _seed_namespace(path, with_near_dup=False)
    _stamp_live_lease(path, NOW)

    store, _ = _open(path)
    try:
        assert live_lease_is_fresh(store, "ns", NOW) is True
        # 6 minutes later → stale (past the 5-minute floor).
        assert live_lease_is_fresh(store, "ns", NOW + 6 * 60_000) is False
    finally:
        store.close()


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The MCP server module rooted at tmp with stub backends (mirrors test_mcp_server)."""
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    import importlib

    import lean_memory.mcp_server as srv

    importlib.reload(srv)
    from lean_memory import Memory

    srv._MEM = Memory(root=tmp_path)
    yield srv
    srv._MEM.close()


async def _call(server, name, args):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
        result = await session.call_tool(name, args)
        return result.content[0].text


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_memory_clear_refuses_under_live_lease(server, tmp_path):
    from lean_memory.types import now_ms

    # Seed the namespace file the server will resolve for 'ns'.
    path = server._namespace_path("ns")
    _seed_namespace(path, with_near_dup=False)
    # A live lease with a fresh (now) heartbeat.
    _stamp_live_lease(path, now_ms())

    out = await _call(server, "memory_clear", {"namespace": "ns"})

    assert out.startswith("refused:")
    assert path.exists()  # the file is untouched


@pytest.mark.anyio
async def test_memory_clear_proceeds_under_stale_lease(server, tmp_path):
    path = server._namespace_path("ns")
    _seed_namespace(path, with_near_dup=False)
    # A live-status row but a heartbeat 10 minutes in the PAST → stale → clear proceeds.
    from lean_memory.types import now_ms

    _stamp_live_lease(path, now_ms() - 10 * 60_000)

    out = await _call(server, "memory_clear", {"namespace": "ns"})

    assert out == "cleared namespace 'ns'"
    assert not path.exists()


# ── runner stops cleanly when the file is unlinked mid-run (§7.3) ──────────────
def test_runner_stops_when_file_unlinked_mid_run(tmp_path):
    """Unlink the namespace file at the first batch boundary (via on_batch); the runner
    must STOP cleanly — no exception escapes, report notes aborted_file_gone, and the
    already-committed batches stand."""
    path = tmp_path / "ns.db"
    # Seed enough exact-dup slots that the auto phase runs at least one batch.
    _seed_namespace(path)

    store, emb = _open(path)
    cfg = MaintenanceConfig(min_new_facts=1)  # trip threshold on the first run anyway
    runner = MaintenanceRunner(store, "ns", config=cfg, trigger="cli")

    fired = {"n": 0}

    def _on_batch():
        fired["n"] += 1
        # Unlink the DB (and sidecars) at the first boundary — simulating memory_clear.
        for p in (path, path.with_suffix(".db-wal"), path.with_suffix(".db-shm")):
            p.unlink(missing_ok=True)

    # Must NOT raise — the runner catches the internal cleared-signal and reports it.
    report = runner.run(on_batch=_on_batch)
    store.close()

    assert fired["n"] >= 1
    assert report.status == "ok"
    assert report.aborted_file_gone is True
