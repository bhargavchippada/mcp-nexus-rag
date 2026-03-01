# MCP Nexus RAG: Instructions & Maintenance

This submodule contains the FastMCP server (`server.py`) and supporting tests for Multi-Tenant **GraphRAG** (Neo4j) and **Vector RAG** (Qdrant) context retrieval, powered by LlamaIndex + Ollama.

**Current package version:** `v2.3` · **Test coverage:** 100% · **Tests:** 188 passed

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
# Expected: nomic-embed-text, llama3.1:8b
# Note: bge-reranker-v2-m3 is loaded from HuggingFace Hub at runtime (not via Ollama)
```

---

## Running Tests

### Unit tests (no live services required)

```bash
poetry run pytest tests/test_unit.py tests/test_coverage.py -v
```

### Integration tests (requires live docker-compose)

```bash
poetry run pytest tests/test_integration.py -v
```

### Full suite + coverage

```bash
poetry run pytest tests/ --cov=nexus --cov=server --cov-report=term-missing
# Expected: 188 passed, 100% coverage
```

### Run the MCP server interactively

```bash
npx @modelcontextprotocol/inspector poetry run python server.py
```

---

## MCP Tools Reference

### Ingestion

| Tool                            | Arguments                                     | Notes                                               |
| ------------------------------- | --------------------------------------------- | --------------------------------------------------- |
| `ingest_graph_document`         | `text, project_id, scope, source_identifier?, auto_chunk?` | Builds property graph in Neo4j (single document)    |
| `ingest_vector_document`        | `text, project_id, scope, source_identifier?, auto_chunk?` | Stores embedding in Qdrant (single document)        |
| `ingest_graph_documents_batch`  | `documents: list[dict], skip_duplicates?, auto_chunk?`     | Batch ingest into GraphRAG (10-50x faster) |
| `ingest_vector_documents_batch` | `documents: list[dict], skip_duplicates?, auto_chunk?`     | Batch ingest into VectorRAG (10-50x faster) |

**Auto-chunking (v2.2):** Documents exceeding `MAX_DOCUMENT_SIZE` (default 512KB) are automatically split into chunks using LlamaIndex's `SentenceSplitter`. Set `auto_chunk=False` to reject oversized documents instead. Batch returns include `chunks` count.

**Batch document format:** Each dict must have `{text, project_id, scope, source_identifier?}`. Returns `{ingested, skipped, errors, chunks}`.

### Retrieval

| Tool                 | Arguments                              | Returns                        |
| -------------------- | -------------------------------------- | ------------------------------ |
| `get_graph_context`  | `query, project_id, scope, rerank?`    | Relevant graph nodes as text, reranked by bge-reranker-v2-m3 |
| `get_vector_context` | `query, project_id, scope, rerank?`    | Relevant vector chunks as text, reranked by bge-reranker-v2-m3 |

**Reranker behaviour**: Both retrieval tools fetch `RERANKER_CANDIDATE_K` (default 20) candidates, then apply the `BAAI/bge-reranker-v2-m3` cross-encoder to return the top `RERANKER_TOP_N` (default 5). Pass `rerank=False` to skip reranking for a specific call. Set `RERANKER_ENABLED=false` env var to disable globally.

### Health & Diagnostics

| Tool               | Arguments            | Returns                                                            |
| ------------------ | -------------------- | ------------------------------------------------------------------ |
| `health_check`     | —                    | Dict with status of Neo4j, Qdrant, Ollama ("ok" or error)          |
| `get_tenant_stats` | `project_id, scope?` | **v1.9**: Dict with `{graph_docs, vector_docs, total_docs}` counts |
| `print_all_stats`  | —                    | **v2.3**: ASCII table of all projects, scopes, and doc counts      |

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

## Troubleshooting

### Common Issues

| Symptom | Possible Cause | Solution |
|---------|---------------|----------|
| "Neo4j: Connection refused" | Service not running | `docker-compose ps` then `docker-compose up -d` |
| "Qdrant collection not found" | Fresh installation | Ingest first document to auto-create collection |
| Slow graph extraction | Large document size | Split into smaller chunks (<10KB recommended) |
| "Duplicate content skipped" | Content already exists | Verify `(project_id, scope, text)` tuple is truly identical |
| Tool timeout | LLM processing slow | Increase `LLM_TIMEOUT` env var (default: 300s) |
| "Error ingesting..." | Backend connectivity | Check `docker-compose logs` for specific service errors |
| "Async client is not initialized" | `QdrantVectorStore` missing `aclient=` | Upgrade to v2.1 — `get_async_qdrant_client()` now wired in `get_vector_index()` |
| "Detected nested async" | Sync LlamaIndex API called inside async context | Use `await retriever.aretrieve()` — never `.retrieve()` inside FastMCP/FastAPI |

### Service Health Checks

```bash
# Verify all containers are running
docker-compose ps

