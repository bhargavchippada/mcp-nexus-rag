# TODO.md — MCP Nexus RAG

<!-- Pending tasks: [ ] incomplete, [x] completed -->

**Version:** v1.4

## Hardening

- [ ] Exception message sanitization (return generic errors to client)
- [x] Move httpx import to module level (fixed 2026-03-01)
- [x] Fix mutable default argument in `ingest_project_directory` (fixed 2026-03-01)
- [ ] Per-tenant rate limiting (optional)

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

- [ ] [MED] Refactor `answer_query` — complexity 21 > 10 (ruff C901)
- [ ] [LOW] Consider splitting tools.py (1519 lines) into logical modules

## Features

- [ ] Structured JSONL logging
- [ ] Export/import tenant data tools
- [ ] Production config validation (fail fast on unsafe defaults)
- [ ] [MED] Cache invalidation on ingest — bust cache when new docs added for same `(project_id, scope)`
- [ ] [LOW] Cache hit rate monitoring — track cache hits/misses in Redis (e.g., counter key)
