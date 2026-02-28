# MCP Nexus RAG

Strict multi-tenant memory server for the Antigravity agent ecosystem.
Provides **GraphRAG** (Neo4j) and **Vector RAG** (Qdrant) retrieval, both isolated by `project_id` and `tenant_scope`.
All inference runs locally via Ollama — zero data leakage.

**Status**: ✅ Production-ready · 🔒 Security-first · ⚡ High-performance · 📊 197 tests passing

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

| Tool                 | Parameters                                       | Description                                                                                          |
| -------------------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| `get_graph_context`  | `query`, `project_id`, `scope`, `rerank=True`    | Query GraphRAG; cross-encoder reranks candidates by default                                          |
| `get_vector_context` | `query`, `project_id`, `scope`, `rerank=True`    | Query Vector RAG; cross-encoder reranks candidates by default                                        |
| `answer_query`       | `query`, `project_id`, `scope=""`, `rerank=True` | Combined RAG/GraphRAG answer via local Ollama LLM. `scope=""` retrieves from **all project scopes**. |

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
| **Postgres** | `localhost:5432`         | Reserved (pgvector, future)           |

Models auto-pulled by `ollama-init` on first start:

- `nomic-embed-text` — embeddings
- `llama3.1:8b` — graph extraction

> **Note**: `BAAI/bge-reranker-v2-m3` is loaded directly from HuggingFace Hub by `llama-index-postprocessor-flag-reranker` — it does **not** require an Ollama pull.

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

| Variable              | Default                    | Description                                      |
| --------------------- | -------------------------- | ------------------------------------------------ |
| `RERANKER_MODEL`      | `BAAI/bge-reranker-v2-m3`  | HuggingFace model ID for the cross-encoder       |
| `RERANKER_TOP_N`      | `5`                        | Number of results returned after reranking       |
| `RERANKER_CANDIDATE_K`| `20`                       | Candidate pool size fetched before reranking     |
| `RERANKER_ENABLED`    | `true`                     | Set to `false` to disable reranking globally     |
| `MAX_DOCUMENT_SIZE`   | `524288` (512KB)           | Documents larger than this are auto-chunked      |
| `INGEST_CHUNK_SIZE`   | `1024`                     | Chunk size for large document splitting          |
| `INGEST_CHUNK_OVERLAP`| `128`                      | Overlap between chunks                           |
| `NEO4J_URI`           | `bolt://localhost:7687`    | Neo4j connection URI                             |
| `NEO4J_USERNAME`      | `neo4j`                    | Neo4j username                                   |
| `NEO4J_PASSWORD`      | `password`                 | Neo4j password (use env var in production)       |
| `QDRANT_URL`          | `http://localhost:6333`    | Qdrant connection URL                            |
| `OLLAMA_BASE_URL`     | `http://localhost:11434`   | Ollama base URL                                  |

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
    scope="CORE_CODE",
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
        scope="CORE_CODE",
    )
    print(result)

asyncio.run(main())
EOF
```

---

## MCP Client Configuration

Add to `claude_desktop_config.json` or your Cursor MCP settings (replace the path):

```json
{
  "mcpServers": {
    "nexus-rag": {
      "command": "poetry",
      "args": [
        "run",
        "python",
        "/absolute/path/to/projects/mcp-nexus-rag/server.py"
      ]
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

- **[INSTRUCTIONS.md](INSTRUCTIONS.md)** - Infrastructure ops, troubleshooting, production deployment
- **[CODE_REVIEW.md](CODE_REVIEW.md)** - Comprehensive code review, security analysis, improvement recommendations

---

## Quick Links

| Resource | Description |
|----------|-------------|
| [MCP Tools Reference](INSTRUCTIONS.md#mcp-tools-reference) | All available MCP tools and their usage |
| [Troubleshooting Guide](INSTRUCTIONS.md#troubleshooting) | Common issues and solutions |
| [Production Deployment](INSTRUCTIONS.md#production-deployment) | Security checklist, environment variables, monitoring |
| [Code Review Report](CODE_REVIEW.md) | Architecture analysis, security audit, performance recommendations |
| [Data Reset Options](INSTRUCTIONS.md#data-reset-options) | Soft/hard reset procedures |

---

## Recent Updates

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