# Check Neo4j connectivity
curl http://localhost:7474 || echo "Neo4j not responding"

# Check Qdrant connectivity
curl http://localhost:6333/collections || echo "Qdrant not responding"

# Check Ollama connectivity
curl -X POST http://localhost:11434/api/generate \
  -d '{"model":"llama3.1:8b","prompt":"ping","stream":false}' \
  || echo "Ollama not responding"

# View real-time logs
docker-compose logs -f
```

### Performance Tuning

| Issue | Configuration | Default | Recommendation |
|-------|--------------|---------|----------------|
| Slow ingestion | `LLM_TIMEOUT` | 300s | Increase to 600s for large docs |
| Slow retrieval | `CHUNK_SIZE` | 1024 | Reduce to 512 for faster queries |
| High memory | `context_window` | 8192 | Reduce to 4096 if Ollama OOMs |
| Slow reranking | `RERANKER_CANDIDATE_K` | 20 | Reduce to 10 for faster p50 latency |
| Reranker OOM | `RERANKER_ENABLED` | true | Set `false` on CPU-only machines |

### Reset Procedures

**Soft reset (data only, preserves models)**:

```bash
docker-compose down
docker volume rm mcp-nexus-rag_neo4j_data mcp-nexus-rag_qdrant_data
docker-compose up -d
```

**Full reset (including Ollama models)**:

```bash
docker-compose down -v
docker-compose up -d
# Wait 5-10 minutes for model downloads
```

---

## Production Deployment

### Pre-Deployment Checklist

- [ ] Set `NEO4J_PASSWORD` environment variable (not default `password123`)
- [ ] Configure `LLM_TIMEOUT` based on expected document sizes
- [ ] Set `CHUNK_SIZE` and `CHUNK_OVERLAP` for your use case
- [ ] Review and optimize docker-compose resource limits
- [ ] Set up monitoring for Neo4j, Qdrant, and Ollama
- [ ] Configure backup strategy (Neo4j exports + Qdrant snapshots)
- [ ] Test health checks and verify all services respond
- [ ] Document tenant naming conventions (`project_id`, `scope`)
- [ ] Enable Ollama GPU acceleration if available
- [ ] Review security settings (network isolation, auth)

### Environment Variables

| Variable | Purpose | Default | Production Recommendation |
|----------|---------|---------|---------------------------|
| `NEO4J_PASSWORD` | Neo4j auth | `password123` | **Required**: Set strong password |
| `LLM_TIMEOUT` | Ollama request timeout | `300.0` | Tune based on doc sizes (60-600s) |
| `CHUNK_SIZE` | Text splitter chunk size | `1024` | Optimize for latency vs context |
| `CHUNK_OVERLAP` | Text splitter overlap | `128` | Keep at ~10% of chunk_size |
| `EMBED_MODEL` | Ollama embedding model | `nomic-embed-text` | Production-ready ✅ |
| `LLM_MODEL` | Ollama LLM model | `llama3.1:8b` | Production-ready ✅ |
| `RERANKER_MODEL` | HuggingFace reranker model | `BAAI/bge-reranker-v2-m3` | Production-ready ✅ |
| `RERANKER_TOP_N` | Reranker output count | `5` | Increase for broader context |
| `RERANKER_CANDIDATE_K` | Retrieval candidate pool | `20` | Must be ≥ RERANKER_TOP_N |
| `RERANKER_ENABLED` | Enable/disable reranker | `true` | Set `false` to skip (CPU-only envs) |

### Resource Requirements

| Service | Minimum | Recommended | Notes |
|---------|---------|-------------|-------|
| **Neo4j** | 1GB RAM | 4GB RAM | Scales with graph size |
| **Qdrant** | 512MB RAM | 2GB RAM | Scales with vector count |
| **Ollama** | 8GB RAM (CPU) | 16GB VRAM (GPU) | GPU highly recommended |
| **Postgres** | 256MB RAM | 1GB RAM | Reserved for future use |

### Monitoring Recommendations

**Critical Metrics**:
- Neo4j connection pool utilization
- Qdrant query latency (p95 < 100ms)
- Ollama response time (p95 < 30s)
- Disk usage (alert at 80%)

**Recommended Alerts**:
- Neo4j connection failures > 5/minute
- Qdrant timeout rate > 5%
- Ollama OOM errors
- Disk usage > 85%

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

---

## Code-Graph-RAG Integration (v2.3)

The **Code-Graph-RAG** system (`~/code-graph-rag`) provides AST-based code analysis complementing the semantic RAG in this project. While mcp-nexus-rag stores **semantic knowledge** (documentation, conversations, decisions), Code-Graph-RAG stores **structural code relationships** (function calls, class hierarchies, imports).

### Architecture Overview

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                         Antigravity Workspace                           │
├─────────────────────────────┬───────────────────────────────────────────┤
│   mcp-nexus-rag (Semantic)  │      code-graph-rag (Structural)          │
├─────────────────────────────┼───────────────────────────────────────────┤
│ • Neo4j GraphRAG            │ • Memgraph (Cypher-compatible)            │
│ • Qdrant Vector Store       │ • Tree-sitter AST parsing                 │
│ • LlamaIndex + Ollama       │ • Function/Class/Module relationships     │
│ • Semantic similarity       │ • CALLS, CONTAINS, IMPORTS edges          │
│ • Document ingestion        │ • Real-time file watcher                  │
└─────────────────────────────┴───────────────────────────────────────────┘

Query Priority:
1. code-graph-rag → "What functions call X?" (precise AST relationships)
2. mcp-nexus-rag  → "How does authentication work?" (semantic context)
```

