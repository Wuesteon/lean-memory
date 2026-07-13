import re

import pytest

from lean_memory_console import config as cfg


def test_safe_ns_re_mirrors_engine_charclass():
    # Mirror of memory.py:38  _SAFE_NS = re.compile(r"[^A-Za-z0-9_.-]")
    assert isinstance(cfg.SAFE_NS_RE, re.Pattern)
    assert cfg.SAFE_NS_RE.pattern == r"[^A-Za-z0-9_.-]"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a b", "a_b"),
        ("", "default"),
        ("_events", "_events"),  # leading underscore preserved (engine parity)
        ("Project.One-2", "Project.One-2"),
        ("weird/slash:x", "weird_slash_x"),
    ],
)
def test_sanitize_namespace_matches_engine(raw, expected):
    assert cfg.sanitize_namespace(raw) == expected


@pytest.mark.parametrize(
    "raw,reserved",
    [
        ("_events", True),
        ("__x", True),
        ("a", False),
        ("", False),  # sanitizes to "default", not reserved
    ],
)
def test_is_reserved_namespace(raw, reserved):
    assert cfg.is_reserved_namespace(raw) is reserved


def test_ns_db_path(tmp_path):
    assert cfg.ns_db_path(tmp_path, "a b") == tmp_path / "a_b.db"
    assert cfg.ns_db_path(tmp_path, "") == tmp_path / "default.db"


def test_resolve_data_root_precedence(tmp_path, monkeypatch):
    cli_root = tmp_path / "cli"
    env_root = tmp_path / "env"
    monkeypatch.setenv("LM_DATA_ROOT", str(env_root))
    # --root wins over env
    assert cfg.resolve_data_root(str(cli_root)) == cli_root
    # env wins over default when no --root
    assert cfg.resolve_data_root(None) == env_root
    # default when neither
    monkeypatch.delenv("LM_DATA_ROOT", raising=False)
    assert cfg.resolve_data_root(None) == (Path.home() / ".lean_memory")


def test_resolve_data_root_expanduser(monkeypatch):
    monkeypatch.setenv("LM_DATA_ROOT", "~/somewhere")
    assert cfg.resolve_data_root(None) == (Path.home() / "somewhere")


def test_load_config_docker_requires_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("LM_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        cfg.load_config("docker")
    assert exc.value.code == 2


def test_load_config_docker_with_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("LM_API_KEY", "secret-key")
    c = cfg.load_config("docker")
    assert c.mode == "docker"
    assert c.api_key == "secret-key"
    assert c.session_token is None
    assert c.data_root == tmp_path


def test_load_config_local_generates_session_token(monkeypatch, tmp_path):
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("LM_API_KEY", raising=False)
    c = cfg.load_config("local")
    assert c.mode == "local"
    assert c.api_key is None
    assert isinstance(c.session_token, str) and len(c.session_token) >= 24
    # two loads => different tokens
    c2 = cfg.load_config("local")
    assert c.session_token != c2.session_token


def test_load_config_models_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LM_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("LM_CONSOLE_MODELS", "stub")
    c = cfg.load_config("local")
    assert c.models == "stub"


from pathlib import Path  # noqa: E402  (kept last so the fixtures above read top-down)
