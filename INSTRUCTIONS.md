# MCP Nexus RAG: Instructions & Maintenance

This submodule contains the FastMCP server (`server.py`) and supporting tests for Multi-Tenant **GraphRAG** (Neo4j) and **Vector RAG** (Qdrant) context retrieval, powered by LlamaIndex + Ollama.

**Current package version:** `v1.1.0` · **Test coverage:** 100% · **Tests:** 99 passed

---

## Core Services

All services are declared in `docker-compose.yml` in this directory:

| Service      | Address                  | Auth                  |
| ------------ | ------------------------ | --------------------- |
| **Neo4j**    | `bolt://localhost:7687`  | `neo4j / password123` |
| **Qdrant**   | `http://localhost:6333`  | —                     |
| **Ollama**   | `http://localhost:11434` | —                     |
| **Postgres** | `localhost:5432`         | `admin / password123` |

> Postgres is reserved for future pgvector use. The RAG server currently uses Neo4j + Qdrant only.

Start everything:

```bash
docker-compose up -d
```

---

## Setup

```bash
# Install runtime + dev dependencies
poetry install --with dev

# Verify Ollama models are ready
docker exec -it turiya-ollama ollama list
# Expected: nomic-embed-text, llama3.1:8b, qllama/bge-reranker-v2-m3
```

---

## Running Tests

### Unit tests (no live services required)

```bash
PYTHONPATH=. poetry run pytest tests/test_unit.py tests/test_coverage.py -v
```

### Integration tests (requires live docker-compose)

```bash
PYTHONPATH=. poetry run pytest tests/test_integration.py -v
```

### Full suite + coverage

```bash
PYTHONPATH=. poetry run pytest tests/ --cov=nexus --cov=server --cov-report=term-missing
# Expected: 99 passed, 100% coverage
```

### Run the MCP server interactively

```bash
npx @modelcontextprotocol/inspector poetry run python server.py
```

---

## MCP Tools Reference

### Ingestion

| Tool                     | Arguments                                     | Notes                                               |
| ------------------------ | --------------------------------------------- | --------------------------------------------------- |
| `ingest_graph_document`  | `text, project_id, scope, source_identifier?` | Builds property graph in Neo4j                      |
| `ingest_vector_document` | `text, project_id, scope, source_identifier?` | Stores embedding in Qdrant (`nexus_rag` collection) |

### Retrieval

| Tool                 | Arguments                  | Returns                        |
| -------------------- | -------------------------- | ------------------------------ |
| `get_graph_context`  | `query, project_id, scope` | Relevant graph nodes as text   |
| `get_vector_context` | `query, project_id, scope` | Relevant vector chunks as text |

### Tenant Management

| Tool                    | Arguments            | Returns                                                                                                                                                                    |
| ----------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `get_all_project_ids`   | —                    | Sorted list of all distinct `project_id` values (merged from both DBs)                                                                                                     |
| `get_all_tenant_scopes` | `project_id?`        | Sorted list of scopes; if `project_id` is given, only scopes for that project                                                                                              |
| `delete_tenant_data`    | `project_id, scope?` | Deletes all data for the project (or just one scope if `scope` is given). Returns `"Successfully deleted..."` or `"Partial failure deleting ...: Neo4j: ...; Qdrant: ..."` |

---

## Data Reset Options

### Soft reset (preserve Ollama models)

Deletes only Neo4j, Qdrant, and Postgres data. Volume prefix (`mcp-nexus-rag`) may differ — check `docker volume ls`.

```bash
docker-compose down
docker volume rm mcp-nexus-rag_neo4j_data mcp-nexus-rag_qdrant_data mcp-nexus-rag_postgres_data
docker-compose up -d
```

**Verify empty state:**

- Neo4j: <http://localhost:7474> → should show 0 nodes
- Qdrant: <http://localhost:6333/dashboard> → should show 0 collections
- Ollama: `docker exec -it turiya-ollama ollama list` → models still present

### Selective programmatic delete (via MCP tool)

```python
delete_tenant_data(project_id="TRADING_BOT")              # wipe entire project
delete_tenant_data(project_id="TRADING_BOT", scope="SYSTEM_LOGS")  # wipe one scope
```

### Full wipe (including Ollama models)

```bash
docker-compose down -v
```

> ⚠️ This destroys all volumes including the ~5 GB Ollama model cache.

---

## Viewing the Graph

```bash
# Launch Dash visualizer
poetry run python visualizer.py
```

Then open the printed URL in your browser to explore Neo4j relationships interactively.

---

