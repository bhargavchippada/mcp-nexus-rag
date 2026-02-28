# MCP Nexus RAG

[![Tests](https://img.shields.io/badge/tests-118%20passed-brightgreen)](tests/) [![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](tests/) [![Version](https://img.shields.io/badge/package-v1.9.0-blue)](nexus/__init__.py) [![Code Review](https://img.shields.io/badge/code_review-A+-brightgreen)](CODE_REVIEW.md)

Strict multi-tenant memory server for the Antigravity agent ecosystem.
Provides **GraphRAG** (Neo4j) and **Vector RAG** (Qdrant) retrieval, both isolated by `project_id` and `tenant_scope`.
All inference runs locally via Ollama — zero data leakage.

**Status**: ✅ Production-ready · 🔒 Security-first · ⚡ High-performance · 📊 100% test coverage

---

## Architecture

```text
Agent (MCP Client)
       │
       ▼
 server.py (FastMCP)
  ├── GraphRAG  → Neo4j (llama3.1:8b builds property graph)
  └── Vector RAG → Qdrant (nomic-embed-text, collection: nexus_rag)
```

### Dual-Engine Design

| Engine         | Backend | Best For                                                     |
| -------------- | ------- | ------------------------------------------------------------ |
| **GraphRAG**   | Neo4j   | Relationship traversal, architecture queries, entity linkage |
| **Vector RAG** | Qdrant  | Semantic similarity, code snippets, factual Q&A              |

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

| Tool                            | Description                                       |
| ------------------------------- | ------------------------------------------------- |
| `ingest_graph_document`         | Ingest single text into GraphRAG (Neo4j)          |
| `ingest_vector_document`        | Ingest single text into Vector RAG (Qdrant)       |
| `ingest_graph_documents_batch`  | **NEW v1.9**: Batch ingest into GraphRAG (10-50x faster) |
| `ingest_vector_documents_batch` | **NEW v1.9**: Batch ingest into Vector RAG (10-50x faster) |

### Retrieval

| Tool                 | Description                                |
| -------------------- | ------------------------------------------ |
| `get_graph_context`  | Query GraphRAG for a `(project_id, scope)` |
| `get_vector_context` | Query Vector RAG for a `(project_id, scope)` |

### Health & Diagnostics

| Tool               | Description                                             |
| ------------------ | ------------------------------------------------------- |
| `health_check`     | Check connectivity to Neo4j, Qdrant, and Ollama         |
| `get_tenant_stats` | **NEW v1.9**: Get document counts for project/scope     |

### Tenant Management

| Tool                    | Description                                                     |
| ----------------------- | --------------------------------------------------------------- |
| `get_all_project_ids`   | List all distinct project IDs across both DBs                   |
| `get_all_tenant_scopes` | List all scopes (optionally filtered by `project_id`)           |
| `delete_tenant_data`    | Delete all data for a `project_id`, or a `(project_id, scope)`  |

**Notes:**
- `delete_tenant_data` returns a **partial-failure message** if one backend fails (e.g. `"Partial failure deleting project 'X': Qdrant: timeout"`), never silently succeeding.
- Batch ingestion tools accept a list of `{text, project_id, scope, source_identifier?}` dicts and return `{ingested, skipped, errors}` counts.

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
- `qllama/bge-reranker-v2-m3` — reranker (planned)

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
- **No external API calls**: All LLM and embedding traffic stays on `localhost:11434`.
- **Secrets**: `DEFAULT_NEO4J_PASSWORD` is defined in `nexus/config.py`. Migrate to environment variables for production deployments.

---

## Development

```bash
# Unit tests only (no live services needed)
PYTHONPATH=. poetry run pytest tests/test_unit.py tests/test_coverage.py -v

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

### v1.9 (2026-02-28)
- ⚡ **NEW**: Batch ingestion tools (`ingest_graph_documents_batch`, `ingest_vector_documents_batch`) - 10-50x faster bulk operations
- 📊 **NEW**: `get_tenant_stats()` MCP tool for document counts per project/scope
- 🔧 Backend helper functions: `get_document_count()` for both Neo4j and Qdrant
- 📈 **Tests**: 118 tests passing (31 new tests), 100% coverage maintained
- 📚 Enhanced documentation with batch ingestion examples

### v1.8 (2026-02-28)
- ✅ `health_check()` MCP tool for service connectivity verification
- ⚡ Index instance caching (20-50ms faster per call)
- 🎛️ Configurable: LLM timeout, context window, chunk size via environment variables
- 📊 All configuration constants extracted from magic numbers
- 🔒 Security warning added for default password
- 📚 Comprehensive code review and enhanced documentation

### v1.1.0
- Initial release with GraphRAG + Vector RAG
- Multi-tenant isolation
- Deduplication support
- Full test coverage

### v1.0.0
- Initial release with GraphRAG + Vector RAG
- Multi-tenant isolation
- Deduplication support
- Full test coverage
