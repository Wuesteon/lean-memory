"""Static structural checks on the Docker deploy artifacts.

No Docker daemon is invoked — these assert file contents so the invariants
(multi-stage targets, full-is-default, required-env fail-fast, packaged
compose path) hold in CI. The `docker build` smoke check is a [manual] step
in the task, not a test.
"""
from __future__ import annotations

from pathlib import Path

import yaml  # PyYAML — in the console[dev] extra since Task 1

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile"
COMPOSE = REPO_ROOT / "deploy" / "docker-compose.yml"


def test_dockerfile_has_three_named_stages() -> None:
    text = DOCKERFILE.read_text()
    # bun build stage, then slim, then full (order matters: full FROM slim).
    assert "oven/bun" in text, "bun stage must use the oven/bun image"
    assert "AS ui-build" in text
    assert "python:3.13-slim AS slim" in text
    assert "FROM slim AS full" in text
    # full adds the models extra; slim must NOT.
    slim_block = text.split("FROM slim AS full")[0]
    assert "[models]" not in slim_block, "slim target must never install [models]"
    assert "[models]" in text, "full target must install lean-memory[models]"


def test_dockerfile_copies_built_static_from_bun_stage() -> None:
    text = DOCKERFILE.read_text()
    assert "COPY --from=ui-build" in text, "built SPA must come from the bun stage"
    assert "static" in text


def test_dockerfile_cmd_selects_docker_mode() -> None:
    # The container entrypoint MUST pass --docker so serve loads Docker mode
    # (bearer auth, 0.0.0.0 bind, /mcp mount). Without it the image silently
    # runs in local mode: LM_API_KEY ignored, /mcp never mounted, LAN clients
    # 403'd by the local Host guard.
    text = DOCKERFILE.read_text()
    cmd_lines = [
        ln for ln in text.splitlines()
        if ln.startswith("CMD") and "lean-memory-console" in ln
    ]
    assert cmd_lines, "expected a CMD launching lean-memory-console"
    assert all("serve" in ln and "--docker" in ln for ln in cmd_lines), (
        "the container CMD must run `serve --docker`"
    )


def test_dockerfile_full_stage_pins_cpu_torch() -> None:
    # The [models] extra depends on torch>=2.2. Without pinning, pip resolves the
    # default CUDA wheels (torch + nvidia-cudnn + cuda-toolkit) → ~5.5 GB image on
    # a CPU-only target. The full stage must install torch from the PyTorch CPU
    # wheel index BEFORE the extra, so the resolve keeps the CPU build. Regression
    # guard: the CPU index URL must appear, and after the `FROM slim AS full`.
    text = DOCKERFILE.read_text()
    full_block = text.split("FROM slim AS full")[-1]
    assert "download.pytorch.org/whl/cpu" in full_block, (
        "full stage must pin torch to the PyTorch CPU wheel index (no CUDA wheels)"
    )
    # The CPU-index install must come before the [models] extra INSTALL so the
    # extra sees torch already satisfied. Ordering is checked against install
    # lines only (comments mention [models] too), so ignore comment lines.
    code = "\n".join(
        ln for ln in full_block.splitlines() if not ln.lstrip().startswith("#")
    )
    cpu_at = code.find("download.pytorch.org/whl/cpu")
    models_at = code.find("[models]")
    assert cpu_at != -1 and models_at != -1 and cpu_at < models_at, (
        "CPU torch must be installed before the [models] extra"
    )


def test_compose_targets_full_and_requires_api_key() -> None:
    data = yaml.safe_load(COMPOSE.read_text())
    svc = data["services"]["console"]
    assert svc["build"]["target"] == "full", "compose must default to the full image"
    assert svc["build"]["context"] == ".."
    assert svc["build"]["dockerfile"] == "deploy/Dockerfile"
    assert "8377:8377" in svc["ports"]
    # LM_API_KEY required — the ${VAR:?message} form fails compose if unset.
    raw = COMPOSE.read_text()
    assert "${LM_API_KEY:?" in raw, "LM_API_KEY must be a required compose variable"
    assert "LM_DATA_ROOT=/data" in raw
    # named data volume + hf cache mount.
    assert "lm_data:/data" in raw
    assert "huggingface" in raw


def test_compose_path_resolves_and_matches_source() -> None:
    # cli._compose_path() prefers the wheel-packaged resource and falls back
    # to the repo deploy/ copy under editable installs (Task 9). Either way
    # the resolved file must exist and be byte-identical to the source of
    # truth. (Wheel packaging itself is validated by Step 5's `unzip -l`
    # listing — importlib.resources cannot see force-include under an
    # editable install, so asserting the raw resource here would fail in dev.)
    from lean_memory_console.cli import _compose_path

    resolved = _compose_path()
    assert resolved.is_file()
    assert resolved.read_text() == COMPOSE.read_text()
