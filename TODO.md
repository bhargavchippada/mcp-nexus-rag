# TODO.md — MCP Nexus RAG

<!-- Pending tasks: [ ] incomplete, [x] completed -->

**Version:** v2.5

## Pending

### Hardening

- [ ] Per-tenant rate limiting (optional) — in-memory limiter keyed by `project_id`

### Performance

- [ ] Async batch parallelism with `asyncio.gather()`

### Refactoring

- [ ] [LOW] Consider splitting tools.py (1600+ lines) into tools/ingest.py, tools/query.py, tools/admin.py
- [ ] [LOW] Chunked ingest returns "Successfully ingested 0 chunks (errors=N)" when all chunks fail — misleading; watcher would incorrectly log "synced" for a failed ingest

### Features

- [ ] Structured JSONL logging
- [ ] Export/import tenant data tools
- [ ] [LOW] Cache hit rate monitoring — track cache hits/misses in Redis (e.g., counter key)

---

## Completed (archived — 2026-03-03, rounds 2–9)

> Full root causes and fixes in `MEMORY.md` Lessons Learned + Changelog v3.7/v3.8.

- [x] Deep code review rounds 1–9: 11 bugs fixed, 92 new regression tests (279→371 total), ruff clean
  - Round 1: 4 bugs (n.score crash, batch cache, answer_query cap, truncated cache) + 11 tests (304)
  - Round 2: neo4j singleton driver, empty project_id validation, delete_all_data cache, split index locks + 18 tests (324)
  - Round 3: scroll_field crash, sync_deleted/sync_project cache, watcher._sync_deleted cache, http fallback scope, invalidate_project_cache tool + 15 tests (339)
  - Round 4: orphan detection (Neo4j∪Qdrant), qdrant_backend.get_all_filepaths, reset_graph/vector_index + 18 tests (357)
  - Round 5: batch ingest per-chunk error handling + 6 tests (363)
  - Round 6: invalidate_cache full-project (per-scope indices) + 5 tests (368)
  - Round 7: watcher._sync_changed pre-ingest cache invalidation + 3 tests (371)
  - Rounds 8–9: no new bugs; 17 E2E scenarios verified

## Completed (archived — 2026-03-03, robustness hardening)

> Details in `MEMORY.md` Changelog v3.5/v3.6.

- [x] cache.py invalidate_cache fixed (secondary Redis Set index); cache wired into ingest tools
- [x] Exception sanitization (generic messages to clients, full detail to server logs)
- [x] answer_query refactored (complexity 21→7); production config validation added
- [x] Cache key collision fix (tool_type discriminator); scope optional on retrieval tools
- [x] max_chars cache bypass fix; _apply_cap helper; MAX_CONTEXT_CHARS env var

## Completed (archived — 2026-03-02, watcher + token optimization)

> Details in `MEMORY.md` Changelog v3.0/v3.4.

- [x] nexus/watcher.py RAG sync daemon (watchdog, debounce, thread-safe queue, 37 tests)
- [x] sync.py fixes: GEMINI.md removed, agentic-trader added, _classify_file helper
- [x] Token optimization: MAX_DOCUMENT_SIZE 512KB→4KB, RERANKER_TOP_N=2, max_chars cap
- [x] Reranker import fix; Redis cache integrated; FlagEmbedding pinned; test isolation fixture
