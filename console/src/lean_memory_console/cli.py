"""`lean-memory-console` CLI: serve | mcp, plus --print-compose-path."""

from __future__ import annotations

import argparse
import importlib.resources
import os
import sys
from pathlib import Path

from .config import ConsoleConfig, load_config, resolve_data_root
from .observe_mcp import run_stdio


def _compose_path() -> Path:
    """Path to the packaged docker-compose.yml.

    Primary: importlib.resources over the installed package's deploy/ dir
    (the wheel force-includes deploy/docker-compose.yml there). Dev fallback:
    resolve the repo's deploy/docker-compose.yml relative to this file when
    the packaged resource is missing (editable installs may not map the
    force-include).
    """
    try:
        res = importlib.resources.files("lean_memory_console").joinpath(
            "deploy/docker-compose.yml"
        )
        if res.is_file():
            return Path(str(res))
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    # Dev fallback: repo_root/deploy/docker-compose.yml.
    # cli.py -> lean_memory_console -> src -> console -> repo_root
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "deploy" / "docker-compose.yml"


def _validate_serve_root(root: Path) -> None:
    if not root.exists() or not os.access(root, os.R_OK):
        sys.stderr.write(
            f"error: data root not readable: {root}\n"
        )
        raise SystemExit(2)


def _run_server(config: ConsoleConfig, no_open: bool) -> None:  # pragma: no cover
    """Start uvicorn on 127.0.0.1 (real entry; monkeypatched in tests)."""
    import uvicorn

    from .app import create_app
    from .engine import EngineGateway
    from .events import EventLog

    event_log = EventLog(config.data_root)
    gateway = EngineGateway(config, event_log)
    app = create_app(config, gateway, event_log)
    url = f"http://127.0.0.1:{config.port}/?token={config.session_token}"
    if not no_open:
        import webbrowser

        webbrowser.open(url)
    sys.stdout.write(f"lean-memory-console serving at {url}\n")
    try:
        uvicorn.run(app, host="127.0.0.1", port=config.port, log_level="info")
    finally:
        gateway.close()
        event_log.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lean-memory-console")
    parser.add_argument(
        "--print-compose-path", action="store_true",
        help="print the packaged docker-compose.yml path and exit",
    )
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the local read-only console")
    p_serve.add_argument("--root", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--no-open", action="store_true")

    p_mcp = sub.add_parser("mcp", help="run the observing MCP stdio server")
    p_mcp.add_argument("--root", default=None)

    args = parser.parse_args(argv)

    if args.print_compose_path:
        sys.stdout.write(f"{_compose_path()}\n")
        return 0

    if args.command == "serve":
        root = resolve_data_root(args.root)
        _validate_serve_root(root)
        config = load_config("local", cli_root=args.root, port=args.port)
        _run_server(config, no_open=args.no_open)
        return 0

    if args.command == "mcp":
        config = load_config("local", cli_root=args.root)
        run_stdio(config)
        return 0

    parser.print_help()
    return 1
