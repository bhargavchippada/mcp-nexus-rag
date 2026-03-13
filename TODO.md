# TODO.md — MCP Nexus RAG

<!-- Pending tasks: [ ] incomplete -->

**Version:** v5.9

## SurrealDB Migration (Phase 4 — After Phases 1-3 Stable)

> **Workspace artifact:** `artifacts/2026-03-11/ANTIGRAVITY_ARCHITECTURE_surrealdb_migration_plan.md`
> **Goal:** Replace Memgraph (7689) + pgvector (5432) with SurrealDB 3.0 unified graph+vector store
> **Estimated effort:** 3-5 sessions
> **Prerequisite:** Split tools.py first (P0 below) — cleaner migration surface

### Infrastructure
- [ ] Add `surrealdb[pydantic]` to `pyproject.toml`, remove `neo4j` and `psycopg2-binary`
- [ ] Create `nexus/surreal_store.py` — Unified graph+vector store (replaces graph_store.py + vector_store.py)
- [ ] Create `scripts/surrealdb-schema.surql` — document table (768-dim HNSW), entity table, relates_to/extracted_from relations
- [ ] Add `SURREAL_*` env vars to config, remove PG/Memgraph config

### Migration
- [ ] Migrate pgvector embeddings → SurrealDB `document` table with HNSW DIMENSION 768 DIST COSINE
- [ ] Migrate Memgraph entities → SurrealDB `entity` table
- [ ] Migrate Memgraph RELATES_TO edges → SurrealDB `relates_to` RELATION
- [ ] Add `extracted_from` RELATION (entity → document) for provenance
- [ ] Implement native hybrid search (vector KNN + BM25 FTS with RRF fusion)
- [ ] Rewrite `nexus/watcher.py` to ingest into SurrealDB
- [ ] Rewrite `nexus/sync.py` for SurrealDB dedup detection
- [ ] Simplify `nexus/cache.py` — no more cross-store invalidation (graph+vector always consistent)

### MCP Interface (MUST remain unchanged)
- [ ] `answer_query()` — same signature, same response format
- [ ] `get_graph_context()` — graph traversal via SurrealQL arrow syntax
- [ ] `get_vector_context()` — KNN via SurrealQL `<|K|>` operator
- [ ] `ingest_graph_document()` / `ingest_vector_document()` — same API, unified backend
- [ ] All downstream consumers (gravity-claw, mission-control) unaffected

### Validation
- [ ] Vector search recall ≥ 95% of pgvector baseline (measured on test query set)
- [ ] Graph traversal latency < 200ms for 3-hop queries
- [ ] Dedup check performance parity
- [ ] Run full 433-test suite against SurrealDB backend

### Cleanup (after 2-week validation)
- [ ] Remove `nexus/graph_store.py` and `nexus/vector_store.py`
- [ ] Remove LlamaIndex PropertyGraphIndex/VectorStoreIndex dependencies
- [ ] Update `scripts/start-services.sh` — remove Memgraph/pgvector startup
- [ ] Decommission Memgraph RAG (7689) and PostgreSQL (5432) Docker containers

## Pending

### In Progress

- [x] Verify reranker health alert source, add Mission Control reranker health visibility, and move hidden Gravity Claw check-ins into persisted cron state (2026-03-11)

### P0 Priority — Immediate (Code Review 2026-03-09)

- [ ] **Split tools.py (2071 lines)** into modular structure (DO THIS BEFORE SurrealDB migration)
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
- [x] [MED] Expand `PERSONA_FILES` in `sync.py` — now tracks CLAUDE.md, README.md, MEMORY.md, AGENTS.md, TODO.md at workspace + per-project (2026-03-11)
- [x] [HIGH] Add performance metrics for ingestion + query tracking — `nexus/metrics.py` v1.0 + JSONL log + 16 tests (2026-03-11)
- [x] [HIGH] Revert watcher to CLAUDE.md-only — expanded 5-file tracking caused constant re-indexing (2026-03-11)
- [x] [HIGH] Fix Memgraph vector index dimension mismatch — pre-create 768-dim index after volume wipe (2026-03-11)
- [ ] [HIGH] Add 768-dim vector index creation to `start-services.sh` bootstrap — prevents dimension mismatch after volume wipe
- [ ] [MED] Gravity-claw heartbeat: switch to compact JSON to reduce chunk count from 11-15 to 1-2
- [ ] [MED] Add Grafana/dashboard for metrics visualization — parse `metrics/performance.jsonl` for trend analysis
- [ ] [LOW] Add metrics alerting — detect ingestion chunks >10s or query latency >30s as anomalies

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
- [x] **Query latency optimization: 50% faster queries, 85% faster retrieval** (2026-03-10)
  - Bypassed LLMSynonymRetriever in graph retrieval (1137ms → ~180ms)
  - Added `num_ctx: 4096` to Ollama API payload (was defaulting to 32768)
  - Persistent httpx client for Ollama calls (eliminates per-call TCP overhead)
  - Graph ingestion: `SimpleLLMPathExtractor(max_paths=5, num_workers=1)` — 27% faster
  - Context window 8192 → 4096 (reduces KV cache VRAM)
  - Query avg: ~1100ms → ~553ms, retrieval: ~1200ms → 183ms, cached: 3ms
- [x] **RAG answer quality optimization: 83% pass rate** (2026-03-10)
  - `_clean_graph_passage()` strips noisy knowledge triples
  - Vector-first dedup ordering, simplified prompt, removed short answer rejection
  - 30s graph timeout in answer_query prevents cascade
- [x] **Performance optimization: chunk size, reranker, system prompt** (2026-03-10)
  - CHUNK_SIZE 512→384, CHUNK_OVERLAP 64→192 (50% overlap), RERANKER_TOP_N 8→5
  - Improved answer_query system prompt for qwen2.5:3b
  - Added timing instrumentation to answer_query
  - Verified: "python package management" query now returns correct answer
- [x] **Simplify watcher to CLAUDE.md-only tracking** (2026-03-09)
  - Removed per-project core docs (README/MEMORY/AGENTS/TODO) from auto-sync
  - `sync.py` v2.0, `watcher.py` v1.8, `tools.py` updated, `test_watcher.py` v2.0 (46 tests)
  - Database wiped via `delete_all_data`, watcher restarted
