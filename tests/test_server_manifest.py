"""MCP Registry manifest (server.json) must describe a WORKING install.

Regression for the v0.1.2 launch blocker: `uvx --from lean-memory lean-memory-mcp`
installs only the core dependencies, but the console script imports `mcp` at module
top level (mcp_server.py) — an optional extra — so every registry install crashed on
startup with ModuleNotFoundError. The --from spec must carry the extras the server
actually needs (and ships its calibrated quality with): mcp, models, extract.
"""

import json
import re
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10 — tomli ships via the [dev] extra
    import tomli as tomllib

_ROOT = Path(__file__).resolve().parents[1]


def _manifest() -> dict:
    return json.loads((_ROOT / "server.json").read_text())


def _from_spec(manifest: dict) -> str:
    """The value of the uvx `--from` runtime argument (the pip requirement spec)."""
    args = manifest["packages"][0]["runtimeArguments"]
    from_args = [a for a in args if a.get("name") == "--from"]
    assert len(from_args) == 1, "expected exactly one --from runtime argument"
    return from_args[0]["value"]


def test_registry_install_spec_carries_required_extras():
    spec = _from_spec(_manifest())
    m = re.fullmatch(r"lean-memory\[([a-z0-9_,-]+)\]", spec)
    assert m, f"--from spec {spec!r} must be lean-memory[<extras>] — bare installs crash"
    extras = set(m.group(1).split(","))
    # mcp: the server cannot even import without it. models/extract: the canonical
    # real-quality install the README/registry description promises (gate item 1).
    assert {"mcp", "models", "extract"} <= extras


def test_manifest_versions_match_pyproject():
    manifest = _manifest()
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    version = pyproject["project"]["version"]
    assert manifest["version"] == version
    assert manifest["packages"][0]["version"] == version


def test_package_dunder_version_matches_pyproject():
    import lean_memory

    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert lean_memory.__version__ == pyproject["project"]["version"]