## Helpful Commands

### Development & Formatting

```bash
# Lint and fix Python code
poetry run ruff check . --fix

# Format Python code
poetry run ruff format .

# Check Markdown linting (pymarkdown + markdownlint)
poetry run pymarkdown scan .
npx markdownlint-cli .

# Auto-format Markdown
npx prettier --write .
```

### Docker Operations

```bash
# Live container logs
docker-compose logs -f

# Check container health and status
docker-compose ps

# Force recreate all containers (useful if images or env changed)
docker-compose up -d --force-recreate
```

### Ollama Operations

```bash
# Test Ollama connectivity
curl -X POST http://localhost:11434/api/generate \
  -d '{"model":"llama3.1:8b","prompt":"ping","stream":false}'

# List downloaded models
docker exec -it turiya-ollama ollama list

# Manually pull a missing model
docker exec -it turiya-ollama ollama pull llama3.1:8b
```

### Database Operations

```bash
# Inspect Qdrant collection points
curl http://localhost:6333/collections/nexus_rag/points/count

# Hard delete the entire Qdrant collection natively (bypass MCP tool)
curl -X DELETE http://localhost:6333/collections/nexus_rag

# Inspect Postgres directly
docker exec -it turiya-postgres psql -U admin -d turiya_memory
```

---

## MCP Client Configuration

Add to `claude_desktop_config.json` or Cursor MCP settings:

```json
{
  "mcpServers": {
    "nexus-rag": {
      "command": "poetry",
      "args": [
        "run",
        "python",
        "/absolute/path/to/antigravity/projects/mcp-nexus-rag/server.py"
      ]
    }
  }
}
```

---

## Security & Design Notes

| Topic                | Decision                                                                                                                                                   |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Key injection**    | Only `project_id`, `tenant_scope`, `source`, `content_hash` are accepted as Neo4j property names (`_ALLOWED_META_KEYS`). All others raise `ValueError`.    |
| **Thread safety**    | `setup_settings()` uses a `threading.Lock` with double-checked locking — safe for concurrent MCP requests.                                                 |
| **Collection name**  | Hardcoded as `COLLECTION_NAME = "nexus_rag"` constant — single source of truth; no scattered string literals.                                              |
| **Delete semantics** | Both Neo4j and Qdrant deletions run independently; partial failures are collected and returned as a descriptive error string rather than silently ignored. |
| **No external APIs** | All LLM and embedding calls go to `localhost:11434`. Zero data exfiltration.                                                                               |

---

## Deduplication Design (v1.6)

Both ingest tools perform a **tenant-scoped SHA-256 content check** before touching any index:

```text
hash = SHA-256(project_id \x00 scope \x00 text)
```

Including `project_id` and `scope` in the hash means the same document in different projects or scopes is **never** treated as a duplicate.

### Per-backend strategy

| Backend             | Dedup mechanism                                                                   | Where hash is stored                        |
| ------------------- | --------------------------------------------------------------------------------- | ------------------------------------------- |
| **Qdrant** (vector) | Scroll for `content_hash` + `project_id` + `tenant_scope` before embed            | Qdrant point payload (`content_hash` field) |
| **Neo4j** (graph)   | Cypher `MATCH (n {content_hash, project_id, tenant_scope})` before LLM extraction | Node properties set by LlamaIndex metadata  |

### Return values

| Condition       | Return string                                                                                          |
| --------------- | ------------------------------------------------------------------------------------------------------ |
| New content     | `"Successfully ingested ..."`                                                                          |
| Duplicate found | `"Skipped: duplicate content already exists in [GraphRAG\|VectorRAG] for project '...', scope '...'."` |
| Ingest error    | `"Error ingesting ...: <exception>"`                                                                   |

### Fail-open behaviour

If the dedup check itself fails (e.g. Qdrant or Neo4j is temporarily down), the function **falls through to ingestion** rather than silently discarding content. A `WARNING` is logged.

### doc_id determinism

Both tools set `doc.doc_id = content_hash`. This means:

- **Qdrant**: LlamaIndex derives point IDs from `doc_id` → Qdrant upserts rather than appending a duplicate even if the pre-check races.
- **Neo4j**: The property graph store tracks documents by `doc_id` at the LlamaIndex layer.

### Clearing existing duplicates

If duplicates were ingested before v1.6, delete the project/scope and re-ingest:

```bash
# Via MCP tool
delete_tenant_data(project_id="MY_PROJECT")  # or with scope= for targeted cleanup
# Then re-ingest — dedup will prevent recurrence
```
