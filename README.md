# MCP Nexus RAG

<!-- Executive summary: tech stack, mission, architecture -->

**Version:** v5.0

> See [AGENTS.md](AGENTS.md) for commands | [MEMORY.md](MEMORY.md) for state | [TODO.md](TODO.md) for tasks

Strict multi-tenant memory server for the Antigravity agent ecosystem.
Provides **GraphRAG** (Memgraph) and **Vector RAG** (pgvector/Postgres) retrieval, both isolated by `project_id` and `tenant_scope`.
All inference runs locally via Ollama -- zero data leakage.

**Status**: Production-ready | Security-first | High-performance | 433 tests passing (445 total, 12 deselected) | Redis semantic cache integrated

---

## Architecture

```text
Agent (MCP Client)
       |
       v
 server.py (FastMCP)
  |-- GraphRAG  -> Memgraph (qwen2.5:3b builds property graph)
  |                 \-- bge-reranker-v2-m3 (cross-encoder reranker)
  \-- Vector RAG -> pgvector/Postgres (nomic-embed-text, table: data_nexus_rag)
                    \-- bge-reranker-v2-m3 (cross-encoder reranker)
```

### Dual-Engine Design

| Engine         | Backend            | Best For                                                     |
| -------------- | ------------------ | ------------------------------------------------------------ |
| **GraphRAG**   | Memgraph (port 7689) | Relationship traversal, architecture queries, entity linkage |
| **Vector RAG** | pgvector (Postgres)  | Semantic similarity, code snippets, factual Q&A              |

### Reranker Pipeline (v2.0)

Both retrieval tools use a two-stage pipeline:

1. **Candidate retrieval** -- fetch `RERANKER_CANDIDATE_K` (default 20) nodes from the index
2. **Cross-encoder reranking** -- `BAAI/bge-reranker-v2-m3` scores each candidate against the query
3. **Top-N selection** -- return the `RERANKER_TOP_N` (default 5) highest-scoring nodes

The reranker is a lazy-loaded singleton (FP16, loaded once per process). Pass `rerank=False` to either tool to skip reranking and return raw retrieval results.

**Shared Reranker Mode (v3.0):** Set `RERANKER_MODE=remote` to offload reranking to a shared HTTP microservice (`reranker_service.py` on port 8767), saving ~2 GB VRAM when both `server.py` and `http_server.py` are running. Default: `local` (in-process model loading, no behavior change).

```text
RERANKER_MODE=remote:
  server.py ------> RemoteReranker --HTTP--> reranker_service.py :8767 --> FlagEmbeddingReranker [~2GB]
  http_server.py -> RemoteReranker --HTTP--/
```

### Multi-Tenant Isolation

Every document and query carries two required metadata keys:

| Key            | Role                       | Examples                                   |
| -------------- | -------------------------- | ------------------------------------------ |
| `project_id`   | Top-level tenant namespace | `TRADING_BOT`, `WEB_PORTAL`                |
| `tenant_scope` | Domain within a project    | `CORE_CODE`, `SYSTEM_LOGS`, `WEB_RESEARCH` |

The `(project_id, tenant_scope)` tuple is enforced as an exact-match filter in both Memgraph Cypher and pgvector SQL queries -- zero crosstalk between projects or scopes.

---

## MCP Tools

### Ingestion

| Tool                            | Description                                                      |
| ------------------------------- | ---------------------------------------------------------------- |
| `ingest_document`               | Ingest into both GraphRAG + Vector RAG in one call               |
| `ingest_document_batches`       | Batch equivalent of `ingest_document`                            |
| `ingest_graph_document`         | Ingest single text into GraphRAG (Memgraph)                      |
| `ingest_vector_document`        | Ingest single text into Vector RAG (pgvector)                    |
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
| `health_check`     | Check connectivity to Memgraph, pgvector, and Ollama    |
| `get_tenant_stats` | Get document/node counts for project/scope              |
| `print_all_stats`  | Display ASCII table of all projects, scopes, and counts |

