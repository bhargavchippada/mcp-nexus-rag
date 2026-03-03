# MEMORY.md — MCP Nexus RAG

<!-- Logical state: known bugs, key findings, changelog -->

**Version:** v3.7

## Known Issues

### Low Priority

- **No per-tenant rate limiting**
  - Issue: Single tenant can flood ingestion pipeline
  - Recommendation: In-memory rate limiter keyed by `project_id`

- **tools.py is 1600+ lines**
  - Issue: Single file contains all MCP tools
  - Recommendation: Consider splitting into tools/ingest.py, tools/query.py, tools/admin.py

## Lessons Learned

### [2026-03-03] Exception Sanitization — Raw Exceptions No Longer Exposed to MCP Clients (FIXED)
**Root Cause:** `except Exception as e: return f"Error: {e}"` in `get_graph_context`, `get_vector_context`, `ingest_graph_document`, `ingest_vector_document`, and `answer_query` returned raw exception strings to MCP clients, potentially leaking internal paths, service addresses, or credentials.
**Fix Applied:** All public tool exception handlers now log the full error via `logger.error(...)` and return a generic client-safe message (e.g., `"Error: Vector context retrieval failed. Check server logs for details."`). Internal detail is preserved in server logs only.
**Prevention Guideline:** Any new `@mcp.tool()` function MUST follow this pattern: log full exception, return a generic string. Never `return f"Error: {e}"` directly from a public tool.

### [2026-03-03] Cache Invalidation on Ingest — Secondary Index Added to cache.py (FIXED)
**Root Cause:** `invalidate_cache()` in `cache.py` used a hash-prefix pattern scan (`nexus:{hash[:8]}*`) which can never match, because cache keys are full 16-char SHA-256 hashes of the complete key string. No existing cache keys could ever be invalidated, and ingest tools never called `invalidate_cache` anyway.
**Fix Applied:** Added secondary Redis Set index: on `set_cached`, `SADD nexus:idx:{project_id}:{scope_sentinel}` → full cache key. On `invalidate_cache(project_id, scope)`, `SMEMBERS` the index set, delete all tracked cache keys plus the "all scopes" index (scope=""), then delete both index keys.  Both `ingest_graph_document` and `ingest_vector_document` now call `cache_module.invalidate_cache(project_id, scope)` after a successful ingest (including chunked ingestion when `ingested > 0`).
**Prevention Guideline:** Cache invalidation based on prefix-scanning hashed keys is always broken. Use a secondary index (Redis Set) to track which keys belong to which tenant, then delete by membership.

### [2026-03-03] answer_query Refactored — Complexity Reduced from 21 to ~7 (FIXED)
**Root Cause:** `answer_query` had two inner closure functions (`_fetch_graph`, `_fetch_vector`) plus dedup loops and prompt building — all inline. ruff C901 reported complexity 21 > 10.
**Fix Applied:** Extracted three module-level helpers: `_fetch_graph_passages(query, project_id, scope, rerank)`, `_fetch_vector_passages(query, project_id, scope, rerank)`, and `_dedup_cross_source(graph_passages, vector_passages)`. The `answer_query` body now calls these via `asyncio.gather()` — complexity is now ~7.
**Prevention Guideline:** Inner async closures count toward the enclosing function's complexity. Extract them as module-level helpers when complexity exceeds 10.

### [2026-03-03] Production Config Validation Added to config.py (NEW)
**Added:** `validate_config()` function in `nexus/config.py` (v2.7). Returns a list of warning strings when unsafe defaults are detected (e.g., default Neo4j password `password123`, localhost URLs in `NEXUS_ENV=production`). Called at server startup in `server.py:main()` — warnings logged at WARNING level. Strict mode activates with `NEXUS_ENV=production`.
**Guideline:** Call `validate_config()` at startup for all new service entry points. Check for `password123` pattern with `# nosec B105` to silence bandit false positives.

### [2026-03-03] Cache Key Collision Between Graph and Vector Context Tools (FIXED)
**Root Cause:** `cache_key()` in `cache.py` used `f"{project_id}:{scope}:{query}"` — no tool type discriminator. When `get_graph_context` was called first with the same `(query, project_id, scope)` triple, its "Graph Context retrieved..." result was stored in Redis. A subsequent `get_vector_context` call with the same arguments got a cache hit and returned the graph result with the wrong label.
**Fix Applied:** Added `tool_type: str = ""` parameter to `cache_key`, `get_cached`, and `set_cached`. Graph calls pass `tool_type="graph"`, vector calls pass `tool_type="vector"`, answer calls pass `tool_type="answer"`. The key format is now `"{tool_type}:{project_id}:{scope}:{query}"`.
**Prevention Guideline:** Any new tool that calls `cache_module.get_cached`/`set_cached` MUST pass a unique `tool_type` string — even if the query prefix already seems unique. Always include a named discriminator in cache keys when multiple tools share the same parameter space.

