# MEMORY.md — MCP Nexus RAG

<!-- Logical state: known bugs, key findings, changelog -->

**Version:** v3.1

## Known Issues

### Medium Priority

- **Raw exception messages exposed to MCP client** (tools.py)
  - Issue: Exception strings may leak internal paths or library versions
  - Recommendation: Log full exception server-side, return sanitized message to client

- **`answer_query` complexity** (C901: 21 > 10)
  - Issue: Function too complex, hard to maintain
  - Recommendation: Extract inner `_fetch_graph` and `_fetch_vector` as module-level helpers

### Low Priority

- **No per-tenant rate limiting**
  - Issue: Single tenant can flood ingestion pipeline
  - Recommendation: In-memory rate limiter keyed by `project_id`

- **tools.py is 1519 lines**
  - Issue: Single file contains all MCP tools
  - Recommendation: Consider splitting into tools/ingest.py, tools/query.py, tools/admin.py

## Key Findings

### Architecture Strengths

- **Clean separation:** config.py, backends/, tools.py, indexes.py, dedup.py, chunking.py
- **Multi-tenant isolation:** `(project_id, tenant_scope)` tuple enforced at Neo4j and Qdrant layers
- **Thread safety:** Double-checked locking in `setup_settings()` and index factories
- **Performance:** Index instance caching (20-50ms saved per call), batch ingestion 10-50x faster

### Security Model

- `ALLOWED_META_KEYS` frozenset prevents Cypher key injection
- Input validation on all entry points via `_validate_ingest_inputs()`
- Fail-open deduplication (availability > consistency)
- No external API calls — all LLM/embed via local Ollama

### Deduplication Design

- Hash: `SHA-256(project_id \x00 scope \x00 text)`
- Same document in different projects/scopes is never treated as duplicate
- `doc_id = content_hash` ensures Qdrant upserts rather than appends

### Qdrant Indexing Behavior

- `indexed_vectors_count=0` is **expected** for collections < `full_scan_threshold` (10,000)
- Qdrant uses linear scan for small collections — faster than HNSW for < 10k points
- HNSW index builds automatically when collection grows past threshold
- Vectors are fully stored and searchable regardless of `indexed_vectors_count`

## Lessons Learned (Post-Fix Documentation)

### 2026-03-02 — RAG token cost optimized (FIXED)

**Root Cause:** Three compounding issues inflated tool response tokens:
1. `MAX_DOCUMENT_SIZE=512KB` — project docs (README, MEMORY, AGENTS; 5–15KB) stored as single nodes; one retrieved node = 15KB = ~3750 tokens
2. `RERANKER_TOP_N=5` — returned 5 full nodes per call; worst case 5 × 15KB = 75KB per call
3. No `max_chars` cap on `get_vector_context`/`get_graph_context` (unlike `answer_query` which has `max_context_chars=6000`)

**Fix Applied:**
- `MAX_DOCUMENT_SIZE`: 512KB → 4KB — all project docs now chunked into 1024-char pieces on ingest
- Added `max_chars: int = 3000` parameter to both retrieval tools (hard cap ~750 tokens per call)
- `.mcp.json`: `RERANKER_TOP_N=2`, `RERANKER_CANDIDATE_K=10` — returns 2 best chunks, not 5
- `test_chunking.py`: updated to use `MAX_DOCUMENT_SIZE` from config (was hardcoded to 512KB)

**Token reduction per call:** ~10,000 tokens worst-case → ~750 tokens (~87% reduction)

**Prevention Guideline:** When adding retrieval tools, always add a `max_chars` cap parameter. Never let raw document nodes flow to Claude's context without a size guard.

> **Rule:** `MAX_DOCUMENT_SIZE` should be ≤ 2× the expected chunk size for your target documents. For markdown docs (5–15KB), 4KB is appropriate. For large code files, increase to 16–32KB.

### 2026-03-02 — Reranker import path wrong (FIXED)

**Root Cause:** `nexus/reranker.py` imported from `llama_index.postprocessor.flag_reranker` (non-existent module). Correct module is `llama_index.postprocessor.flag_embedding_reranker`. The wrong path caused `ModuleNotFoundError` on every reranker load, silently degrading to un-reranked results with a WARNING in logs.

**Fix Applied:** Updated both `TYPE_CHECKING` guard and runtime import in `reranker.py`. Updated all 6 `sys.modules` patch keys in `tests/test_reranker.py`.

**Prevention Guideline:** When adding a new llama-index postprocessor, always verify the exact import path via `poetry show llama-index-postprocessor-flag-embedding-reranker` and `python -c "import llama_index.postprocessor.flag_embedding_reranker"`.

> **Guideline:** Test the reranker load explicitly: `poetry run python -c "from nexus.reranker import get_reranker; get_reranker()"` — this catches import errors before prod.

### 2026-03-02 — Redis cache never called (FIXED)

**Root Cause:** `cache_module` was imported in `tools.py` (`from nexus import cache as cache_module`) but `cache_module.get_cached()` and `cache_module.set_cached()` were never invoked in `get_vector_context`, `get_graph_context`, or `answer_query`. All RAG queries hit Qdrant/Neo4j on every call.

**Fix Applied:** Added cache check at function entry and cache write at function exit in all three retrieval functions. `answer_query` uses `f"answer:{query}"` as cache key for namespace separation.

**Prevention Guideline:** When adding a cache module, always verify integration with a live cache hit test: call the same query twice and confirm the second call does NOT hit the backend (check logs for "cache hit").

> **Guideline:** After integrating cache into any function, run the `disable_cache` autouse fixture pattern in tests: monkeypatch both `get_cached → None` and `set_cached → noop` in `nexus.tools.cache_module`.

### 2026-03-02 — FlagEmbedding incompatible with transformers 5.x (FIXED)

