# TODO.md — MCP Nexus RAG

<!-- Pending tasks: [ ] incomplete, [x] completed -->

**Version:** v1.2

## Hardening

- [ ] Exception message sanitization (return generic errors to client)
- [x] Move httpx import to module level (fixed 2026-03-01)
- [x] Fix mutable default argument in `ingest_project_directory` (fixed 2026-03-01)
- [ ] Per-tenant rate limiting (optional)

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
