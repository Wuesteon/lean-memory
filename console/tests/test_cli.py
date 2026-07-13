import os
from pathlib import Path

import pytest

from lean_memory_console import cli


def test_print_compose_path_exists(capsys):
    rc = cli.main(["--print-compose-path"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out
    assert Path(out).exists()


def test_print_compose_path_prefers_packaged_resource():
    # When the wheel force-include has materialized deploy/docker-compose.yml
    # inside the installed package, _compose_path() must resolve THAT copy (the
    # packaged branch), not the dev fallback. Editable installs don't map the
    # force-include, so we materialize it temporarily to exercise the branch.
    import importlib.resources

    pkg_deploy = Path(
        str(importlib.resources.files("lean_memory_console").joinpath("deploy"))
    )
    packaged = pkg_deploy / "docker-compose.yml"
    already = packaged.exists()
    if not already:
        packaged.write_text("# packaged copy (branch-coverage fixture)\n")
    try:
        resolved = cli._compose_path()
        assert resolved == packaged
        assert resolved.is_file()
    finally:
        if not already:
            packaged.unlink()


def test_print_compose_path_dev_fallback(monkeypatch):
    # With no packaged resource (the editable-install reality), _compose_path()
    # falls back to the repo's deploy/docker-compose.yml resolved relative to
    # this file. Force the primary branch to miss so the fallback is exercised
    # regardless of whether a wheel materialized the packaged copy.
    import importlib.resources

    real_files = importlib.resources.files

    def files_missing_deploy(pkg):
        base = real_files(pkg)
        if pkg == "lean_memory_console":

            class _NoFile:
                def joinpath(self, *_a, **_k):
                    return self

                def is_file(self):
                    return False

            return _NoFile()
        return base

    monkeypatch.setattr(importlib.resources, "files", files_missing_deploy)
    resolved = cli._compose_path()
    assert resolved.name == "docker-compose.yml"
    assert resolved.parent.name == "deploy"
    assert resolved.exists()


def test_serve_boot_fails_on_unreadable_root(tmp_path, monkeypatch):
    bad = tmp_path / "nope"  # does not exist and cannot be read
    with pytest.raises(SystemExit) as ei:
        cli.main(["serve", "--root", str(bad)])
    assert ei.value.code == 2


def test_docker_mode_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("LM_API_KEY", raising=False)
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    # 'mcp' subcommand in a docker context: load_config('docker') must exit 2.
    # We drive load_config directly through a docker boot to assert exit 2.
    from lean_memory_console.config import load_config

    with pytest.raises(SystemExit) as ei:
        load_config("docker", cli_root=str(tmp_path))
    assert ei.value.code == 2


def test_mcp_subcommand_wires_run_stdio(tmp_path, monkeypatch):
    called = {}

    def fake_run_stdio(config):
        called["root"] = config.data_root

    monkeypatch.setattr(cli, "run_stdio", fake_run_stdio)
    rc = cli.main(["mcp", "--root", str(tmp_path)])
    assert rc == 0
    assert called["root"] == tmp_path


def test_serve_boot_ok_readable_root(tmp_path, monkeypatch):
    # serve requires only readability; a fresh writable dir passes and the
    # server start is monkeypatched so we don't block.
    started = {}

    def fake_serve(config, no_open):
        started["port"] = config.port

    monkeypatch.setattr(cli, "_run_server", fake_serve)
    rc = cli.main(["serve", "--root", str(tmp_path), "--no-open", "--port", "9999"])
    assert rc == 0
    assert started["port"] == 9999


def test_serve_default_is_local_mode(tmp_path, monkeypatch):
    # Without --docker, serve must load LOCAL mode (per-launch token, no bearer).
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    captured = {}
    monkeypatch.setattr(cli, "_run_server", lambda config, no_open: captured.update(mode=config.mode))
    rc = cli.main(["serve", "--root", str(tmp_path), "--no-open"])
    assert rc == 0
    assert captured["mode"] == "local"


def test_serve_docker_requires_api_key(tmp_path, monkeypatch):
    # `serve --docker` without LM_API_KEY must fail fast with exit 2 (the same
    # boot validation load_config('docker') enforces) — the container refuses
    # to start rather than silently running unauthenticated.
    monkeypatch.delenv("LM_API_KEY", raising=False)
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    # _run_server must NOT be reached; if boot validation regresses this fails loud.
    monkeypatch.setattr(
        cli, "_run_server",
        lambda *a, **k: pytest.fail("serve --docker booted without LM_API_KEY"),
    )
    with pytest.raises(SystemExit) as ei:
        cli.main(["serve", "--docker", "--root", str(tmp_path), "--no-open"])
    assert ei.value.code == 2


def test_serve_docker_loads_docker_mode_config(tmp_path, monkeypatch):
    # With LM_API_KEY set, `serve --docker` hands _run_server a docker-mode config.
    monkeypatch.setenv("LM_API_KEY", "secret-xyz")
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    captured = {}

    def fake_serve(config, no_open):
        captured["mode"] = config.mode
        captured["api_key"] = config.api_key
        captured["session_token"] = config.session_token

    monkeypatch.setattr(cli, "_run_server", fake_serve)
    rc = cli.main(["serve", "--docker", "--root", str(tmp_path), "--no-open"])
    assert rc == 0
    assert captured["mode"] == "docker"
    assert captured["api_key"] == "secret-xyz"
    assert captured["session_token"] is None  # no per-launch token in docker mode


def test_run_server_binds_0_0_0_0_in_docker_mode(tmp_path, monkeypatch):
    # _run_server derives the bind host from config.mode: docker -> 0.0.0.0 so the
    # container is reachable via published ports (the local Host guard does not
    # run in docker mode; bearer + MCP allowlist are the controls). Local stays
    # loopback. Assert the host actually handed to uvicorn.run.
    #
    # _run_server does *function-local* imports (import uvicorn; from .app import
    # create_app; from .engine import EngineGateway; from .events import
    # EventLog), so patch each at its source module / sys.modules.
    import sys
    import types

    import lean_memory_console.app as app_mod
    import lean_memory_console.cli as cli_mod
    import lean_memory_console.engine as engine_mod
    import lean_memory_console.events as events_mod
    from lean_memory_console.config import ConsoleConfig

    captured = {}

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, host, port, log_level: captured.update(host=host)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    class _Noop:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

    monkeypatch.setattr(engine_mod, "EngineGateway", _Noop)
    monkeypatch.setattr(events_mod, "EventLog", _Noop)
    monkeypatch.setattr(app_mod, "create_app", lambda *a, **k: object())

    docker_cfg = ConsoleConfig(
        data_root=tmp_path, mode="docker", api_key="k", port=8377, session_token=None
    )
    cli_mod._run_server(docker_cfg, no_open=True)
    assert captured["host"] == "0.0.0.0"

    local_cfg = ConsoleConfig(
        data_root=tmp_path, mode="local", api_key=None, port=8377, session_token="tok"
    )
    cli_mod._run_server(local_cfg, no_open=True)
    assert captured["host"] == "127.0.0.1"
