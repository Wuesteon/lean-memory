# lean-memory

Embedded, local-first agent memory. No server, no daemon, no mandatory cloud key.

```python
from lean_memory import Memory

mem = Memory(root="./data")

mem.add("user-42", "I work at Acme Corp.")
mem.add("user-42", "I moved to Globex last week.")   # supersedes Acme automatically

mem.search("user-42", "where does the user work?")   # → "I moved to Globex last week."
```

Facts are extracted from natural language, stored in a per-namespace SQLite file, and retrieved with hybrid dense+sparse search. Old facts are never deleted — they're superseded and queryable at any past point in time.

## Install

```bash
pip install lean-memory
```

Runs fully offline out of the box. Optional extras unlock real model quality:

| Extra | What it adds |
|---|---|
| `lean-memory[models]` | Real embedder + reranker (Qwen3-0.6B + Ettin-32M) |
| `lean-memory[extract]` | GLiNER2 candidate generation for richer extraction |
| `lean-memory[llm]` | Ollama-backed LLM typing pass |
| `lean-memory[mcp]` | MCP server bridge for Claude Desktop / Claude Code |
| `lean-memory[examples]` | Terminal demo agent (requires `anthropic` SDK) |

## Quickstart

```python
from lean_memory import Memory

mem = Memory(root="./data")   # one SQLite file per namespace, stored under ./data/

# Store facts in natural language
mem.add("alice", "I'm a backend engineer at Stripe.")
mem.add("alice", "I switched to frontend at Vercel last month.")

# Retrieve — superseded Stripe fact is automatically de-ranked
results = mem.search("alice", "what does Alice do for work?", k=3)
for hit in results:
    print(hit.fact.fact_text, hit.final_score)

# Point-in-time query — what was true at a specific moment?
mem.search("alice", "employer", as_of=<timestamp_ms>, is_latest_only=False)

# Always close when done (flushes WAL)
mem.close()
```

## Demo Agent

A terminal chatbot showing the full memory loop — add, retrieve, supersede, restart:

```bash
pip install 'lean-memory[examples]'
export ANTHROPIC_API_KEY=sk-ant-...
python examples/chat.py                  # uses offline stubs by default
python examples/chat.py --namespace bob  # separate memory tenant, persists across restarts
```

No API key? The demo still runs — it echoes the retrieved memory context instead of calling Claude, so you can watch the engine work offline.

## MCP Server

Expose lean-memory as three MCP tools (`memory_add`, `memory_search`, `memory_clear`) to any MCP-compatible agent:

```bash
pip install 'lean-memory[mcp]'
lean-memory-mcp    # stdio transport
```

Drop the config from `examples/mcp_config.json` into your Claude Desktop or Claude Code `mcpServers` block. Data root is controlled by `LM_DATA_ROOT` (default `~/.lean_memory`).

## Real Model Quality

The default backends are offline stubs — deterministic and dependency-free, but semantically meaningless. Swap in real models for production-quality retrieval:

```bash
pip install 'lean-memory[models]'
```

With `Qwen3-Embedding-0.6B` + `Ettin-32M` reranker, retrieval jumps from 1/5 to 4/5 on the internal benchmark with zero code changes.

> For benchmark results, architecture decisions, and implementation status see [ARCHITECTURE.md](ARCHITECTURE.md).

## How It Works

Each `mem.add()` call runs a 4-pass hybrid extraction pipeline:

1. **Rules** — regex + dateparser for common predicates (`works_at`, `lives_in`, …)
2. **GLiNER2** — open-vocabulary NER candidate generation (offline stub by default)
3. **Router** — recall-biased escalation: low-confidence, coreference, and cross-turn facts escalate to the LLM pass
4. **LLM typing** — constrained relation typing via a local Ollama model (stub by default)

Contradiction detection runs cheap-first (slot match → cosine → token subsumption → LLM). Conflicting facts are superseded, not deleted — the old fact stays with `is_latest=False` and a `superseded_by` pointer.

Retrieval fuses two-stage Matryoshka dense search (256-dim coarse KNN → 768-dim re-score) with BM25 sparse, applies RRF fusion, reranks with a cross-encoder, and scores with salience-decay (`0.6·relevance + 0.2·recency + 0.2·importance`).

## Develop

```bash
git clone https://github.com/Wuesteon/lean-memory
cd lean-memory
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q    # 56 tests, all offline, ~4s
```

## Project Layout

```
src/lean_memory/
  memory.py                   Memory facade — the public API
  types.py                    Episode / Fact / RetrievedFact types
  store/                      Store interface + SqliteStore (vec0 + FTS5)
  embed/                      Embedder interface, FakeEmbedder, SentenceTransformer
  extract/                    4-pass extraction pipeline
  retrieve/                   Reranker interface, retrieval pipeline
examples/
  chat.py                     Terminal demo agent
  mcp_config.json             Drop-in MCP client config
tests/                        56 offline tests
bench/                        Retrieval quality + BET-2 ablation harnesses
```

## License

Apache-2.0
