# TODO.md — MCP Nexus RAG

<!-- Pending tasks: [ ] incomplete, [x] completed -->

**Version:** v3.7

## Pending

### Hardening

- [ ] Per-tenant rate limiting (optional) — in-memory limiter keyed by `project_id`
- [x] Add integrity-check command to detect duplicate `content_hash` groups in Neo4j/Qdrant and unscoped Neo4j nodes (`project_id`/`tenant_scope` missing) — DONE: `scripts/safe_cleanup.py` (dry-run + apply)
- [ ] Add watcher auto-restart guard for `nexus.watcher` daemon mode (liveness startup guard is done in `start-services.sh` v1.3)

### Performance

- [ ] Async batch parallelism with `asyncio.gather()`

### Refactoring

- [ ] [LOW] Consider splitting tools.py (1700+ lines) into tools/ingest.py, tools/query.py, tools/admin.py
- [ ] [LOW] `http_query` in http_server.py has cyclomatic complexity 11 (threshold 10) — extract scope-result-parsing into helper functions
- [x] [LOW] Chunked ingest returns "Successfully ingested 0 chunks (errors=N)" when all chunks fail — FIXED: now returns "Error: All N chunks failed..." (tools.py v4.1)

### Dependencies (major — manual review required)

- [ ] [LOW] `neo4j` 5.28.3 → 6.1.0 — MAJOR; review changelog for breaking API changes before upgrading
- [ ] [LOW] `redis` 5.3.1 → 7.2.1 — MAJOR; review async client API changes
- [ ] [LOW] `watchdog` 4.0.2 → 6.0.0 — MAJOR; review event handler API changes
- [ ] [LOW] `huggingface-hub` 0.36.2 → 1.5.0 — MAJOR; review download API
- [ ] [LOW] `transformers` 4.57.6 → 5.2.0 — MAJOR; review pipeline API changes
- [ ] [LOW] `marshmallow` 3.26.2 → 4.2.2 — MAJOR; review Schema API changes
- [ ] [LOW] `pytest-cov` 6.3.0 → 7.0.0 — MAJOR; low risk (dev dep)

### Features

- [ ] Structured JSONL logging
- [ ] Export/import tenant data tools (`export_tenant_data`, `import_tenant_data`) — backup/restore
- [x] Cache hit rate monitoring — DONE: added `get_cache_hit_rate()` in cache.py v1.6, integrated into `cache_stats()`
- [ ] Add `deduplicate_tenant_data` admin tool (remove duplicate `content_hash` records per `(project_id, scope)` in both stores)
- [ ] `get_reranker_stats` tool — expose reranker performance metrics (latency, throughput)
- [ ] `compare_retrieval_methods` tool — A/B comparison of graph vs vector retrieval for debugging
- [ ] `search_by_metadata` tool — filter documents by source/file_path without text query

### Code-Graph-RAG Hygiene

- [x] Exclude `.playwright-mcp/*.log`, `.coverage`, and transient `sed*` temp files from Code-Graph indexing filters
- [x] Purge stale Memgraph `File` nodes for missing-on-disk paths, then run clean re-index

---

## Completed Archive (2026-03-02 to 2026-03-04)

> **Summary:** 22 deep code review rounds completed. 26 bugs fixed, ~23 regression tests added (413→432 total currently passing). All findings documented in `MEMORY.md` Lessons Learned section with root causes and prevention guidelines.

**Highlights:**
- Inspection run (2026-03-04): database/watcher/code-graph audit completed
  - Detected duplicate hash groups in both Neo4j and Qdrant
  - Detected 52 unscoped Neo4j chunk nodes
  - Verified watcher sync path works in foreground/live-change test
  - Identified stale/unwanted Memgraph file nodes (`sed*`, `.playwright-mcp/*.log`, `.coverage`)
- Round 22 (2026-03-04): retry hardening + e2e verification (tools.py v4.6, config.py v2.9)
  - Bug 1: Retry config guard — `OLLAMA_RETRY_COUNT` clamped to minimum 1
  - Bug 2: Transient HTTP retry — added retries for `429/500/502/503/504`
  - Added 3 unit tests for transient/non-transient HTTP behavior and retry-count safeguard
  - Verification: `432 passed, 13 deselected` + integration `13 passed`
- Round 21 (2026-03-04): 5 bug fixes + cache hit rate monitoring (tools.py v4.5, config.py v2.8, cache.py v1.6)
  - Bug 1: answer_query cache validation — prevent caching empty/short LLM responses
  - Bug 2: Ollama retry logic — exponential backoff on transient failures
  - Bug 3: _dedup_cross_source logging — warn when ALL passages from a source are empty
  - Bug 4: max_context_chars bounds — clamp to MAX_ANSWER_CONTEXT_LIMIT
  - Bug 5: ingest_document_batches logging — warn when document missing text+file_path
  - Feature: Cache hit rate monitoring with get_cache_hit_rate() + integrated into cache_stats()
- Round 20: Extension validation + ingest_document warnings (tools.py v4.4)
- Rounds 16–19: Final verification passes — sync.py, qdrant.py, neo4j.py, E2E scenarios confirmed correct
- Rounds 10–15: Cache invalidation chain fixes, watcher reliability improvements
- Rounds 1–9: Neo4j driver singleton, batch ingest error handling, orphan detection, scroll_field None handling
- Robustness hardening: Exception sanitization, cache secondary index, answer_query refactor
- Watcher + optimization: RAG sync daemon, token cost reduction (512KB→4KB chunks, max_chars caps)