**Root Cause:** `FlagEmbedding 1.3.5` imports `is_torch_fx_available` from `transformers.utils.import_utils`. This symbol was removed in `transformers 5.0`. Installing `FlagEmbedding` without a version pin pulled `transformers 5.2.0` which caused `ImportError` on reranker load.

**Fix Applied:** `poetry run pip install "transformers<5.0"` → installed 4.57.6. Pinned in `pyproject.toml`: `FlagEmbedding>=1.3.5,<2.0.0` and `transformers>=4.40.0,<5.0.0`.

**Prevention Guideline:** Always pin `transformers<5.0` when using FlagEmbedding. Check `poetry show FlagEmbedding` for required transformers version before upgrading.

> **Guideline:** When adding ML/HuggingFace dependencies, immediately pin upper bounds on transformers and torch to prevent silent breakage from upstream API removals.

### 2026-03-01 — Late imports anti-pattern (FIXED)

**Root Cause:** `import httpx` placed inside function body instead of module level, breaking static analysis and IDE support.

**Fix Applied:** Moved httpx import to module level (line 14). Removed duplicate imports from `answer_query()` and `health_check()`.

**Prevention Guideline:** All imports at module level. Only use late imports for optional heavy dependencies with clear fallback.

> **Lint Rule:** ruff E402 catches imports not at top of file.

### 2026-03-01 — Mutable default argument (FIXED)

**Root Cause:** `include_extensions: list[str] = [...]` creates a single list object shared across all calls.

**Fix Applied:** Changed to `Optional[list[str]] = None` with explicit copy inside function body.

**Prevention Guideline:** Never use mutable objects (list, dict, set) as default arguments. Use `None` and create inside function.

> **Lint Rule:** ruff B006 catches mutable default arguments.

---

## Changelog

### v3.0 — 2026-03-02

- **FIXED:** Reranker import path `flag_reranker` → `flag_embedding_reranker` (was silently falling back on every query)
- **FIXED:** Redis cache not integrated into retrieval tools (was imported but never called)
- **FIXED:** FlagEmbedding incompatible with transformers 5.x — pinned `transformers<5.0` in pyproject.toml
- **Added:** `autouse=True` disable_cache fixture in conftest.py (test isolation)
- **Updated:** `start-services.sh` v1.1 — Redis check, `--watcher` option
- **Updated:** `install-hooks.sh` v1.1 — `xargs -r` safety fix, UUOC cleanup
- Tests: 197 passed in 2.25s, lint clean

### v2.9 — 2026-03-01

- **FIXED:** Late httpx imports moved to module level (tools.py:14)
- **FIXED:** Mutable default argument in `ingest_project_directory`
- **Added:** `DEFAULT_INCLUDE_EXTENSIONS` constant for code clarity
- Tests: 197 passed in 2.25s

### v2.8 — 2026-03-01

- **RAG Reset & Rebuild:** Full reset of Neo4j (21,355 nodes) and Qdrant stores
- **Core Documentation Focus:** Removed SKILL/CHAT scopes, now ingests only:
  - Project files: README.md, MEMORY.md, AGENTS.md, TODO.md
  - Persona files: CLAUDE.md, mission.md, .claude/persona/GEMINI.md
- **New module: nexus/sync.py** — Pattern-based file synchronization
  - `get_core_doc_files()` — Scan workspace for core docs
  - `check_file_changed()` — Content-hash based change detection
  - `get_files_needing_sync()` — Return files needing re-ingestion
  - `delete_stale_files()` — Remove docs for deleted files
- **New MCP tools:**
  - `sync_project_files` — Sync core documentation with dedup
  - `list_core_doc_files` — List files that would be synced
  - `cache_stats` — Redis cache statistics
- **Timestamp support:** `_make_metadata()` helper adds `created_at`/`updated_at`
- **Qdrant clarification:** `indexed_vectors_count=0` is expected for collections < 10,000 points (uses efficient linear scan)
- Tests: 197 passed, lint clean

### v2.7 — 2026-03-01

- **Test optimization:** Separated integration tests with `@pytest.mark.integration`
- Default `pytest` runs 197 unit tests in **2.3s** (was 5+ min)
- Integration tests run with: `pytest -m integration`
- Fixed cache reset in `test_fallback_creates_empty_index_when_from_existing_fails`
- Auto-fix: 11 files reformatted by ruff

### v2.6 — 2026-02-28

- **BUGFIX:** Critical silent data loss in batch ingest (`file_path` NameError)
- Tests: 197 passing (11 previously failing now fixed), 83% coverage
- Grade: A+ (Production Ready)

### v2.5 — 2026-02-28

- `ingest_project_directory` — recursive codebase ingestion
- `sync_deleted_files` — remove stale entries
- `delete_all_data` — full database wipe
- `file_path` metadata field on all ingest tools

### v2.3 — 2026-02-28

- `print_all_stats` MCP tool — ASCII table of all projects, scopes, doc counts
- Code-Graph-RAG integration documentation

### v2.2 — 2026-02-28

- Auto-chunking for oversized documents via LlamaIndex SentenceSplitter
- `auto_chunk` parameter (default: True) on all ingest tools

### v2.0 — 2026-02-28

- bge-reranker-v2-m3 cross-encoder reranking
- Two-stage retrieval: candidate pool → reranked top-k
- Configurable `RERANKER_TOP_N`, `RERANKER_CANDIDATE_K`, `RERANKER_ENABLED`
- Graceful fallback on reranker errors

### v1.6 — Initial Architecture

- Nexus RAG MCP: FastMCP wrapper around llama_index PropertyGraphIndex
- Multi-tenant isolation via `project_id + tenant_scope` metadata
- Neo4j GraphRAG + Qdrant VectorRAG dual-engine design
