# MCP Nexus RAG

<!-- Executive summary: tech stack, mission, architecture -->

**Version:** v3.4

> See [AGENTS.md](AGENTS.md) for commands | [MEMORY.md](MEMORY.md) for state | [TODO.md](TODO.md) for tasks

Strict multi-tenant memory server for the Antigravity agent ecosystem.
Provides **GraphRAG** (Neo4j) and **Vector RAG** (Qdrant) retrieval, both isolated by `project_id` and `tenant_scope`.
All inference runs locally via Ollama — zero data leakage.

**Status**: ✅ Production-ready · 🔒 Security-first · ⚡ High-performance · 📊 394 tests passing · ⚡ Redis semantic cache integrated

---

## Architecture

```text
Agent (MCP Client)
       │
       ▼
 server.py (FastMCP)
  ├── GraphRAG  → Neo4j (llama3.1:8b builds property graph)
  │                └── bge-reranker-v2-m3 (cross-encoder reranker)
  └── Vector RAG → Qdrant (nomic-embed-text, collection: nexus_rag)
                   └── bge-reranker-v2-m3 (cross-encoder reranker)
```

### Dual-Engine Design

| Engine         | Backend | Best For                                                     |
| -------------- | ------- | ------------------------------------------------------------ |
| **GraphRAG**   | Neo4j   | Relationship traversal, architecture queries, entity linkage |
| **Vector RAG** | Qdrant  | Semantic similarity, code snippets, factual Q&A              |

### Reranker Pipeline (v2.0)

Both retrieval tools use a two-stage pipeline:

1. **Candidate retrieval** — fetch `RERANKER_CANDIDATE_K` (default 20) nodes from the index
2. **Cross-encoder reranking** — `BAAI/bge-reranker-v2-m3` scores each candidate against the query
3. **Top-N selection** — return the `RERANKER_TOP_N` (default 5) highest-scoring nodes

The reranker is a lazy-loaded singleton (FP16, loaded once per process). Pass `rerank=False` to either tool to skip reranking and return raw retrieval results.

### Multi-Tenant Isolation

Every document and query carries two required metadata keys:

| Key            | Role                       | Examples                                   |
| -------------- | -------------------------- | ------------------------------------------ |
| `project_id`   | Top-level tenant namespace | `TRADING_BOT`, `WEB_PORTAL`                |
| `tenant_scope` | Domain within a project    | `CORE_CODE`, `SYSTEM_LOGS`, `WEB_RESEARCH` |

The `(project_id, tenant_scope)` tuple is enforced as an exact-match filter in both Neo4j Cypher and Qdrant scroll/delete — zero crosstalk between projects or scopes.

---

## MCP Tools

### Ingestion

| Tool                            | Description                                                      |
| ------------------------------- | ---------------------------------------------------------------- |
| `ingest_graph_document`         | Ingest single text into GraphRAG (Neo4j)                         |
| `ingest_vector_document`        | Ingest single text into Vector RAG (Qdrant)                      |
| `ingest_graph_documents_batch`  | Batch ingest into GraphRAG (10-50x faster)                       |
| `ingest_vector_documents_batch` | Batch ingest into Vector RAG (10-50x faster)                     |

### Retrieval

| Tool                 | Parameters                                        | Description                                                                                          |
| -------------------- | ------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `get_graph_context`  | `query`, `project_id`, `scope=""`, `rerank=True`  | Query GraphRAG; cross-encoder reranks candidates by default. `scope=""` queries all scopes.          |
| `get_vector_context` | `query`, `project_id`, `scope=""`, `rerank=True`  | Query Vector RAG; cross-encoder reranks candidates by default. `scope=""` queries all scopes.        |
| `answer_query`       | `query`, `project_id`, `scope=""`, `rerank=True`  | Combined RAG/GraphRAG answer via local Ollama LLM. `scope=""` retrieves from **all project scopes**. |

### Health & Diagnostics

| Tool               | Description                                             |
| ------------------ | ------------------------------------------------------- |
| `health_check`     | Check connectivity to Neo4j, Qdrant, and Ollama         |
| `get_tenant_stats` | Get document/node counts for project/scope              |
| `print_all_stats`  | Display ASCII table of all projects, scopes, and counts |

`get_tenant_stats` returns:

| Key                  | Description                                                     |
| -------------------- | --------------------------------------------------------------- |
| `graph_nodes_total`  | All Neo4j nodes for the project/scope                           |
| `graph_chunk_nodes`  | Source doc nodes (have `content_hash`) — what you ingested      |
| `graph_entity_nodes` | LLM-extracted concept/entity nodes (no `content_hash`)          |
| `vector_docs`        | Qdrant point count                                              |
| `total_docs`         | `graph_nodes_total` + `vector_docs`                             |

