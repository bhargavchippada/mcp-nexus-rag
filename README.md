# MCP Nexus RAG

[![Tests](https://img.shields.io/badge/tests-99%20passed-brightgreen)](tests/) [![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](tests/) [![Version](https://img.shields.io/badge/package-v1.1.0-blue)](nexus/__init__.py)

Strict multi-tenant memory server for the Antigravity agent ecosystem.
Provides **GraphRAG** (Neo4j) and **Vector RAG** (Qdrant) retrieval, both isolated by `project_id` and `tenant_scope`.
All inference runs locally via Ollama — zero data leakage.

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

| Tool                     | Description                                                    |
| ------------------------ | -------------------------------------------------------------- |
| `ingest_graph_document`  | Ingest text into GraphRAG (Neo4j)                              |
| `ingest_vector_document` | Ingest text into Vector RAG (Qdrant)                           |
| `get_graph_context`      | Query GraphRAG for a `(project_id, scope)`                     |
| `get_vector_context`     | Query Vector RAG for a `(project_id, scope)`                   |
| `get_all_project_ids`    | List all distinct project IDs across both DBs                  |
| `get_all_tenant_scopes`  | List all scopes (optionally filtered by `project_id`)          |
| `delete_tenant_data`     | Delete all data for a `project_id`, or a `(project_id, scope)` |

`delete_tenant_data` returns a **partial-failure message** if one backend fails (e.g. `"Partial failure deleting project 'X': Qdrant: timeout"`), never silently succeeding.

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

See [`INSTRUCTIONS.md`](INSTRUCTIONS.md) for infrastructure ops, data reset, and advanced maintenance.