`get_tenant_stats` returns:

| Key                  | Description                                                     |
| -------------------- | --------------------------------------------------------------- |
| `graph_nodes_total`  | All Memgraph nodes for the project/scope                        |
| `graph_chunk_nodes`  | Source doc nodes (have `content_hash`) -- what you ingested      |
| `graph_entity_nodes` | LLM-extracted concept/entity nodes (no `content_hash`)          |
| `vector_docs`        | pgvector row count                                              |
| `total_docs`         | `graph_nodes_total` + `vector_docs`                             |

`print_all_stats` table columns: `PROJECT_ID`, `SCOPE`, `GRAPH`, `CHUNKS`, `ENTITIES`, `VECTOR`, `TOTAL`

### Tenant Management

| Tool                    | Description                                                     |
| ----------------------- | --------------------------------------------------------------- |
| `get_all_project_ids`   | List all distinct project IDs across both DBs                   |
| `get_all_tenant_scopes` | List all scopes (optionally filtered by `project_id`)           |
| `delete_tenant_data`    | Delete all data for a `project_id`, or a `(project_id, scope)`  |

### Administration & Sync

| Tool                       | Description                                                                            |
| -------------------------- | -------------------------------------------------------------------------------------- |
| `delete_all_data`          | **Full wipe** -- delete all data from Memgraph and pgvector across all tenants         |
| `ingest_project_directory` | Recursively ingest an entire directory tree into both GraphRAG and VectorRAG            |
| `sync_project_files`       | Re-ingest tracked persona file (CLAUDE.md) if changed (idempotent, SHA-256 dedup)      |
| `sync_deleted_files`       | Remove stale database entries for files deleted from disk                               |
| `list_core_doc_files`      | List tracked persona file(s) for sync (dry-run for `sync_project_files`)               |
| `invalidate_project_cache` | Targeted cache invalidation without data deletion                                      |
| `cache_stats`              | Get Redis cache stats: key count, memory usage, TTL settings                           |

**Notes:**

- `delete_tenant_data` returns a **partial-failure message** if one backend fails, never silently succeeding.
- Batch ingestion tools accept a list of `{text, project_id, scope, source_identifier?}` dicts and return `{ingested, skipped, errors}` counts.
- Reranker errors are caught and logged as warnings; the tool falls back to un-reranked results rather than failing.

---

## Infrastructure

Services are defined in `docker-compose.yml`:

| Service          | Address                  | Purpose                                   |
| ---------------- | ------------------------ | ----------------------------------------- |
| **Memgraph RAG** | `bolt://localhost:7689`  | GraphRAG property graph store             |
| **Postgres**     | `localhost:5432`         | pgvector vector store (`data_nexus_rag`)  |
| **Ollama**       | `http://localhost:11434` | Local LLM + embeddings                    |
| **Redis**        | `redis://localhost:6379` | Semantic query result cache (24h TTL)     |

Models auto-pulled by `ollama-init` on first start:

- `nomic-embed-text` -- embeddings (768 dimensions)
- `qwen2.5:3b` -- graph extraction + answer synthesis

> **Note**: `BAAI/bge-reranker-v2-m3` is loaded directly from HuggingFace Hub by `llama-index-postprocessor-flag-embedding-reranker` -- it does **not** require an Ollama pull. Requires `FlagEmbedding>=1.3.5,<2.0.0` and `transformers>=4.40.0,<5.0.0`.

---

## Quick Start

```bash
# 1. Start services
docker-compose up -d

# 2. Enable pgvector extension (first time only)
docker exec turiya-postgres psql -U admin -d turiya_memory -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 3. Install dependencies
poetry install

# 4. Run the full suite + coverage (no live services required)
PYTHONPATH=. poetry run pytest tests/ --cov=nexus --cov=server --cov-report=term-missing
```

---

## Environment Variables

