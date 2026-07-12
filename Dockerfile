# MCP server image for inspection/introspection (Glama and similar directories).
# Installs from PyPI with the [mcp] extra only: the server starts with the
# deterministic offline stub backends (no model downloads), which is all an
# introspection pass (initialize + tools/list) exercises. Real deployments
# should install 'lean-memory[mcp,models,extract]' per the README.
FROM python:3.12-slim

RUN pip install --no-cache-dir 'lean-memory[mcp]'

ENV LM_DATA_ROOT=/data
RUN mkdir -p /data

# stdio transport: the MCP client (or inspector) talks JSON-RPC over stdin/stdout.
CMD ["lean-memory-mcp"]