### Services

| Service         | Address                 | Purpose                           |
| --------------- | ----------------------- | --------------------------------- |
| **Memgraph**    | `bolt://localhost:7688` | Code structure graph database     |
| **Memgraph Lab**| `http://localhost:3000` | Graph visualization UI            |

Start Code-Graph-RAG services:

```bash
cd ~/code-graph-rag
docker-compose up -d
```

### Real-Time File Watcher

The `realtime_updater.py` script watches a repository for file changes and updates the Memgraph database in real-time.

```bash
# Start watcher for mcp-nexus-rag
cd ~/code-graph-rag
source .venv/bin/activate
python realtime_updater.py ~/antigravity/projects/mcp-nexus-rag --host localhost --port 7688
```

**Update Algorithm** (5-step process):

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                     Real-Time Graph Update Steps                        │
├─────────────────────────────────────────────────────────────────────────┤
│ Step 1: Delete all old data from the graph for this file                │
│         - CYPHER_DELETE_MODULE: Remove Module nodes + children          │
│         - Delete File nodes: Remove non-code file entries               │
│                                                                         │
│ Step 2: Clear in-memory state for the file                              │
│         - Remove from AST cache and function registry                   │
│                                                                         │
│ Step 3: Re-parse the file (if modified/created)                         │
│         - Build AST via Tree-sitter                                     │
│         - Extract function/class/method definitions                     │
│         - Create File node for ALL file types (.py, .md, .json, etc.)   │
│                                                                         │
│ Step 4: Reprocess ALL function calls across codebase                    │
│         - Fixes "island problem" where cross-file references break      │
│         - Ensures caller→callee relationships stay consistent           │
│                                                                         │
│ Step 5: Flush all changes to Memgraph                                   │
│         - Batch write nodes and relationships                           │
└─────────────────────────────────────────────────────────────────────────┘
```

### Known Issues & Fixes

#### Issue 1: Spurious Event Handling (Fixed in PR #405)

**Symptom**: Files mysteriously disappear from the graph after opening them in an IDE.

**Root Cause**: Read-only filesystem events (`opened`, `closed_no_write`) triggered the deletion step but not the recreation step, since Step 3 only runs for `MODIFIED`/`CREATED` events.

**Fix**: Filter events at dispatch entry point:

```python
relevant_events = {EventType.MODIFIED, EventType.CREATED, "deleted"}
if event.event_type not in relevant_events:
    return