| Variable               | Default                    | Description                                                         |
| ---------------------- | -------------------------- | ------------------------------------------------------------------- |
| `RERANKER_MODEL`       | `BAAI/bge-reranker-v2-m3`  | HuggingFace model ID for the cross-encoder                          |
| `RERANKER_TOP_N`       | `8`                        | Number of results returned after reranking                          |
| `RERANKER_CANDIDATE_K` | `20`                       | Candidate pool size fetched before reranking                        |
| `RERANKER_ENABLED`     | `true`                     | Set to `false` to disable reranking globally                        |
| `RERANKER_MODE`        | `local`                    | `local` = in-process model, `remote` = shared HTTP service          |
| `RERANKER_SERVICE_URL` | `http://localhost:8767`    | URL of the shared reranker service (only used when mode=remote)     |
| `MAX_DOCUMENT_SIZE`    | `4096` (4KB)               | Documents larger than this are auto-chunked on ingest               |
| `MAX_CONTEXT_CHARS`    | `1500`                     | Hard cap on chars returned by retrieval tools (0 = disabled)        |
| `INGEST_CHUNK_SIZE`    | `512`                      | Chunk size for large document splitting                             |
| `INGEST_CHUNK_OVERLAP` | `64`                       | Overlap between chunks                                              |
| `MEMGRAPH_URL`         | `bolt://localhost:7689`    | Memgraph RAG connection URI                                         |
| `MEMGRAPH_USER`        | (empty)                    | Memgraph username (no auth by default)                              |
| `MEMGRAPH_PASSWORD`    | (empty)                    | Memgraph password (no auth by default)                              |
| `PG_HOST`              | `localhost`                | PostgreSQL host                                                     |
| `PG_PORT`              | `5432`                     | PostgreSQL port                                                     |
| `PG_USER`              | `admin`                    | PostgreSQL user                                                     |
| `PG_PASSWORD`          | `password123`              | PostgreSQL password (use env var in production)                     |
| `PG_DB`                | `turiya_memory`            | PostgreSQL database name                                            |
| `OLLAMA_URL`           | `http://localhost:11434`   | Ollama base URL                                                     |
| `REDIS_URL`            | `redis://localhost:6379`   | Redis connection URL for semantic cache                             |
| `CACHE_TTL`            | `86400` (24h)              | Cache entry TTL in seconds                                          |
| `CACHE_ENABLED`        | `true`                     | Set to `false` to bypass Redis cache globally                       |

---

## Querying the Server

The server uses **stdio transport** (standard MCP protocol), not HTTP -- so `curl` cannot reach it directly.

### Option 1: MCP Inspector (browser UI)

```bash
npx @modelcontextprotocol/inspector poetry run python server.py
```

In the browser UI, select a tool, fill in the arguments, and click **Run**.

### Option 2: Python MCP client (scriptable)

The `mcp` Python package is already installed as a transitive dependency. Use it to call any tool:

```bash
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

# Change the tool name and arguments as needed:
#   "get_graph_context"  - query GraphRAG (add rerank=False to skip reranking)
#   "get_vector_context" - query Vector RAG
#   "answer_query"       - combined RAG answer via Ollama LLM
#   "health_check"       - service connectivity check (no arguments needed)
#   "ingest_vector_document" - ingest text (add text=, project_id=, scope=)
asyncio.run(call(
    "get_vector_context",
    query="how does deduplication work?",
    project_id="MY_PROJECT",
    scope="CORE_CODE",    # omit or pass scope="" to query all scopes
))
EOF
```

### Option 3: Direct function call (no MCP overhead)

```bash
poetry run python - <<'EOF'
import asyncio
from nexus.tools import get_vector_context

async def main():
    result = await get_vector_context(
        query="how does deduplication work?",
        project_id="MY_PROJECT",
        scope="CORE_CODE",
    )
    print(result)

asyncio.run(main())
EOF
```

---

## MCP Client Configuration

For the Antigravity workspace the active config lives at `~/antigravity/.mcp.json`.
The `nexus` MCP server is auto-started by Claude Code on session init.

**Nexus RAG** entry (uses venv Python for dependency isolation):

