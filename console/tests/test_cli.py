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
