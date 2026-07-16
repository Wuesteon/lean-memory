"""`lean-memory-maintain` — the primary sleep-time maintenance trigger (spec §6.1).

    lean-memory-maintain --root PATH [--namespace NS] [--apply] [--auto-only] [--json]

Runs the offline maintenance job over one namespace, or every namespace under a
root, from its OWN process — so cross-process safety comes from the lease +
busy_timeout + short batches (§7), never from an in-process gateway. That is why
this lives in the CORE package beside `lean-memory-mcp`, with no console dependency
(§6.1).

Semantics (spec §6.1, §7.1):
  * `--root` is required, OR defaults to $LM_DATA_ROOT when that env var is set (the
    spec's `--root $LM_DATA_ROOT` idiom). With neither, the CLI errors.
  * DRY-RUN IS THE DEFAULT. Without `--apply`, every namespace is a report-only pass:
    zero writes, no lease row, no proposals — it just computes the would-do report.
  * `--apply` executes the auto band (dedup-exact, evict auto) AND stages proposals.
  * `--auto-only` (only meaningful with `--apply`) runs the auto band and stages
    NOTHING — the provably-safe transforms only.
  * Without `--namespace`, every `*.db` file under the root is processed in turn.
  * Each namespace opens a DEDICATED maintenance store at busy_timeout_ms=5000 via
    `Memory.maintain()` (§7.1) — reused verbatim rather than re-driving the runner, so
    the store construction, dim wiring, lease, and cleanup match the in-process path
    exactly.
  * Human-readable per-namespace report to stdout (this is the CLI's OWN process — its
    stdout is free, unlike the MCP server's JSON-RPC channel). `--json` emits one
    machine-readable object with per-namespace reports and stable keys.
  * Exit 0 on success, including a below-threshold no-op and a "skipped: lease held"
    line (a lease held by another run is a NORMAL outcome, not an error). Nonzero only
    on a real error: an unusable root, or an unexpected exception during a run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from ..memory import Memory
from .config import MaintenanceConfig
from .runner import RunReport


def _resolve_root(arg_root: Optional[str]) -> Path:
    """The data root: --root wins; else $LM_DATA_ROOT (the spec's `--root $LM_DATA_ROOT`
    idiom, §6.1); else an error. Raises SystemExit(2) with a clear message on neither."""
    raw = arg_root if arg_root is not None else os.environ.get("LM_DATA_ROOT")
    if not raw:
        raise SystemExit(
            "error: --root is required (or set LM_DATA_ROOT). "
            "Point it at the directory holding the per-namespace .db files."
        )
    root = Path(raw).expanduser()
    if not root.is_dir():
        raise SystemExit(f"error: root is not a directory: {root}")
    return root


def _discover_namespaces(root: Path) -> list[str]:
    """Every namespace under `root`, derived from its `<namespace>.db` file (BET 4:
    one SQLite file per namespace). The filename stem IS the on-disk (sanitized)
    namespace, so it round-trips through Memory's namespace→path mapping unchanged.
    WAL/SHM sidecars (`*.db-wal`, `*.db-shm`) are NOT separate namespaces — glob only
    the exact `*.db` suffix. Sorted for deterministic output/tests."""
    return sorted(p.stem for p in root.glob("*.db") if p.suffix == ".db")


def _report_to_dict(namespace: str, report: RunReport, *, apply: bool, auto_only: bool) -> dict:
    """A stable, JSON-able summary of one namespace's RunReport (`--json` payload)."""
    tr = report.transform_report
    return {
        "namespace": namespace,
        "status": report.status,  # 'ok' | 'skipped'
        "mode": ("auto-only" if auto_only else "apply") if apply else "dry-run",
        "skipped_reason": report.skipped_reason,
        "below_threshold": report.below_threshold,
        "aborted_file_gone": report.aborted_file_gone,
        "run_id": report.run_id,
        "config_hash": report.config_hash,
        "merges": len(tr.merges) if tr else 0,
        "demoted": len(tr.demoted_ids) if tr else 0,
        "staged": len(tr.proposals) if tr else 0,
        "dropped_proposals": tr.dropped_proposals if tr else 0,
        "threshold_stats": report.threshold_stats,
    }


def _human_line(d: dict) -> str:
    """A one-line human summary of a namespace's report dict (stdout, non-JSON mode)."""
    ns = d["namespace"]
    if d["status"] == "skipped":
        return f"{ns}: skipped ({d['skipped_reason']})"
    if d["below_threshold"]:
        return f"{ns}: no-op (below threshold)"
    tail = ""
    if d["aborted_file_gone"]:
        tail = "  [stopped: namespace cleared mid-run]"
    return (
        f"{ns}: {d['mode']}  "
        f"merges={d['merges']} demoted={d['demoted']} "
        f"staged={d['staged']} dropped={d['dropped_proposals']}{tail}"
    )


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="lean-memory-maintain",
        description="Run the sleep-time maintenance job (dry-run by default).",
    )
    p.add_argument(
        "--root",
        default=None,
        help="Data root holding per-namespace .db files (defaults to $LM_DATA_ROOT).",
    )
    p.add_argument(
        "--namespace",
        default=None,
        help="Maintain only this namespace (default: every namespace under the root).",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Execute the auto band AND stage proposals (default: dry-run, no writes).",
    )
    p.add_argument(
        "--auto-only",
        action="store_true",
        dest="auto_only",
        help="With --apply: run ONLY the auto band; stage nothing.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit one machine-readable JSON object with per-namespace reports.",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console-script / module entry point. `argv=None` reads sys.argv (testability).

    Returns the process exit code (0 success, nonzero error). `_resolve_root` raises
    SystemExit(2) on a bad/missing root — argparse's own convention — which the console
    script surfaces as a nonzero exit with the message on stderr.
    """
    args = _parse_args(argv)
    root = _resolve_root(args.root)  # SystemExit(2) on bad/missing root

    if args.namespace is not None:
        namespaces = [args.namespace]
    else:
        namespaces = _discover_namespaces(root)

    config = MaintenanceConfig()
    mem = Memory(root=root)
    results: list[dict] = []
    exit_code = 0
    try:
        for ns in namespaces:
            try:
                report = mem.maintain(
                    ns, config=config, apply=args.apply, auto_only=args.auto_only,
                )
            except Exception as exc:  # a genuine run error — report it, keep going.
                exit_code = 1
                results.append({
                    "namespace": ns,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
            results.append(
                _report_to_dict(ns, report, apply=args.apply, auto_only=args.auto_only)
            )
    finally:
        mem.close()

    if args.as_json:
        print(json.dumps({"root": str(root), "namespaces": results}, sort_keys=True))
    else:
        if not results:
            print(f"no namespaces found under {root}")
        for d in results:
            if d.get("status") == "error":
                print(f"{d['namespace']}: error ({d['error']})", file=sys.stderr)
            else:
                print(_human_line(d))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