### [2026-03-03] scope Parameter Made Optional on get_vector_context and get_graph_context (FIXED)
**Root Cause:** `scope` was a required positional parameter with no default. Passing an invalid or unknown scope (e.g. `CORE_DOCS` instead of `PERSONA`) silently returned "No context found" with no guidance. Empty scope was not supported.
**Fix Applied:** Changed `scope: str` → `scope: str = ""` on both `get_vector_context` and `get_graph_context`. When scope is empty, the `tenant_scope` metadata filter is omitted — only `project_id` is applied — so results come from all scopes for that project. Log and result messages display "all scopes" when scope is empty.
**Prevention Guideline:** Optional scoped retrieval is the correct default — always allow cross-scope queries as a fallback so callers can progressively narrow scope rather than getting empty results from a wrong scope name.

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

## Lessons Learned (Post-Fix Documentation)

### 2026-03-03 — Cache bypass of max_chars (FIXED)

**Root Cause:** `get_vector_context` and `get_graph_context` applied the `max_chars` cap to *fresh* retrieval results but returned cache hits **unconditionally**, bypassing the cap entirely. A stale large entry in Redis (stored before `max_chars` was added) would be returned verbatim — up to 10.6k tokens vs the intended ~375 tokens.

**Fix Applied:**
- Added `_apply_cap(text, max_chars)` helper in `tools.py`
- Applied it to **all** cache hit return paths in `get_vector_context`, `get_graph_context`, `answer_query`
- Added `MAX_CONTEXT_CHARS` config constant (env var `MAX_CONTEXT_CHARS`, default `1500` chars ≈ 375 tokens)
- Changed `max_chars` default from hardcoded `3000` to `MAX_CONTEXT_CHARS` (env-var configurable)
- Set `MAX_CONTEXT_CHARS=1500` in `.mcp.json`
- Cleared Redis cache to evict stale large entries
- Added 7 regression tests (`TestApplyCap`, cache-bypass tests)

**Prevention Guideline:** When adding a `max_chars`/`max_tokens` parameter to any cached function, ALWAYS apply the cap AFTER the cache retrieval, not only in the fresh-fetch path. The cache may store old (uncapped) results from before the parameter existed.

> **Rule:** Cache returns must pass through the same output-size guards as fresh results.

## Changelog

### v3.5 — 2026-03-03

- **Fixed:** Cache bypass of `max_chars` in `get_vector_context`, `get_graph_context`, `answer_query`
- **Added:** `_apply_cap()` helper — applied to BOTH fresh results AND cache hits
- **Added:** `MAX_CONTEXT_CHARS` config constant (env var, default 1500 chars ≈ 375 tokens)
- **Updated:** `max_chars` default changed from hardcoded `3000` → `MAX_CONTEXT_CHARS`
- **Updated:** `.mcp.json` — `MAX_CONTEXT_CHARS=1500` added to nexus env
- **Cache:** Cleared Redis (`nexus:*` keys) to evict stale large entries
- Tests: 245 passed (7 new: `TestApplyCap` × 5, cache-bypass × 2), lint clean

### v3.4 — 2026-03-02

- **Added:** `nexus/watcher.py` — background RAG sync daemon using `watchdog`
  - `CoreDocEventHandler` — thread-safe event queue with 3s debounce
  - `_classify_file()` in `sync.py` — path-only classification (works for deleted files)
  - `_sync_changed()` — delete old chunks → re-ingest updated content
  - `_sync_deleted()` — remove RAG documents on file deletion
  - Started via: `poetry run python -m nexus.watcher` or `start-services.sh --rag-sync`
- **Fixed:** `sync.py` — removed deleted `.claude/persona/GEMINI.md` from `PERSONA_FILES`
- **Fixed:** `sync.py` — added `agentic-trader → AGENTIC_TRADER` to `PROJECT_MAPPINGS`
- **Fixed:** `sync_project_files` — now calls `delete_stale_files` after sync to prune removed files
- **Added:** `watchdog>=4.0.0,<5.0.0` to pyproject.toml
- **Added:** `start-services.sh --rag-sync` option (v1.1→v1.2)
- Tests: 238 passed (37 new watcher tests), lint clean

### v3.3 — 2026-03-03

- **http_server.py v1.1:** Added `/scopes` endpoint to expose `get_all_tenant_scopes` MCP tool via HTTP
- Supports optional `?project_id=X` query param to filter scopes by project
- Used by mission-control Nexus Query interface for dynamic scope dropdown

### v3.2 — 2026-03-02

- **RAG Reset & Rebuild:** Full reset of Neo4j + Qdrant volumes; re-ingested 23 core docs with new 4KB chunk threshold
  - Qdrant `nexus_rag` collection created fresh; initial dedup warnings (404) are expected on empty collection
  - `sync_project_files` completed: 23/23 files ingested
- **OPTIMIZED:** Token cost: `MAX_DOCUMENT_SIZE` 512KB→4KB, `max_chars=3000` on retrieval tools, `RERANKER_TOP_N=2`
- Tests: 201 passed in 2.25s, lint clean

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
