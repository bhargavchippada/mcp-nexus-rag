# TODO.md — MCP Nexus RAG

<!-- Pending tasks: [ ] incomplete, [x] completed -->

**Version:** v3.0

## Pending

### Hardening

- [ ] Per-tenant rate limiting (optional) — in-memory limiter keyed by `project_id`

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
- [ ] Export/import tenant data tools
- [ ] [LOW] Cache hit rate monitoring — track cache hits/misses in Redis (e.g., counter key)

---

## Completed (archived — 2026-03-03, loops 16–18)

> Full findings in `MEMORY.md` Lessons Learned (v4.9).

- [x] Deep code review loops 16–18: 0 new bugs; dedup.py, indexes.py, reranker.py, config.py, chunking.py, retrieval/admin tools all verified correct
  - Loop 16: SHA-256 dedup, singleton locks, reset functions — all verified
  - Loop 17: ALLOWED_META_KEYS injection prevention, byte-length chunking — all verified
  - Loop 18: retrieval pipeline, answer_query, admin tools — all verified; 1 LOW inconsistency (get_tenant_stats ValueError vs error string)

## Completed (archived — 2026-03-03, loops 13–15)

> Full findings in `MEMORY.md` Lessons Learned (v4.8).

- [x] Deep code review loops 13–15: 0 new bugs; sync.py, qdrant.py, neo4j.py, E2E edge cases all verified correct

## Completed (archived — 2026-03-03, loops 10–12)

> Full root causes and fixes in `MEMORY.md` Lessons Learned (v4.7).

- [x] Deep code review loops 10–12: 3 bugs fixed, 8 new regression tests (371→379 total), ruff clean
  - Loop 10: sync_project_files bare except swallowed pre-delete errors (L10-1), cache not invalidated after pre-delete (L10-2) + 5 tests (376)
  - Loop 11: cache.py full review — no new bugs, all 9 functions verified correct
  - Loop 12: watcher._sync_changed "Successfully" in result false-negative on "Skipped: duplicate" (L12-4) + 3 tests (379)

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