`print_all_stats` table columns: `PROJECT_ID`, `SCOPE`, `GRAPH`, `CHUNKS`, `ENTITIES`, `VECTOR`, `TOTAL`

### Tenant Management

| Tool                    | Description                                                     |
| ----------------------- | --------------------------------------------------------------- |
| `get_all_project_ids`   | List all distinct project IDs across both DBs                   |
| `get_all_tenant_scopes` | List all scopes (optionally filtered by `project_id`)           |
| `delete_tenant_data`    | Delete all data for a `project_id`, or a `(project_id, scope)`  |

### Administration & Sync

| Tool                      | Description                                                                            |
| ------------------------- | -------------------------------------------------------------------------------------- |
| `delete_all_data`         | **Full wipe** — delete all data from Neo4j and Qdrant across all tenants               |
| `ingest_project_directory`| Recursively ingest an entire directory tree into both GraphRAG and VectorRAG           |
| `sync_project_files`      | Re-ingest core docs that have changed (idempotent, SHA-256 dedup)                      |
| `sync_deleted_files`      | Remove stale database entries for files deleted from disk                              |
| `list_core_doc_files`     | List all core documentation files tracked for sync (dry-run for `sync_project_files`) |
| `cache_stats`             | Get Redis cache stats: key count, memory usage, TTL settings                           |

**Notes:**

- `delete_tenant_data` returns a **partial-failure message** if one backend fails, never silently succeeding.
- Batch ingestion tools accept a list of `{text, project_id, scope, source_identifier?}` dicts and return `{ingested, skipped, errors}` counts.
- Reranker errors are caught and logged as warnings; the tool falls back to un-reranked results rather than failing.

---

## Infrastructure

Services are defined in `docker-compose.yml`:

| Service      | Address                  | Purpose                               |
| ------------ | ------------------------ | ------------------------------------- |
| **Neo4j**    | `bolt://localhost:7687`  | GraphRAG graph store                  |
| **Qdrant**   | `http://localhost:6333`  | Vector store (`nexus_rag` collection) |
| **Ollama**   | `http://localhost:11434` | Local LLM + embeddings                |
| **Redis**    | `redis://localhost:6379` | Semantic query result cache (24h TTL) |
| **Postgres** | `localhost:5432`         | Reserved (pgvector, future)           |

Models auto-pulled by `ollama-init` on first start:

- `nomic-embed-text` — embeddings
- `llama3.1:8b` — graph extraction

> **Note**: `BAAI/bge-reranker-v2-m3` is loaded directly from HuggingFace Hub by `llama-index-postprocessor-flag-embedding-reranker` — it does **not** require an Ollama pull. Requires `FlagEmbedding>=1.3.5,<2.0.0` and `transformers>=4.40.0,<5.0.0`.

```bash
docker-compose up -d
```

---

## Quick Start

```bash
# 1. Start services
docker-compose up -d

# 2. Install dependencies
poetry install

# 3. Run the full suite + coverage (no live services required)
PYTHONPATH=. poetry run pytest tests/ --cov=nexus --cov=server --cov-report=term-missing
```

---

## Environment Variables

| Variable               | Default                    | Description                                                         |
| ---------------------- | -------------------------- | ------------------------------------------------------------------- |
| `RERANKER_MODEL`       | `BAAI/bge-reranker-v2-m3`  | HuggingFace model ID for the cross-encoder                          |
| `RERANKER_TOP_N`       | `5`                        | Number of results returned after reranking                          |
| `RERANKER_CANDIDATE_K` | `20`                       | Candidate pool size fetched before reranking                        |
| `RERANKER_ENABLED`     | `true`                     | Set to `false` to disable reranking globally                        |
| `MAX_DOCUMENT_SIZE`    | `4096` (4KB)               | Documents larger than this are auto-chunked on ingest               |
| `MAX_CONTEXT_CHARS`    | `1500`                     | Hard cap on chars returned by retrieval tools (0 = disabled)        |
| `INGEST_CHUNK_SIZE`    | `1024`                     | Chunk size for large document splitting                             |
| `INGEST_CHUNK_OVERLAP` | `128`                      | Overlap between chunks                                              |
| `NEO4J_URL`            | `bolt://localhost:7687`    | Neo4j connection URI                                                |
| `NEO4J_USER`           | `neo4j`                    | Neo4j username                                                      |
| `NEO4J_PASSWORD`       | `password123`              | Neo4j password (use env var in production)                          |
| `QDRANT_URL`           | `http://localhost:6333`    | Qdrant connection URL                                               |
| `OLLAMA_URL`           | `http://localhost:11434`   | Ollama base URL                                                     |
| `REDIS_URL`            | `redis://localhost:6379`   | Redis connection URL for semantic cache                             |
| `CACHE_TTL`            | `86400` (24h)              | Cache entry TTL in seconds                                          |
| `CACHE_ENABLED`        | `true`                     | Set to `false` to bypass Redis cache globally                       |

