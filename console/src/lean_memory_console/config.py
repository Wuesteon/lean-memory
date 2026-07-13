"""Console configuration: data-root resolution, namespace sanitizer mirror,
reserved-namespace guard, and mode-specific config loading (spec §5, §7, §10).

SAFE_NS_RE and sanitize_namespace mirror the engine's private sanitizer
(memory.py:38, 70-71). The mirror is guarded by the fail-loud tripwire in
inspect_sql.py (spec §13) so engine drift turns the suite red.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

# Mirror of engine memory.py:38  _SAFE_NS = re.compile(r"[^A-Za-z0-9_.-]")
SAFE_NS_RE: re.Pattern = re.compile(r"[^A-Za-z0-9_.-]")

DEFAULT_DATA_ROOT = Path("~/.lean_memory")
DEFAULT_PORT = 8377


def sanitize_namespace(name: str) -> str:
    """Mirror of memory.py:70-71  safe = _SAFE_NS.sub("_", name) or "default"."""
    return SAFE_NS_RE.sub("_", name) or "default"


def is_reserved_namespace(name: str) -> bool:
    """True when the sanitized namespace begins with '_' (collides with sidecars
    like _events.db); the data plane rejects these (spec §5)."""
    return sanitize_namespace(name).startswith("_")


def ns_db_path(data_root: Path, namespace: str) -> Path:
    """The engine store file for a namespace: root / f'{safe}.db'."""
    return data_root / f"{sanitize_namespace(namespace)}.db"


def resolve_data_root(cli_root: str | None) -> Path:
    """One rule, both commands (spec §10): --root > LM_DATA_ROOT > ~/.lean_memory.
    expanduser is applied; this function never creates the directory."""
    if cli_root:
        return Path(cli_root).expanduser()
    env = os.environ.get("LM_DATA_ROOT")
    if env:
        return Path(env).expanduser()
    return DEFAULT_DATA_ROOT.expanduser()


@dataclass
class ConsoleConfig:
    data_root: Path
    mode: str  # "local" | "docker"
    api_key: str | None = None
    port: int = DEFAULT_PORT
    models: str = "auto"  # "auto" | "stub"
    session_token: str | None = None


def load_config(
    mode: str, cli_root: str | None = None, port: int | None = None
) -> ConsoleConfig:
    """Build a ConsoleConfig for the given mode.

    docker: LM_API_KEY is required — a missing key raises SystemExit(2) with a
            clear message. No per-launch session token.
    local:  a fresh random session_token is minted (embedded in the tokened URL).
    """
    data_root = resolve_data_root(cli_root)
    models = os.environ.get("LM_CONSOLE_MODELS", "auto")
    if models not in ("auto", "stub"):
        models = "auto"
    resolved_port = port if port is not None else DEFAULT_PORT

    if mode == "docker":
        api_key = os.environ.get("LM_API_KEY")
        if not api_key:
            print(
                "LM_API_KEY is required in Docker mode; refusing to boot.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return ConsoleConfig(
            data_root=data_root,
            mode="docker",
            api_key=api_key,
            port=resolved_port,
            models=models,
            session_token=None,
        )

    # local mode
    return ConsoleConfig(
        data_root=data_root,
        mode="local",
        api_key=None,
        port=resolved_port,
        models=models,
        session_token=secrets.token_urlsafe(24),
    )
