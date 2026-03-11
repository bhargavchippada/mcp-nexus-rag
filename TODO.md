# TODO.md — MCP Nexus RAG

<!-- Pending tasks: [ ] incomplete -->

**Version:** v5.4

## Pending

### P0 Priority — Immediate (Code Review 2026-03-09)

- [ ] **Split tools.py (2071 lines)** into modular structure
  - Create `nexus/tools/ingest.py` — ingestion tools (single/batch, graph/vector)
  - Create `nexus/tools/query.py` — retrieval tools (graph context, vector context, answer_query)
  - Create `nexus/tools/admin.py` — admin tools (delete, stats, health check)
  - Create `nexus/tools/sync.py` — sync tools (sync_project_files, sync_deleted_files)
  - Update `nexus/tools/__init__.py` to re-export all tools
  - Update imports in `server.py`, `watcher.py`, `http_server.py`
  - Estimated effort: 4-6 hours
  - **Reference:** `artifacts/code_review_2026-03-09.md`

### Hardening

- [ ] **Add per-tenant rate limiting** — in-memory limiter keyed by `(project_id, scope)`
  - Implement `_rate_limits = defaultdict(list)` with configurable `RATE_LIMIT` (e.g., 10 req/min)
  - Add `check_rate_limit(project_id, scope)` helper in `tools/__init__.py`
  - Apply to all high-cost tools (ingest, query, answer_query)
  - Add env var `RATE_LIMIT_REQUESTS_PER_MINUTE` (default: 10)
  - Estimated effort: 1-2 hours
  - **Reference:** `artifacts/code_review_2026-03-09.md`
- [ ] **Add watcher auto-restart guard** for `nexus.watcher` daemon mode
  - Implement supervisor loop in `start-services.sh` with exponential backoff
  - Detect death (process exit) and auto-restart
  - Add max restart count to prevent infinite loops
  - Estimated effort: 2-3 hours
  - **Reference:** `artifacts/code_review_2026-03-09.md`
- [x] Add initial-scan mode to watcher — v2.1: bootstraps unsynced files on startup (2026-03-10)
- [ ] [LOW] Clean up old host Ollama models at `/usr/share/ollama/.ollama/models` (systemd Ollama replaced by host `ollama serve`)
- [ ] [LOW] Remove stale Docker volumes: `docker volume rm mcp-nexus-rag_neo4j_data mcp-nexus-rag_qdrant_data` (orphaned from v2.x → v3.0 migration)
- [ ] [MED] Expand `PERSONA_FILES` in `sync.py` — currently only tracks `CLAUDE.md`; docstring in `sync_project_files` claims to track README.md/MEMORY.md/AGENTS.md/TODO.md per project but code doesn't match

### P2 Priority — Next Sprint (Code Review 2026-03-09)

- [ ] **Add async batch parallelism** with `asyncio.gather()`
  - Parallelize graph/vector retrieval for multiple scopes
  - Add per-task timeout via `asyncio.wait_for()`
  - Estimated effort: 4-6 hours
  - **Reference:** `artifacts/code_review_2026-03-09.md`

### Refactoring

- [ ] **Generate OpenAPI documentation** for `http_server.py`
  - Enable FastAPI's built-in `/openapi.json`
  - Deploy Swagger UI at `/docs` endpoint
  - Add API documentation to README.md
  - Estimated effort: 1 hour
  - **Reference:** `artifacts/code_review_2026-03-09.md`

### P3 Priority — Backlog (Code Review 2026-03-09)

### Dependencies (major — manual review required)

- [ ] [LOW] `redis` 5.3.1 → 7.2.1 — MAJOR; review async client API changes
- [ ] [LOW] `watchdog` 4.0.2 → 6.0.0 — MAJOR; review event handler API changes
- [ ] [LOW] `huggingface-hub` 0.36.2 → 1.5.0 — MAJOR; review download API
- [ ] [LOW] `transformers` 4.57.6 → 5.2.0 — MAJOR; review pipeline API changes
- [ ] [LOW] `marshmallow` 3.26.2 → 4.2.2 — MAJOR; review Schema API changes
- [ ] [LOW] `pytest-cov` 6.3.0 → 7.0.0 — MAJOR; low risk (dev dep)

### Features

- [ ] Structured JSONL logging
- [ ] Export/import tenant data tools (`export_tenant_data`, `import_tenant_data`) — backup/restore
- [ ] Add `deduplicate_tenant_data` admin tool (remove duplicate `content_hash` records per `(project_id, scope)` in both stores)
- [ ] `get_reranker_stats` tool — expose reranker performance metrics (latency, throughput)
- [ ] `compare_retrieval_methods` tool — A/B comparison of graph vs vector retrieval for debugging
- [ ] `search_by_metadata` tool — filter documents by source/file_path without text query

---

## Completed

- [x] **Backend migration: Neo4j/Qdrant → Memgraph/pgvector** (2026-03-10)
  - Neo4j → Memgraph (port 7689, `MemgraphPropertyGraphStore`, llama-index-graph-stores-memgraph v0.4.1)
  - Qdrant → pgvector (existing Postgres, `PGVectorStore`, llama-index-vector-stores-postgres v0.7.3)
  - New backends: `nexus/backends/memgraph.py`, `nexus/backends/pgvector.py`
  - Deleted: `nexus/backends/neo4j.py`, `nexus/backends/qdrant.py`
  - Docker: removed Neo4j + Qdrant containers, added Memgraph RAG container
  - All 433 tests pass, lint clean, scripts updated
- [x] **Watcher dedup fix: file_content_hash + per-file locks** (2026-03-10)
  - Added `file_content_hash` metadata field on all ingested chunks (tools.py v5.1)
  - Added `is_file_content_duplicate()` to neo4j.py v2.5 and qdrant.py v2.6
  - `check_file_sync_status()` now uses whole-file hash dedup (sync.py v2.1)
  - Per-file asyncio locks prevent watcher/sync race conditions
  - Verified: second sync correctly returns "nothing to sync"
- [x] **Performance optimization: chunk size, reranker, system prompt** (2026-03-10)
  - CHUNK_SIZE 1024→512, CHUNK_OVERLAP 128→64, RERANKER_TOP_N 5→8
  - Improved answer_query system prompt for qwen2.5:3b
  - Added timing instrumentation to answer_query
  - Verified: "python package management" query now returns correct answer
- [x] **Simplify watcher to CLAUDE.md-only tracking** (2026-03-09)
  - Removed per-project core docs (README/MEMORY/AGENTS/TODO) from auto-sync
  - `sync.py` v2.0, `watcher.py` v1.8, `tools.py` updated, `test_watcher.py` v2.0 (46 tests)
  - Database wiped via `delete_all_data`, watcher restarted