```json
{
  "mcpServers": {
    "nexus": {
      "command": "/home/turiya/antigravity/projects/mcp-nexus-rag/.venv/bin/python",
      "args": ["/home/turiya/antigravity/projects/mcp-nexus-rag/server.py"],
      "env": {
        "OLLAMA_URL": "http://localhost:11434",
        "MEMGRAPH_URL": "bolt://localhost:7689",
        "REDIS_URL": "redis://localhost:6379"
      }
    }
  }
}
```

**Other clients** (Cursor, Claude Desktop -- replace the path):

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

- **Metadata key allowlist**: `nexus.config.ALLOWED_META_KEYS` -- only `project_id`, `tenant_scope`, `source`, `content_hash` are accepted as Cypher/SQL property names, preventing key injection.
- **No external API calls**: All LLM and embedding traffic stays on `localhost:11434`. The reranker model is downloaded from HuggingFace Hub on first use and cached locally.
- **Exception sanitization**: All public tools return generic error messages to MCP clients; full exception details logged server-side only.
- **Secrets**: `DEFAULT_PG_PASSWORD` is defined in `nexus/config.py`. Migrate to environment variables for production deployments.

---

## Development

```bash
# Unit tests only (no live services needed)
PYTHONPATH=. poetry run pytest tests/test_unit.py tests/test_coverage.py tests/test_reranker.py -v

# Integration tests (slow, requires docker-compose)
PYTHONPATH=. poetry run pytest tests/test_integration.py -v

# Full suite + 100% coverage
PYTHONPATH=. poetry run pytest tests/ --cov=nexus --cov=server --cov-report=term-missing

# Interactive MCP Inspector
npx @modelcontextprotocol/inspector poetry run python server.py
```

### Operational Audit Notes (2026-03-04)

- Integrity cleanup completed: duplicate `content_hash` groups and unscoped graph chunks removed.
- `file_path` metadata normalization completed in both stores (absolute paths converted to workspace-relative).
- `nexus.watcher` daemon startup hardened via `start-services.sh` process-stability checks.
- `ingest_document` normalizes workspace-absolute `file_path` inputs to relative metadata before graph/vector ingest.
- `scripts/safe_cleanup.py` deduplicates Memgraph `:Chunk` nodes only (avoids false-positive dedup against non-chunk graph nodes).

### Backend Migration: Neo4j/Qdrant -> Memgraph/pgvector (2026-03-10)

- **Neo4j -> Memgraph:** `MemgraphPropertyGraphStore` (llama-index-graph-stores-memgraph v0.4.1), dedicated container on port 7689 (separate from code-graph-rag on 7688). 25x faster reads, no JVM overhead.
- **Qdrant -> pgvector:** `PGVectorStore` (llama-index-vector-stores-postgres v0.7.3) using existing Postgres container with pgvector extension. HNSW index, 768 dimensions (nomic-embed-text). Eliminates standalone Qdrant container.
- **Backend modules:** `nexus/backends/memgraph.py` (graph ops), `nexus/backends/pgvector.py` (vector ops via psycopg2 SQL).
- All 433 tests pass, lint clean.

### Watcher Simplification (2026-03-09)

- Watcher now tracks **only `CLAUDE.md`** (agent persona). Per-project core docs (README.md, MEMORY.md, AGENTS.md, TODO.md) removed from auto-sync.
- Database fully wiped and re-initialized for clean CLAUDE.md-only ingestion.
- `sync.py` v2.0: removed `CORE_DOC_PATTERNS`, `PROJECT_MAPPINGS`, `_project_id_from_path()`. Single tracked file: `CLAUDE.md` → `("AGENT", "PERSONA")`.

```bash
# Integrity audit / cleanup (dry-run first, then apply)
cd ~/antigravity/projects/mcp-nexus-rag
PYTHONPATH=. poetry run python scripts/safe_cleanup.py
PYTHONPATH=. poetry run python scripts/safe_cleanup.py --apply
```
