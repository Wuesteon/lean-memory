# lean-memory

[![test](https://github.com/Wuesteon/lean-memory/actions/workflows/test.yml/badge.svg)](https://github.com/Wuesteon/lean-memory/actions/workflows/test.yml)

Embedded, local-first agent memory. No server, no daemon, no mandatory cloud key.

> **Status (2026-07):** working toward the first public launch (MCP-first).
> Roadmap and rationale: `docs/superpowers/specs/2026-07-08-strategic-direction-design.md`.
> Public benchmark runs (LongMemEval/LoCoMo) are deferred until after launch;
> the harness is complete (`bench/phase2_*.py`) and the engine flaws it exposed
> are fixed on this branch — see `docs/phase2-learnings.md`.

```python
from lean_memory import Memory

mem = Memory(root="./data")

mem.add("user-42", "I work at Acme Corp.")
mem.add("user-42", "I now work at Globex.")          # supersedes Acme automatically

mem.search("user-42", "where does the user work?")   # → "I now work at Globex."
```

![lean-memory quickstart](docs/assets/quickstart.gif)

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
mem.add("alice", "I work at Stripe.")
mem.add("alice", "I now work at Vercel.")   # supersedes Stripe automatically

# Retrieve — the superseded Stripe fact drops out; only the current one is returned
results = mem.search("alice", "what does Alice do for work?", k=3)
for hit in results:
    print(hit.fact.fact_text, hit.final_score)
# → I now work at Vercel. 0.89

# Point-in-time query — what was true at a specific moment?
mem.search("alice", "employer", as_of=1_700_000_000_000, is_latest_only=False)  # epoch ms

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

## MCP Server — memory for Claude Code / Claude Desktop

Give any MCP agent persistent local memory: three tools (`memory_add`,
`memory_search`, `memory_clear`), one SQLite file per namespace, nothing
leaves your machine.

```bash
pip install 'lean-memory[mcp,models,extract]'
```

> First run downloads three open models (~2.0 GB total: Qwen3-Embedding-0.6B
> + Ettin-32M reranker for retrieval, plus GLiNER2-base (~0.8 GB) for real
> extraction — all ungated). Pre-warm once so your MCP client never waits on
> a download:
>
> ```bash
> python -c "from lean_memory.embed.sentence_transformer import SentenceTransformerEmbedder; \
> from lean_memory.retrieve.rerank import CrossEncoderReranker; \
> SentenceTransformerEmbedder().embed_one('warm'); CrossEncoderReranker().score('warm', ['up']); \
> from lean_memory.extract.gliner_extractor import Gliner2Generator; from lean_memory.types import Episode; \
> Gliner2Generator().generate(Episode(namespace='w', raw='I work at Acme.', t_ref=0, source='user'))"
> ```

**Claude Code:**

```bash
claude mcp add lean-memory -- lean-memory-mcp
```

**Claude Desktop** — add to `mcpServers` (or copy `examples/mcp_config.json`):

```json
{ "lean-memory": { "command": "lean-memory-mcp", "env": { "LM_DATA_ROOT": "~/.lean_memory" } } }
```

Data root: `LM_DATA_ROOT` (default `~/.lean_memory`). Works offline-only too —
the server opportunistically upgrades each backend that its extra is installed
for (`[models]` → real embedder + reranker, `[extract]` → GLiNER2 extraction)
and otherwise falls back to deterministic stub backends (fine for CI,
semantically meaningless for real use — install `[mcp,models,extract]`).

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
3. **Router** — recall-biased escalation: low-confidence, coreference, and inferential (`derives`) facts escalate to the LLM pass
4. **LLM typing** — constrained relation typing via a local Ollama model (stub by default)

Contradiction detection runs cheap-first (slot match → cosine → token subsumption → LLM). Conflicting facts are superseded, not deleted — the old fact stays with `is_latest=False` and a `superseded_by` pointer.

Retrieval fuses two-stage Matryoshka dense search (256-dim coarse KNN → 768-dim re-score) with BM25 sparse, applies RRF fusion, reranks with a cross-encoder, and scores with salience-decay (`0.6·relevance + 0.2·recency + 0.2·importance`).

## Develop

```bash
git clone https://github.com/Wuesteon/lean-memory
cd lean-memory
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q    # full offline suite, no downloads
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
tests/                        offline test suite
bench/                        Retrieval quality + BET-2 ablation harnesses
```

## License

Apache-2.0