```

#### Issue 2: Non-Code Files Not Indexed in Real-Time (Fixed in PR #405)

**Symptom**: Creating a `.md` or `.json` file doesn't add it to the graph.

**Root Cause**: Step 3 only processed files with recognized language configs (Python, JS, etc.). Non-code files were only indexed during initial full scan.

**Fix**: Added `process_generic_file()` call for ALL file types:

```python
# Create File node for ALL files (code and non-code)
self.updater.factory.structure_processor.process_generic_file(path, path.name)
```

#### Issue 3: Non-Code File Deletion Not Reflected (Fixed in PR #405)

**Symptom**: Deleting a `.md` file leaves a stale entry in the graph.

**Root Cause**: `CYPHER_DELETE_MODULE` only deletes `Module` nodes (for code files). `File` nodes (used for non-code files) were never deleted.

**Fix**: Added explicit File node deletion:

```python
ingestor.execute_write(
    "MATCH (f:File {path: $path}) DETACH DELETE f", {KEY_PATH: relative_path_str}
)
```

### Hash Cache Behavior

The watcher maintains a hash cache (`.cgr-hash-cache.json`) to skip unchanged files during startup.

**Common issue**: Stale hash cache causes "Found 0 functions/methods" during initial scan.

**Solution**:

```bash
rm -f ~/antigravity/projects/mcp-nexus-rag/.cgr-hash-cache.json
# Restart watcher for full re-index
```

### Configuration

Code-Graph-RAG uses the same port scheme as mcp-nexus-rag for consistency:

| Variable            | Default | Description                          |
| ------------------- | ------- | ------------------------------------ |
| `MEMGRAPH_PORT`     | `7688`  | Bolt protocol port (mapped from 7687)|
| `MEMGRAPH_HTTP_PORT`| `7445`  | HTTP API port                        |
| `LAB_PORT`          | `3000`  | Memgraph Lab UI port                 |

**docker-compose.yaml** (code-graph-rag):

```yaml
services:
  memgraph:
    image: memgraph/memgraph-mage
    ports:
      - "${MEMGRAPH_PORT:-7688}:7687"
      - "${MEMGRAPH_HTTP_PORT:-7445}:7444"
  lab:
    image: memgraph/lab
    ports:
      - "${LAB_PORT:-3000}:3000"
    environment:
      QUICK_CONNECT_MG_HOST: memgraph
```

### Cypher Query Examples

```cypher
-- List all indexed projects
MATCH (p:Project) RETURN p.name;

-- Find functions in a module
MATCH (m:Module)-[:CONTAINS]->(f:Function)
WHERE m.path CONTAINS 'server.py'
RETURN f.name, f.qualified_name;

-- Find all callers of a function
MATCH (caller)-[:CALLS]->(f:Function {name: 'ingest_document'})
RETURN caller.qualified_name;

-- Get function hierarchy for a file
MATCH (m:Module {path: 'nexus/tools.py'})-[:CONTAINS*]->(n)
RETURN n.name, labels(n);

-- List all File nodes (including non-code)
MATCH (f:File) RETURN f.name, f.path ORDER BY f.path;
```

### Code-Graph-RAG Troubleshooting

| Symptom                            | Cause                    | Solution                                        |
| ---------------------------------- | ------------------------ | ----------------------------------------------- |
| "Found 0 functions/methods"        | Stale hash cache         | Delete `.cgr-hash-cache.json` and restart       |
| Files not appearing after creation | Watcher not running      | Check `ps aux \| grep realtime_updater`         |
| Graph shows stale deleted files    | Old bug before PR #405   | Update code-graph-rag and re-index              |
| Connection refused on 7688         | Memgraph not running     | `docker-compose up -d` in code-graph-rag        |
| IDE triggers false deletions       | Old bug before PR #405   | Update to include event filtering fix           |

### Upstream Contribution

Bug fixes for Code-Graph-RAG are contributed back to the open-source project:

- **Repository**: [vitali87/code-graph-rag](https://github.com/vitali87/code-graph-rag)
- **PR #405**: fix(realtime): handle non-code files and filter spurious events