---

## Querying the Server

The server uses **stdio transport** (standard MCP protocol), not HTTP — so `curl` cannot reach it directly. Use one of the methods below.

### Option 1: MCP Inspector (browser UI)

```bash
# Start the inspector — opens http://localhost:5173 in your browser
npx @modelcontextprotocol/inspector poetry run python server.py
```

In the browser UI, select a tool (e.g. `get_vector_context`), fill in the arguments, and click **Run**.

### Option 2: Python MCP client (scriptable, no extra install)

The `mcp` Python package is already installed as a transitive dependency. Use it to call any tool from a script:

```bash
# Query Vector RAG
poetry run python - <<'EOF'
import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(command="poetry", args=["run", "python", "server.py"])

async def call(tool, **kwargs):
    async with stdio_client(SERVER) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, kwargs)
            print(json.dumps([c.text for c in res.content], indent=2))

asyncio.run(call(
    "get_vector_context",
    query="how does deduplication work?",
    project_id="MY_PROJECT",
    scope="CORE_CODE",    # omit or pass scope="" to query all scopes
))
EOF
```

```bash
# Query GraphRAG with reranking disabled
poetry run python - <<'EOF'
import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(command="poetry", args=["run", "python", "server.py"])

async def call(tool, **kwargs):
    async with stdio_client(SERVER) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, kwargs)
            print(json.dumps([c.text for c in res.content], indent=2))

asyncio.run(call(
    "get_graph_context",
    query="what entities are related to ingestion?",
    project_id="MY_PROJECT",
    scope="CORE_CODE",
    rerank=False,
))
EOF
```

```bash
# Ingest a document
poetry run python - <<'EOF'
import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(command="poetry", args=["run", "python", "server.py"])

async def call(tool, **kwargs):
    async with stdio_client(SERVER) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, kwargs)
            print(json.dumps([c.text for c in res.content], indent=2))

asyncio.run(call(
    "ingest_vector_document",
    text="Deduplication uses SHA-256 content hashing.",
    project_id="MY_PROJECT",
    scope="CORE_CODE",
))
EOF
```

```bash
# Health check
poetry run python - <<'EOF'
import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(command="poetry", args=["run", "python", "server.py"])

async def call(tool, **kwargs):
    async with stdio_client(SERVER) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, kwargs)
            print(json.dumps([c.text for c in res.content], indent=2))

asyncio.run(call("health_check"))
EOF
```

### Option 3: Python one-liner (no extra tooling)

```bash
poetry run python - <<'EOF'
import asyncio
from nexus.tools import get_vector_context

async def main():
    result = await get_vector_context(
        query="how does deduplication work?",
        project_id="MY_PROJECT",
        scope="CORE_CODE",  # omit or pass scope="" to query all scopes
    )
    print(result)

asyncio.run(main())
EOF
```

---

## MCP Client Configuration

For the Antigravity workspace the active config lives at `~/antigravity/.mcp.json`.
Both `nexus` and `code-graph-rag` are auto-started by Claude Code on session init.

**Nexus RAG** entry (uses venv Python for dependency isolation):

```json
{
  "mcpServers": {
    "nexus": {
      "command": "/home/turiya/antigravity/projects/mcp-nexus-rag/.venv/bin/python",
      "args": ["/home/turiya/antigravity/projects/mcp-nexus-rag/server.py"],
      "env": {
        "OLLAMA_URL": "http://localhost:11434",
        "NEO4J_URL": "bolt://localhost:7687",
        "QDRANT_URL": "http://localhost:6333",
        "REDIS_URL": "redis://localhost:6379"
      }
    }
  }
}
```

**Code-Graph-RAG** entry (uses `uv run` from the code-graph-rag repo):

```json
{
  "mcpServers": {
    "code-graph-rag": {
      "command": "/home/turiya/.local/bin/uv",
      "args": ["run", "--directory", "/home/turiya/code-graph-rag", "code-graph-rag", "mcp-server"],
      "env": {
        "TARGET_REPO_PATH": "/home/turiya/antigravity",
        "CYPHER_PROVIDER": "ollama",
        "CYPHER_MODEL": "llama3.1:8b",
        "OLLAMA_BASE_URL": "http://localhost:11434",
        "MEMGRAPH_PORT": "7688"
      }
    }
  }
}
```

**Other clients** (Cursor, Claude Desktop — replace the path):

