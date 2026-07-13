# syntax=docker/dockerfile:1
#
# lean-memory-console — multi-stage image.
#
#   ui-build : oven/bun — builds the React SPA into dist/.
#   slim     : python:3.13-slim — console + core lean-memory + built SPA,
#              stub embedder only (no real-model extra). For API/UI dev.
#   full     : slim + the lean-memory models extra (CPU torch, real
#              embedder+reranker). The documented first-run image
#              (compose target: full).
#
# Build the default (full) image:
#   docker build -f deploy/Dockerfile --target full -t lean-memory-console .
# Build the slim dev image:
#   docker build -f deploy/Dockerfile --target slim -t lean-memory-console:slim .

# ── UI build stage ───────────────────────────────────────────────────────
FROM oven/bun:1 AS ui-build
WORKDIR /ui
# Install deps first for layer caching.
COPY ui/package.json ui/bun.lock ./
RUN bun install --frozen-lockfile
COPY ui/ ./
# vite.config.ts writes outDir ../console/src/lean_memory_console/static; force
# a stage-local dist/ so `COPY --from=ui-build /ui/dist/` below is deterministic.
RUN bun run build --outDir dist --emptyOutDir

# ── slim runtime (stub embedder; no models) ──────────────────────────────
FROM python:3.13-slim AS slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LM_DATA_ROOT=/data \
    PORT=8377
WORKDIR /app
# Core engine + console sources from the repo checkout (build context = repo root).
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY console/ ./console/
COPY deploy/ ./deploy/
# Install the core engine (path) then the console (path). No model extra here.
RUN python -m pip install --upgrade pip \
 && python -m pip install . \
 && python -m pip install ./console
# Drop the built SPA into the *installed* package's static dir. The wheel build
# excludes static/ (it is gitignored, so hatchling's VCS file selection skips
# it), and the console serves the SPA from `<installed pkg>/static` — so copy
# the built assets straight into the installed tree rather than the source copy.
COPY --from=ui-build /ui/dist/ /tmp/spa/
RUN python - <<'PY'
import pathlib, shutil, lean_memory_console
dest = pathlib.Path(lean_memory_console.__file__).parent / "static"
dest.mkdir(parents=True, exist_ok=True)
shutil.copytree("/tmp/spa", dest, dirs_exist_ok=True)
print("SPA installed to", dest)
PY
RUN rm -rf /tmp/spa
VOLUME ["/data"]
EXPOSE 8377
# Docker mode (explicit --docker flag, no env-based magic): the app serves the
# console UI + data plane + /mcp mount under bearer auth, bound on 0.0.0.0.
# LM_API_KEY is required — boot validation in cli.py refuses to start without it.
CMD ["lean-memory-console", "serve", "--docker", "--root", "/data", "--no-open"]

# ── full runtime (real models) ───────────────────────────────────────────
FROM slim AS full
# CPU torch + sentence-transformers + reranker via the core [models] extra.
RUN python -m pip install '.[models]'
# HF cache lives on a mounted volume so downloads survive container restarts.
ENV HF_HOME=/root/.cache/huggingface
VOLUME ["/root/.cache/huggingface"]
