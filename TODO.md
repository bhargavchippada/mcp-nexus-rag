# TODO.md — MCP Nexus RAG

<!-- Pending tasks: [ ] incomplete, [x] completed -->

**Version:** v1.8

## Hardening

- [x] Exception message sanitization — generic messages to client, full error in server logs (fixed 2026-03-03)
- [x] Move httpx import to module level (fixed 2026-03-01)
- [x] Fix mutable default argument in `ingest_project_directory` (fixed 2026-03-01)
- [x] Fix `n.score=None` crash in `get_graph/vector_context` — TypeError on `:.4f` format spec (fixed 2026-03-03)
- [x] Fix batch ingest missing `cache_module.invalidate_cache()` — stale cache after batch (fixed 2026-03-03)
- [x] Fix `answer_query` cache hit wrongly applying `_apply_cap(answer, max_context_chars)` (fixed 2026-03-03)
- [x] Fix get_graph/vector_context caching truncated result — `max_chars` became cache-state-dependent (fixed 2026-03-03)
- [ ] Per-tenant rate limiting (optional)

## Completed (2026-03-03 — robustness hardening)

- [x] Fix `invalidate_cache` in cache.py — was broken (hash-prefix scan never matched); now uses secondary Redis Set index (`nexus:idx:{project_id}:{scope}`)
- [x] Wire `cache_module.invalidate_cache(project_id, scope)` into `ingest_graph_document` and `ingest_vector_document` (both single-doc and chunked paths)
- [x] Exception sanitization — return generic messages to MCP clients, log full details server-side (get_vector_context, get_graph_context, ingest tools, answer_query)
- [x] Refactor `answer_query` — extract `_fetch_graph_passages`, `_fetch_vector_passages`, `_dedup_cross_source` to module level; complexity reduced from 21 to ~7 (ruff C901)
- [x] Production config validation — `validate_config()` in config.py, called at server startup; warns on default Neo4j password, localhost URLs in production mode
- [x] 30 new tests added (279 total); ruff clean; cache.py v1.1→v1.2, tools.py v3.2→v3.3, config.py v2.6→v2.7, server.py v1.8→v1.9, test_unit.py v2.1→v2.2

## Completed (2026-03-03 — cache clear, service check, docs, code review)

- [x] Clear Redis nexus:* cache (9 stale keys removed)
- [x] Verify all services healthy: Neo4j, Qdrant, Ollama, Redis, Postgres all Up
- [x] Update README.md v2.7→v2.8: fix test count (197→249), MAX_DOCUMENT_SIZE (512KB→4KB), add MAX_CONTEXT_CHARS env var, fix NEO4J_USERNAME→NEO4J_USER and OLLAMA_BASE_URL→OLLAMA_URL, add 6 missing tools to MCP Tools section, scope now shown as optional, v2.8 changelog
- [x] Update AGENTS.md v1.5→v1.6: fix MCP server count (13→15, add playwright+chrome-devtools), add Redis cache clear command
- [x] Code review: ruff standard=clean, no new bugs found beyond existing Known Issues

## Completed (2026-03-03 — cache collision fix + optional scope)

- [x] Fix cache key collision: add `tool_type` param to `cache_key`, `get_cached`, `set_cached` (graph/vector/answer get distinct keys)
- [x] Make `scope` optional (default `""`) on `get_vector_context` and `get_graph_context` — empty scope queries all scopes for project
- [x] Add 4 regression tests: cache collision, empty-scope filter, scoped filter

## Completed (2026-03-03 — cache bypass fix)

- [x] Fix `max_chars` cache bypass in `get_vector_context`, `get_graph_context`, `answer_query`
- [x] Add `_apply_cap()` helper applied to both cache hits and fresh results
- [x] Add `MAX_CONTEXT_CHARS` config constant (env var, default 1500)
- [x] Set `MAX_CONTEXT_CHARS=1500` in `.mcp.json`
- [x] Clear Redis cache to evict stale large entries
- [x] Add 7 regression tests (`TestApplyCap` + cache-bypass coverage)

## Completed (2026-03-02 — RAG sync watcher)

- [x] Create `nexus/watcher.py` background daemon (watchdog, debounce, thread-safe queue)
- [x] Fix `sync.py` — remove stale GEMINI.md from PERSONA_FILES, add agentic-trader mapping
- [x] Fix `sync.py` — add `_classify_file()` helper for path-only classification
- [x] Fix `sync_project_files` — call `delete_stale_files` after sync
- [x] Add `watchdog>=4.0.0,<5.0.0` to pyproject.toml
- [x] Add `--rag-sync` option to `start-services.sh` v1.2
- [x] Write 37 watcher tests (all passing)

## Completed (2026-03-02 — token optimization)

- [x] Lower `MAX_DOCUMENT_SIZE` from 512KB to 4KB — project docs now chunked on ingest
- [x] Add `max_chars=3000` parameter to `get_vector_context` and `get_graph_context`
- [x] Set `RERANKER_TOP_N=2`, `RERANKER_CANDIDATE_K=10` in `.mcp.json`
- [x] Update `test_chunking.py` to use `MAX_DOCUMENT_SIZE` from config (not hardcoded 512KB)
- [x] RAG reset + re-ingest core docs with new chunk sizes

## Completed (2026-03-02)

- [x] Fix reranker import path `flag_reranker` → `flag_embedding_reranker` (was silently falling back on every query)
- [x] Integrate Redis cache into `get_vector_context`, `get_graph_context`, `answer_query` (was imported but never called)
- [x] Install FlagEmbedding and pin `transformers<5.0` in pyproject.toml (incompatible with transformers 5.x)
- [x] Add `autouse=True` disable_cache fixture in conftest.py (prevents Redis cache pollution in tests)
- [x] Fix `.xml` missing from IGNORE_EXTENSIONS in code-graph-rag (repomix-output.xml was being indexed)
- [x] Add Redis health check and `--watcher` option to `start-services.sh` v1.1
- [x] Fix `xargs` without `-r` in `install-hooks.sh` (empty stdin safety)

## Performance

- [ ] Async batch parallelism with `asyncio.gather()`

## Refactoring

- [x] Refactor `answer_query` — complexity 21 > 10 (fixed 2026-03-03)
- [ ] [LOW] Consider splitting tools.py (1600+ lines) into logical modules

## Features

- [ ] Structured JSONL logging
- [ ] Export/import tenant data tools
- [ ] [LOW] Cache hit rate monitoring — track cache hits/misses in Redis (e.g., counter key)