```json
{
  "mcpServers": {
    "nexus": {
      "command": "poetry",
      "args": ["run", "python", "/absolute/path/to/mcp-nexus-rag/server.py"]
    }
  }
}
```

---

## Security Notes

- **Metadata key allowlist**: `nexus.config.ALLOWED_META_KEYS` — only `project_id`, `tenant_scope`, `source`, `content_hash` are accepted as Cypher/Qdrant property names, preventing key injection.
- **No external API calls**: All LLM and embedding traffic stays on `localhost:11434`. The reranker model is downloaded from HuggingFace Hub on first use and cached locally.
- **Secrets**: `DEFAULT_NEO4J_PASSWORD` is defined in `nexus/config.py`. Migrate to environment variables for production deployments.

---

## Development

```bash
# Unit tests only (no live services needed)
PYTHONPATH=. poetry run pytest tests/test_unit.py tests/test_coverage.py tests/test_reranker.py -v

# Integration tests (requires live docker-compose)
PYTHONPATH=. poetry run pytest tests/test_integration.py -v

# Full suite + 100% coverage
PYTHONPATH=. poetry run pytest tests/ --cov=nexus --cov=server --cov-report=term-missing

# Interactive MCP Inspector
npx @modelcontextprotocol/inspector poetry run python server.py
```

---

## Documentation

- **[AGENTS.md](AGENTS.md)** - Commands for testing, building, and running
- **[MEMORY.md](MEMORY.md)** - Known bugs, key findings, changelog
- **[TODO.md](TODO.md)** - Pending tasks and roadmap

---

## Quick Links

| Resource | Description |
|----------|-------------|
| [AGENTS.md](AGENTS.md) | Testing, building, reset commands |
| [MEMORY.md](MEMORY.md) | Known issues, architecture findings, changelog |
| [TODO.md](TODO.md) | Pending hardening and feature tasks |

---

## Code-Graph-RAG Integration

The Antigravity workspace also integrates [Code-Graph-RAG](https://github.com/vitali87/code-graph-rag) — a graph-based RAG system for codebase analysis using Memgraph and Tree-sitter parsing.

### Architecture Overview

```text
┌─────────────────────────────────────────────────────────────────┐
│                    Antigravity Agent Ecosystem                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌───────────────────┐         ┌───────────────────┐          │
│   │   MCP Nexus RAG   │         │  Code-Graph-RAG   │          │
│   │   (Memory/RAG)    │         │  (Code Analysis)  │          │
│   └────────┬──────────┘         └────────┬──────────┘          │
│            │                             │                      │
│   ┌────────┴──────────┐         ┌────────┴──────────┐          │
│   │ Neo4j   │ Qdrant  │         │     Memgraph      │          │
│   │ :7687   │ :6333   │         │      :7688        │          │
│   └─────────┴─────────┘         └───────────────────┘          │
│            │                             │                      │
│            └─────────────┬───────────────┘                      │
│                          │                                      │
│                    ┌─────┴─────┐                                │
│                    │  Ollama   │                                │
│                    │  :11434   │                                │
│                    └───────────┘                                │
└─────────────────────────────────────────────────────────────────┘
```

| System | Purpose | Port |
|--------|---------|------|
| **Nexus RAG** | Semantic memory, knowledge graphs, document retrieval | Neo4j :7687, Qdrant :6333 |
| **Code-Graph-RAG** | AST-based code analysis, function/class relationships | Memgraph :7688 |

### Setup Code-Graph-RAG for Antigravity

**Prerequisites:**
- Code-Graph-RAG cloned to `~/code-graph-rag`
- `uv` package manager installed

**1. Start Memgraph (standalone container):**

```bash
docker run -d --name memgraph-cgr \
  -p 7688:7687 -p 7445:7444 \
  --restart unless-stopped \
  memgraph/memgraph-mage
```

**2. Index the Antigravity codebase:**

```bash
cd ~/code-graph-rag
MEMGRAPH_PORT=7688 uv run cgr start \
  --repo-path ~/antigravity \
  --update-graph --clean
```

**3. Verify indexing:**

```bash
MEMGRAPH_PORT=7688 uv run cgr export -o /tmp/graph.json
# Should show nodes and relationships count
```

### MCP Configuration

Add to Claude Code (`~/.claude.json`) or Gemini (`~/.gemini/antigravity/mcp_config.json`):

```json
{
  "mcpServers": {
    "code-graph-rag": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/home/turiya/code-graph-rag",
        "code-graph-rag",
        "mcp-server"
      ],
      "env": {
        "TARGET_REPO_PATH": "/home/turiya/antigravity",
        "CYPHER_PROVIDER": "ollama",
        "CYPHER_MODEL": "llama3.1:8b",
        "ORCHESTRATOR_PROVIDER": "ollama",
        "ORCHESTRATOR_MODEL": "llama3.1:8b",
        "OLLAMA_BASE_URL": "http://localhost:11434",
        "MEMGRAPH_PORT": "7688"
      }
    }
  }
}
```

### Code-Graph-RAG MCP Tools

| Tool | Description |
|------|-------------|
| `list_projects` | List all indexed projects |
| `index_repository` | Re-index the codebase |
| `query_code_graph` | Natural language queries: *"What functions call X?"* |
| `get_code_snippet` | Get source code by qualified name |
| `surgical_replace_code` | Precise code block replacement |
| `read_file` / `write_file` | File operations |

### Example Queries

```
> What classes are in the nexus module?
> Show me functions that handle ingestion
> Find all methods that call neo4j_driver
> What does the answer_query function do?
```

### Quick Startup Script

Use the automation script to start all services after a reboot:

```bash
# Start all Antigravity AI services (Neo4j, Qdrant, Redis, Ollama, Postgres, Memgraph)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh

# Start Code-Graph-RAG realtime watcher (keeps Memgraph in sync with code changes)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --watcher

# Check status
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --status

# Run health checks on all services (Neo4j, Qdrant, Ollama, Redis, Memgraph)
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --health

# Re-index antigravity codebase into Memgraph
~/antigravity/projects/mcp-nexus-rag/scripts/start-services.sh --reindex
```

> **MCP Servers**: Both `nexus` and `code-graph-rag` MCP servers are **automatically started by Claude Code** on session init using `~/antigravity/.mcp.json` — no manual action needed. See [AGENTS.md](AGENTS.md) → *Services — Full Startup* for the complete service map and after-reboot checklist.

---

## Recent Updates

### v3.4 (2026-03-03) — Code Review Round 19: 3 Bugs Fixed

- 🐛 **BUGFIX**: `ingest_project_directory` — `count` incremented unconditionally even when both `ingest_graph_document` and `ingest_vector_document` returned `"Error: ..."` strings; misleading "Successfully ingested N files" on total ingest failure; now captures results and only counts on non-error (`tools.py` v4.2, **regression: 4 new tests**)
- 🐛 **BUGFIX**: `_parse_context_results` in `http_server.py` — "no results" guard used `"No " in ctx` (substring anywhere); real retrieved content containing "No" and "context found" would silently return empty results; fixed with `ctx.startswith("No ")` (`http_server.py` v1.9, **regression: 3 new tests**)
- 🔧 **CLEANUP**: `get_all_tenant_scopes` — renamed stale `vector_scopes2` variable to `vector_scopes` in else-branch (`tools.py` v4.2)
- 📝 **DOCS**: `invalidate_project_cache` docstring corrected — empty scope invalidates ALL project cache entries (not just cross-scope queries); `check_file_changed` comment clarified to match actual `not (A and B)` semantics
- ✅ **Tests**: 394 tests passing (+7 regression tests), lint clean (ruff)

### v3.3 (2026-03-03) — Bug Fixes: Chunked Ingest All-Fail + get_tenant_stats

- 🐛 **BUGFIX**: `ingest_graph_document` / `ingest_vector_document` — chunked ingest returned `"Successfully ingested 0 chunks (errors=N)"` when ALL chunks failed; watcher incorrectly logged "synced" because `"Error" not in result` was True; now returns `"Error: All N chunks failed..."` (`tools.py` v4.1, **regression: 5 new tests**)
- 🐛 **BUGFIX**: `get_tenant_stats` raised `ValueError` on empty `project_id` — inconsistent with all other tools that return `"Error: ..."` strings; changed to return error string, updated type annotation to `str | dict[str, int]` (`tools.py` v4.1, **regression: 3 new tests**)
- ✅ **Tests**: 387 tests passing (+8 regression tests), lint clean (ruff)

### v3.2 (2026-03-03) — Deep Code Review Loops 10–12: 3 Bugs Fixed

- 🐛 **BUGFIX**: `sync_project_files` bare `except Exception: pass` on pre-delete silently swallowed connection errors, leaving old chunks alive alongside new ingest = duplicate content; now logs, skips ingest, and reports error (`tools.py` v4.0, **Bug L10-1**)
- 🐛 **BUGFIX**: `sync_project_files` — cache not invalidated after pre-delete when ingest failed; stale cache served deleted content; fixed with `cache_module.invalidate_cache()` before ingest (`tools.py` v4.0, **Bug L10-2**)
- 🐛 **BUGFIX**: `watcher._sync_changed` — `"Successfully" in result` check treated "Skipped: duplicate" as a partial failure (warning log); changed to `"Error" not in result` consistent with all other ingest success checks (`watcher.py` v1.3, **Bug L12-4**)
- ✅ **Tests**: 379 tests passing (+8 regression tests), lint clean (ruff)

### v3.1 (2026-03-03) — Deep Code Review Rounds 2–9: 11 Bugs Fixed

- 🐛 **BUGFIX**: `invalidate_cache(project_id, "")` only cleared `__all__` index — per-scope indices not cleared after full-project delete; scoped queries returned stale cached data (`cache.py` v1.5)
- 🐛 **BUGFIX**: `watcher._sync_changed` — cache not invalidated after `_delete_from_rag`; if both ingest calls failed, stale cache remained for deleted content (`watcher.py` v1.2)
- 🐛 **BUGFIX**: Batch ingest chunk loops had no per-chunk `try/except` — error on chunk N silently skipped N+1..end; single-doc path was correct (`tools.py` v3.9)
- 🐛 **BUGFIX**: `delete_stale_files` / `sync_deleted_files` only queried Neo4j — Qdrant-only orphans never cleaned; now unions both stores (`sync.py` v1.2, `qdrant.py` v2.4)
- 🐛 **BUGFIX**: `neo4j_driver()` created a new connection pool per call — replaced with `get_driver()` singleton (double-checked locking) (`neo4j.py` v2.2)
- 🐛 **BUGFIX**: `delete_all_data` never invalidated Redis cache — added `invalidate_all_cache()` call (`cache.py` v1.4, `tools.py` v3.6)
- 🐛 **BUGFIX**: Empty `project_id` silently passed to Neo4j/Qdrant returning empty results with no error (`tools.py` v3.6)
- 🐛 **BUGFIX**: `scroll_field` in qdrant.py added `None` payload values to `set[str]` — `sorted()` raised `TypeError` in `get_all_tenant_scopes` / `print_all_stats` (`qdrant.py` v2.3)
- 🐛 **BUGFIX**: `sync_deleted_files`, `sync_project_files`, `watcher._sync_deleted` — backend deletes without Redis cache invalidation; stale cached results persisted (`tools.py` v3.7, `watcher.py` v1.1)
- 🐛 **BUGFIX**: `http_server.py` fallback scope hardcoded `"CORE_CODE"` instead of `""` (empty = all scopes) (`http_server.py` v1.8)
- 🐛 **BUGFIX**: Shared `_index_cache_lock` for both graph and vector index init — split into independent locks (`indexes.py` v2.2)
- ✨ **NEW**: `invalidate_project_cache` MCP tool — targeted cache invalidation without data deletion
- ✨ **NEW**: `reset_graph_index()` / `reset_vector_index()` in `indexes.py` — symmetric with `reset_reranker()` for test isolation
- ✅ **Tests**: 371 tests passing (+81 regression tests across rounds 2–9), lint clean (ruff)
- 🔍 **Verified**: 17 E2E scenarios (new/update/delete/move files, rapid saves, partial ingest recovery, concurrent access, cache invalidation chains)

### v3.0 (2026-03-03) — Code Review: 4 Bugs Fixed

- 🐛 **BUGFIX**: `n.score=None` crash in `get_graph_context` / `get_vector_context` — when the reranker fails and falls back to un-reranked nodes, graph nodes can have `score=None`. The f-string `{n.score:.4f}` raised `TypeError`. Fixed with `{(n.score if n.score is not None else 0.0):.4f}`.
- 🐛 **BUGFIX**: Batch ingest functions (`ingest_graph_documents_batch`, `ingest_vector_documents_batch`) never called `cache_module.invalidate_cache()`. Stale cache entries persisted after bulk ingestion. Fixed: both functions now collect dirty `(project_id, scope)` pairs and invalidate at the end.
- 🐛 **BUGFIX**: `answer_query` cache hits applied `_apply_cap(cached_answer, max_context_chars)` — incorrectly treating the output size parameter as the input context limit. Fixed: cache hits return the cached answer as-is; `max_context_chars` only bounds the Ollama prompt input.
- 🐛 **BUGFIX**: `get_graph/vector_context` cached the truncated result (capped to `max_chars`), making cache output depend on the first caller's `max_chars`. A subsequent caller with a larger `max_chars` got less content than expected. Fixed: cache stores full result; `_apply_cap` is applied at return time for both fresh and cache paths.
- 🔧 **CLEANUP**: Fixed type annotations in `cache.py` — `set_cached`/`get_cached` used `dict[str, Any]` but callers pass/return `str`. Changed to `Any`.
- 📝 **DOCS**: Fixed docstrings in both ingest functions that incorrectly stated "default 512KB" — actual `MAX_DOCUMENT_SIZE` default is 4KB.
- ✅ **Tests**: 290 tests passing (+11 regression tests: `TestScoreNoneHandling`, `TestBatchIngestCacheInvalidation`, `TestAnswerQueryCacheHit`, `TestFreshResultCaching`), lint clean (ruff)

### v2.9 (2026-03-03)

- 🔒 **SECURITY**: Exception message sanitization — all public tools now return generic error messages to MCP clients; full exception details logged server-side only. Prevents leaking internal paths, service URLs, or credentials via error strings.
- 🐛 **BUGFIX**: `invalidate_cache()` in `nexus/cache.py` was broken — hash-prefix pattern scan (`nexus:{hash[:8]}*`) never matched any keys. Fixed with a secondary Redis Set index (`nexus:idx:{project_id}:{scope}`): `set_cached` now also `SADD`s each key to the index; `invalidate_cache` uses `SMEMBERS` + bulk delete.
- 🐛 **BUGFIX**: `ingest_graph_document` and `ingest_vector_document` never called `cache_module.invalidate_cache()`. Fixed: both now invalidate the tenant's cache after a successful ingest (single-doc and chunked paths).
- ♻️ **REFACTOR**: `answer_query` complexity reduced from 21 → ~7 (ruff C901 fix). Extracted `_fetch_graph_passages()`, `_fetch_vector_passages()`, and `_dedup_cross_source()` as module-level helpers. Logic is identical, function is now maintainable.
- ✨ **NEW**: `validate_config()` in `nexus/config.py` — startup config check warns on default Neo4j password and localhost service URLs in `NEXUS_ENV=production` mode. Called at server startup in `server.py:main()`.
- ✅ **Tests**: 279 tests passing (+30 new: cache secondary index, exception sanitization, cache-on-ingest, config validation, answer_query helpers), lint clean (ruff)
<!-- Note: test count updated to 290 in v3.0 (+11 regression tests) -->

### v2.8 (2026-03-03)

- 🐛 **BUGFIX**: Cache key collision between graph and vector context tools
  - `cache_key()` had no tool-type discriminator — a `get_graph_context` call cached "Graph Context…" and a subsequent `get_vector_context` with identical `(query, project_id, scope)` returned the cached graph result with the wrong label
  - Fix: Added `tool_type` parameter to `cache_key`, `get_cached`, `set_cached` in `nexus/cache.py`. Graph calls use `tool_type="graph"`, vector `"vector"`, answer `"answer"`. Key format: `nexus:{SHA256(tool_type|project_id|scope|query)[:16]}`
- ✨ **FEATURE**: `scope` parameter is now optional (`scope=""`) on `get_graph_context` and `get_vector_context`
  - When `scope` is empty, the `tenant_scope` metadata filter is omitted — all scopes for the project are searched
  - Result messages display "all scopes" when scope is omitted
- ✅ **Tests**: 249 tests passing (+4 regression tests for cache collision and optional scope), lint clean

### v2.7 (2026-03-02)

- 🐛 **BUGFIX**: Reranker import path corrected (`flag_reranker` → `flag_embedding_reranker`)
  - Warning `No module named 'llama_index.postprocessor.flag_reranker'` was causing silent fallback to un-reranked results on every query
  - Fix: Updated both `TYPE_CHECKING` guard and runtime import in `nexus/reranker.py`; updated 6 `sys.modules` patches in `tests/test_reranker.py`
- ⚡ **NEW**: Redis semantic cache integrated into all retrieval tools
  - `get_vector_context`, `get_graph_context`, and `answer_query` now check Redis on entry and write on exit
  - Cache key (v2.7): `nexus:{SHA256(project_id|scope|query)[:16]}` — superseded in v2.8 to include `tool_type`
  - Previously imported but never called — all RAG queries were bypassing cache entirely
- 📦 **NEW**: FlagEmbedding dependency pinned in `pyproject.toml`
  - Added `FlagEmbedding>=1.3.5,<2.0.0` and `transformers>=4.40.0,<5.0.0`
  - Prevents `ImportError: cannot import name 'is_torch_fx_available'` from transformers 5.x
- 🧪 **Tests**: Added `autouse=True` `disable_cache` fixture in `conftest.py` to prevent Redis from polluting unit tests
- 🔧 **Scripts**: `start-services.sh` v1.1 — added Redis health check, `--watcher` option
- ✅ **Tests**: 197 tests passing, lint clean

### v2.6 (2026-02-28)

- 🐛 **BUGFIX**: Critical silent data loss in `ingest_graph_documents_batch` and `ingest_vector_documents_batch`
  - `file_path` was referenced but never extracted from `doc_dict`, causing a `NameError` swallowed by the `except` handler
  - All batch ingestion calls were silently returning `{"ingested": 0, "errors": N}` — zero documents ingested
  - Fix: added `file_path = doc_dict.get("file_path", "")` to both batch functions
- ✅ **Tests**: 197 tests passing (11 previously failing now fixed), 83% coverage
- 📝 **Docs**: Updated CODE_REVIEW.md with v2.6 audit, mutable default arg finding

### v2.5 (2026-02-28)

- 🗂️ **NEW**: `ingest_project_directory` — recursively ingest an entire codebase into both GraphRAG and VectorRAG
  - Respects `.gitignore` rules via `pathspec`
  - Configurable file extension filter (default: `.py`, `.ts`, `.js`, `.md`, `.txt`, `.json`)
  - Stores `file_path` metadata on every ingested document
- 🔄 **NEW**: `sync_deleted_files` — remove stale database entries for files deleted from disk
- 🗑️ **NEW**: `delete_all_data` — full database wipe across all projects and scopes
- 🔧 `file_path` metadata field added to all single-document ingest tools

### v2.4 (2026-02-28)

- 🔬 **NEW**: `get_tenant_stats` returns a detailed graph breakdown:
  - `graph_nodes_total` — all Neo4j nodes for the project/scope
  - `graph_chunk_nodes` — source doc nodes (content we explicitly ingested)
  - `graph_entity_nodes` — LLM-extracted concept/entity nodes (produced by `llama3.1:8b`)
- 📊 **NEW**: `print_all_stats` table now includes **CHUNKS** and **ENTITIES** columns
- 🔧 Two new `nexus/backends/neo4j.py` helpers: `get_chunk_node_count`, `get_entity_node_count`
- ✅ **Tests**: 194 tests passing (6 new), 100% coverage maintained, mypy clean

### v2.3 (2026-02-28)

- 📊 **NEW**: `print_all_stats` MCP tool — displays comprehensive ASCII table of all projects, scopes, and document counts
- 🎯 Shows project_id, scope, graph docs, vector docs, and totals in a formatted table
- 📈 **Tests**: 188 tests passing (7 new stats tests), 100% coverage maintained

### v2.2 (2026-02-28)

- 🎯 **NEW**: Automatic chunking for large documents exceeding `MAX_DOCUMENT_SIZE` (default 512KB)
- 📦 Documents are split using LlamaIndex's `SentenceSplitter` for intelligent sentence-boundary-aware chunking
- 🎛️ New env vars: `MAX_DOCUMENT_SIZE`, `INGEST_CHUNK_SIZE`, `INGEST_CHUNK_OVERLAP`
- 🔧 New `auto_chunk` parameter on all ingest tools (default: `True`)
- 📊 **Tests**: 181 tests passing (20 new chunking tests), 100% coverage maintained

### v2.1 (2026-02-28)

- 🔍 **Audit**: Comprehensive code review — zero critical bugs, 2 medium-priority hardening opportunities
- 📊 **Tests**: 161 tests passing (13 new), 100% coverage maintained
- 📝 **Docs**: Updated CODE_REVIEW.md with v2.1 audit findings
- ⚠️ **Known issues** (non-blocking): exception sanitization, late httpx import (see CODE_REVIEW.md)

### v2.0 (2026-02-28)

- 🎯 **NEW**: Cross-encoder reranking via `BAAI/bge-reranker-v2-m3` for both GraphRAG and Vector RAG
- ⚡ Two-stage retrieval: candidate pool of 20 → reranked top-5 (configurable via env vars)
- 🔧 `nexus/reranker.py`: lazy-loaded singleton with FP16 inference and `reset_reranker()` for test isolation
- 🎛️ 4 new env vars: `RERANKER_MODEL`, `RERANKER_TOP_N`, `RERANKER_CANDIDATE_K`, `RERANKER_ENABLED`
- 🛡️ Graceful fallback: reranker errors log a warning and return un-reranked results
- 📈 **Tests**: 148 tests passing (27 new reranker tests), 100% coverage maintained

### v1.9 (2026-02-28)

- ⚡ Batch ingestion tools (`ingest_graph_documents_batch`, `ingest_vector_documents_batch`) — 10-50x faster bulk operations
- 📊 `get_tenant_stats()` MCP tool for document counts per project/scope
- 🔧 Backend helper functions: `get_document_count()` for both Neo4j and Qdrant
- 📈 **Tests**: 121 tests passing (31 new tests), 100% coverage maintained

### v1.8 (2026-02-28)

- ✅ `health_check()` MCP tool for service connectivity verification
- ⚡ Index instance caching (20-50ms faster per call)
- 🎛️ Configurable: LLM timeout, context window, chunk size via environment variables
- 📊 All configuration constants extracted from magic numbers
- 🔒 Security warning added for default password

### v1.1.0

- Initial release with GraphRAG + Vector RAG
- Multi-tenant isolation
- Deduplication support
- Full test coverage
