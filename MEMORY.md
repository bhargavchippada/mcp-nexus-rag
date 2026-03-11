# MEMORY.md — MCP Nexus RAG

<!-- Logical state: known bugs, key findings, lessons learned -->

**Version:** v6.21

## Project Status

| Metric | Value |
|--------|-------|
| **Tests** | 436 passed (448 total, 12 deselected) |
| **Coverage** | ~83% |
| **Status** | Production-ready |
| **Last Updated** | 2026-03-10 |

---

## Known Issues

### High Priority

- **tools.py is 2071 lines**
  - Issue: Single file contains all MCP tools (ingest, query, admin, sync)
  - Impact: Hard to navigate, maintain, and review
  - Recommendation: Split into modular structure:
    - `tools/ingest.py` — ingestion tools (single/batch, graph/vector)
    - `tools/query.py` — retrieval tools (graph context, vector context, answer_query)
    - `tools/admin.py` — admin tools (delete, stats, health check)
    - `tools/sync.py` — sync tools (sync_project_files, sync_deleted_files)
  - Estimated effort: 4-6 hours
  - **Reference:** Code review 2026-03-09 (artifact: `artifacts/code_review_2026-03-09.md`)

### Medium Priority

- **No per-tenant rate limiting**
  - Issue: Single tenant can flood ingestion pipeline, causing Ollama VRAM exhaustion
  - Recommendation: Add in-memory rate limiter keyed by `(project_id, scope)`:
    ```python
    _rate_limits = defaultdict(list)  # (project_id, scope) → [timestamps]
    RATE_LIMIT = 10  # requests per minute
    ```
  - Estimated effort: 1-2 hours
  - **Reference:** Code review 2026-03-09

- **Missing watcher auto-restart for daemon mode**
  - Issue: If `nexus.watcher` dies (OOM, segfault), it stays dead until manual restart
  - Current state: `start-services.sh` has liveness guard (checks log freshness), but no auto-restart
  - Recommendation: Add supervisor loop with exponential backoff
  - Estimated effort: 2-3 hours
  - **Reference:** Code review 2026-03-09

### Low Priority

- **`neo4j` Python driver deprecation warning** — still present because `llama-index-graph-stores-memgraph` uses `neo4j` driver internally. No functional impact.

- **Context window not documented in config.py**
  - Issue: `DEFAULT_CONTEXT_WINDOW = 8192` (reduced from 32768) lacks comment explaining trade-off
  - Recommendation: Add comment about VRAM savings vs. context length

- **Batch ingest returns different type than single ingest**
  - Single: Returns `str` ("Successfully ingested X chunks...")
  - Batch: Returns `dict` (`{'ingested': N, 'skipped': M, 'errors': K}`)
  - Impact: Inconsistent API surface
  - Recommendation: Standardize (either keep as-is or add `status` field to single ingest)

- **HTTP server CORS allows all origins**
  - Location: `http_server.py` line 124
  - Current: `allow_origins=["*"]` with `allow_credentials=False` (correct per CORS spec)
  - Recommendation: In production, restrict to specific origins via env var

## Key Facts

### Multiple server.py Instances Are Normal
Each MCP client (Claude Code session, Gemini CLI, desktop app) spawns its own `server.py` via stdio. 3-4 concurrent instances is expected when multiple Claude Code sessions are open. These are NOT duplicates — each serves a different client. Do NOT kill them.

### Docker Ollama Is the Intended Runtime
Ollama runs via Docker (`docker-compose.yml` service `turiya-ollama`). Key docker-compose settings: `OLLAMA_MAX_LOADED_MODELS=2` (embed + LLM both loaded). Context window is controlled by `DEFAULT_CONTEXT_WINDOW` in `nexus/config.py` (8192 tokens) — this is what LlamaIndex sends as `num_ctx` to Ollama. The env var on Ollama container has no effect; the client controls context size.

### MCP SSE Server (port 8765) — For Docker Consumers
Gravity-claw (Docker container) connects to Nexus RAG via MCP SSE transport on port 8765. Started via `start-services.sh --mcp-sse`. Uses `mcp.settings.host/port` + `mcp.run(transport='sse', mount_path='/')`. This is separate from the HTTP REST API (port 8766, for mission-control) and the stdio MCP server (for Claude Code/Gemini CLI). If gravity-claw shows "SSE error: Non-200 status code (404)", the MCP SSE server isn't running.

### Graph Context Retrieval Can Hang
`PropertyGraphIndex.aretrieve()` makes multiple sequential Ollama LLM calls (NL->Cypher, then Neo4j, then synthesis). If the GPU is saturated (e.g., other LLM servers running), these calls hang indefinitely. The HTTP API (`http_server.py` v2.2) wraps all retrieval in `asyncio.wait_for()` -- 60s per retrieval, 120s for synthesis. Additionally, `answer_query` in `tools.py` v6.1 has its own 30s timeout on graph retrieval — if graph hangs, it falls back to vector-only context. If graph times out, vector results still return.

### RAG Answer Quality Optimization (2026-03-10)
**Problem:** `answer_query` returned false negatives — e.g., "no explicit mention of python package management" when CLAUDE.md clearly states "poetry for Python packages".

**Root Causes (5 identified and fixed):**
1. **Noisy graph passages:** LlamaIndex PropertyGraphIndex extracts knowledge triples (`X -> Y -> Z`) that overwhelm the 3B model. Fix: `_clean_graph_passage()` strips triple-format lines and preamble.
2. **Graph-first context ordering:** Graph triples were listed before cleaner vector text, pushing useful content down. Fix: Vector-first dedup in `_dedup_cross_source()`.
3. **Overly complex prompt:** Extract-then-generate two-step prompt confused qwen2.5:3b. Fix: Simplified to direct Q&A prompt.
4. **Short answer rejection:** `len(answer) < 10` check rejected valid short answers like "poetry" (6 chars). Fix: Only reject truly empty responses.
5. **Graph retrieval timeouts cascading:** Graph retrieval hanging 60+s consumed the 90s synthesis timeout budget. Fix: 30s graph timeout in answer_query, increased HTTP synthesis timeout to 120s.

**Tuning changes (config.py v4.2):**
- `DEFAULT_CHUNK_SIZE`: 512 → 384 tokens (finer granularity)
- `DEFAULT_CHUNK_OVERLAP`: 64 → 192 tokens (50% overlap, research-backed for nomic-embed-text)
- `DEFAULT_RERANKER_TOP_N`: 8 → 5 (fewer focused passages for 3B model)

**Benchmark results:** 5/6 queries pass (83%), avg 32s response time. One remaining failure ("what is the tech stack") is a qwen2.5:3b limitation — it has the answer in context but can't synthesize from dense markdown tables.

### Orphan Nodes and Duplicates -- Root Causes and Fixes
- **Unscoped entity nodes:** LlamaIndex's `PropertyGraphIndex.insert()` creates entity nodes during LLM extraction without propagating tenant metadata (project_id, tenant_scope). Fix: `backfill_all_unscoped()` in `neo4j.py` v2.4 runs after every graph insert -- tags ALL nodes with `project_id IS NULL`.
- **Duplicate content_hash entries:** When one store (Neo4j/Qdrant) has a doc but the other doesn't (partial failure), `check_file_changed()` triggered re-ingest into BOTH stores. Fix: `check_file_sync_status()` in `sync.py` v1.5 returns per-store needs, watcher v1.5 only ingests into the store that's missing the doc.
- **Whole-file vs per-chunk hash mismatch (FIXED v2.1):** `check_file_sync_status()` computed a whole-file hash but `is_duplicate()` searched per-chunk `content_hash` — these never match, so EVERY sync call re-ingested, creating massive duplicates (294 graph chunks, 46 vector points from one file). Fix: Added `file_content_hash` metadata field stored on every chunk during ingestion, and new `is_file_content_duplicate()` backend functions in both `neo4j.py` v2.5 and `qdrant.py` v2.6.
- **Watcher/sync race condition (FIXED v2.1):** Both watcher and manual `sync_project_files` could attempt to sync the same file concurrently, creating duplicates. Fix: Per-file asyncio locks via `get_sync_lock()` in `sync.py` v2.1. Both watcher (`_sync_changed`) and `sync_project_files` acquire the lock and double-check `check_file_sync_status` after acquisition.

> **Guideline:** After any `PropertyGraphIndex.insert()`, always call `backfill_all_unscoped()`. Never re-ingest into a store that already has the content -- use `check_file_sync_status()` for selective ingest. Always acquire per-file lock before ingesting.

---

## Lessons Learned

### Watcher Had No Initial Sync — Empty Database Root Cause (2026-03-10) (FIXED)

**Root Cause:** `nexus.watcher` only reacted to filesystem change events via watchdog. If tracked files weren't modified after watcher startup, zero events fired and no ingestion occurred. The `data_nexus_rag` table is created by `PGVectorStore` on first ingestion — since ingestion never happened, the table didn't exist.
**Fix Applied:** Added initial sync in `run_watcher()` — calls `get_files_needing_sync()` on startup and ingests any unsynced tracked files before entering the event loop. (`watcher.py` v2.0 → v2.1)
**Prevention Guideline:** Any file-watching daemon that also needs to ensure data consistency must perform a reconciliation/bootstrap pass on startup — don't rely solely on change events.

### PG_TABLE_NAME Double-Prefix Bug (2026-03-10) (FIXED)

**Root Cause:** `PG_TABLE_NAME = "data_nexus_rag"` in config.py, but LlamaIndex's `PGVectorStore` prepends `data_` to the table name — actual Postgres table was created as `data_data_nexus_rag`. The raw SQL backend (`pgvector.py`) queried `data_nexus_rag` which didn't exist.
**Fix Applied:** Split into two constants: `PG_TABLE_NAME = "nexus_rag"` (passed to LlamaIndex) and `PG_TABLE_NAME_SQL = "data_nexus_rag"` (used in raw SQL). Renamed existing table. Updated backend, tests, and safe_cleanup script.
**Prevention Guideline:** LlamaIndex PGVectorStore always prepends `data_` to `table_name` param. When using raw SQL alongside LlamaIndex, use the derived `PG_TABLE_NAME_SQL` constant, not `PG_TABLE_NAME` directly.

### Memgraph Vector Index Dimension Mismatch (2026-03-10) (FIXED)

**Root Cause:** `MemgraphPropertyGraphStore` auto-creates a vector index with 1536 dimensions (LlamaIndex default), but `nomic-embed-text` produces 768-dim embeddings. All graph chunk ingestions failed with "Vector index property must have the same number of dimensions."
**Fix Applied:** Dropped wrong-dimension index (`DROP INDEX ON :__Entity__(embedding)`) and recreated with 768 dims (`CREATE VECTOR INDEX entity ON :__Entity__(embedding) WITH CONFIG {"dimension": 768, "capacity": 1000}`).
**Prevention Guideline:** After Memgraph container rebuild or first graph ingestion, verify vector index dimension matches embed model: `SHOW INDEX INFO;` in mgconsole. For `nomic-embed-text`, dimension must be 768.

### Stale Docker Volumes from v2.x → v3.0 Migration (2026-03-10)

`mcp-nexus-rag_neo4j_data` and `mcp-nexus-rag_qdrant_data` volumes are orphaned — docker-compose.yml no longer references Neo4j or Qdrant after the Memgraph/pgvector migration. Safe to remove with `docker volume rm`.

### Backend Migration: Neo4j/Qdrant → Memgraph/pgvector (2026-03-10)

**Scope:** Full backend swap — Neo4j → Memgraph (graph), Qdrant → pgvector (vector).

**Key Decisions:**
- **Memgraph** on port 7689 (separate from code-graph-rag on 7688). Uses `neo4j` Python driver internally — no auth needed (empty user/password). `MemgraphPropertyGraphStore` is a drop-in for `Neo4jPropertyGraphStore`.
- **pgvector** reuses existing Postgres container. `PGVectorStore.from_params()` with `embed_dim=768` (nomic-embed-text), HNSW index. Backend module uses raw `psycopg2` SQL for metadata queries (dedup, delete, scope discovery).
- **Table name:** `data_nexus_rag` (config: `PG_TABLE_NAME`). pgvector metadata column is `metadata_` (trailing underscore) in SQLAlchemy model — all SQL queries use `metadata_->>` JSONB accessor.

**Files Changed:**
- `nexus/backends/memgraph.py` (NEW) — graph ops, same Cypher as neo4j.py
- `nexus/backends/pgvector.py` (NEW) — vector ops via psycopg2 SQL (`_query_metadata`, `_execute`, `get_connection`)
- `nexus/backends/neo4j.py` (DELETED), `nexus/backends/qdrant.py` (DELETED)
- `nexus/config.py` v4.0 — new config vars (`DEFAULT_MEMGRAPH_*`, `DEFAULT_PG_*`)
- `nexus/indexes.py` v3.0 — store imports swapped
- `nexus/tools.py` v6.0 — backend imports + health check keys updated
- `docker-compose.yml` — removed neo4j + qdrant, added memgraph-rag
- All test files updated (mock targets, assertions)

**Prevention:** When swapping LlamaIndex stores, the store class is typically drop-in but the backend utility module (dedup, delete, scope queries) needs full rewrite since it uses store-specific APIs.

### Performance Optimization — Chunk Size, Reranker, System Prompt (2026-03-10)

**Problem:** `answer_query("python package management")` returned "context does not contain information" despite CLAUDE.md containing `poetry for Python packages`. Ingestion was slow (~9s for 7 chunks). `answer_query` total latency was high.

**Root Causes:**
1. **Chunk size too large (1024 chars):** Relevant mentions buried in heterogeneous chunks. Semantic similarity scores clustered (0.46-0.51 spread), so relevant content ranked 15th/19 and barely survived reranking.
2. **RERANKER_TOP_N too low (5):** Only 5 of 20 candidates survived — relevant chunk was at position 5/5 (borderline).
3. **System prompt too restrictive:** `qwen2.5:3b` is a small model that gives up easily with "if no information found" instructions.

**Fixes Applied (config.py v3.2, tools.py):**
- `DEFAULT_CHUNK_SIZE`: 1024 → **512** (smaller, more focused chunks)
- `DEFAULT_CHUNK_OVERLAP`: 128 → **64** (proportional reduction)
- `DEFAULT_RERANKER_TOP_N`: 5 → **8** (more candidates survive to LLM)
- System prompt: Changed from restrictive to encouraging — "Look carefully through ALL passages for any relevant information, even partial mentions"
- Added timing instrumentation to `answer_query` (retrieve ms + LLM ms + total)

**Benchmarks (per component):**
- Chunking: 7 chunks in 423ms (trivial)
- Embedding: 5ms per chunk (Ollama nomic-embed-text, negligible)
- Graph insert: **1.3s per chunk** — LLM entity extraction is the bottleneck (PropertyGraphIndex.insert() makes an LLM call per chunk)
- Reranker first load: ~2-3s (model warm-up), subsequent: ~0.7s for 19 candidates
- Qdrant search: 32ms (fast)
- LLM synthesis: ~1-2s (414 tok/s, fast)

**Prevention:** When tuning retrieval quality, adjust chunk size FIRST — it has the largest impact on what the reranker sees. Graph ingestion is inherently slow due to LLM entity extraction; this is by design and cannot be optimized without removing graph features.

### Watcher Dedup Fix — file_content_hash + Per-File Locks (2026-03-10)

**Problem:** Every `sync_project_files` or watcher sync call re-ingested CLAUDE.md, creating massive duplicates (294 Neo4j graph chunks, 46 Qdrant vector points from a single file). Watcher and manual sync could also race, doubling duplicates.

**Root Causes:**
1. `check_file_sync_status()` hashed the whole file content, but `is_duplicate()` searched per-chunk `content_hash` field — these never matched, so sync always reported "changed."
2. No locking between watcher's `_sync_changed` and manual `sync_project_files` — concurrent ingestion of the same file.

**Fixes Applied:**
- `_make_metadata()` (tools.py v5.1): Added `file_content_hash` parameter — stores whole-file SHA-256 on every chunk
- `ingest_graph_document` + `ingest_vector_document`: Compute `file_chash = content_hash(text, project_id, scope)` and pass to `_make_metadata`
- `is_file_content_duplicate()`: New function in both `neo4j.py` v2.5 and `qdrant.py` v2.6 — queries on `file_content_hash` field
- `check_file_sync_status()` (sync.py v2.1): Now uses `is_file_content_duplicate` instead of `is_duplicate`
- `get_sync_lock()` (sync.py v2.1): Per-file asyncio locks prevent concurrent ingestion
- `sync_project_files` (tools.py v5.1): Acquires lock + double-check pattern after lock acquisition
- `_sync_changed` (watcher.py v1.8): Same lock + double-check pattern

**Verification:** After re-ingestion, second `sync_project_files` correctly returns "All core documentation files are up to date."

**Prevention:** Always store whole-file hash alongside per-chunk metadata. Use per-file asyncio locks for any operation that deletes+re-ingests.

### Watcher Has No Initial-Scan — Must `sync_project_files` After DB Wipe (2026-03-10)
- **Root Cause:** Watcher uses inotify (file-change events only). After `delete_all_data`, CLAUDE.md wasn't modified, so watcher never re-ingested it. Stale MISSION_CONTROL data survived from pre-wipe because `delete_all_data` may not have completed atomically across both stores.
- **Fix:** Manually ran `sync_project_files` to re-ingest CLAUDE.md. Manually ran `delete_tenant_data` for stale projects.
- **Prevention:** After any `delete_all_data` or DB wipe, ALWAYS run `sync_project_files` to re-ingest tracked files. The watcher's missing initial-scan mode is tracked in TODO.md.

### Code Review Summary (2026-03-09, Grade: A, 95/100)

**Strengths:** Dual-engine architecture (GraphRAG + Vector RAG), thread-safe singletons with double-checked locking, comprehensive cache invalidation chain, strong tenant isolation, excellent documentation.

**Recommendations Prioritized:**
- **P0:** Split tools.py into modular structure
- **P1:** Add per-tenant rate limiting
- **P2:** Add watcher auto-restart mechanism
- **P3:** Generate OpenAPI docs, add ADRs, upgrade Neo4j driver

### Code Review Rounds Summary (Rounds 2-22, 2026-03-03 to 2026-03-04)

All bugs below were FIXED. 40+ bugs across 22 rounds, 279 -> 462 tests.

| Round | Key Bugs Fixed | Prevention Guideline |
|-------|---------------|---------------------|
| 1 | `n.score=None` crashes `.4f` format; batch ingest missing cache invalidation; `answer_query` cache hit wrongly truncates with `max_context_chars`; fresh results cached TRUNCATED | Guard None on format specifiers. Collect dirty tenant keys and invalidate in bulk. Distinguish input-size vs output-size params. Cache full results, apply caps at retrieval time. |
| 2 | `neo4j_driver()` created new connection pool per call (CRITICAL PERF); empty `project_id` passed through silently; `delete_all_data` never invalidated cache; shared index lock for graph+vector | Use process-level driver singleton via `get_driver()`. Validate all required string params. Any data mutation must invalidate cache. Use per-resource locks. |
| 3 | `scroll_field` None crashes `sorted()` (CRASH); `sync_deleted_files`/`sync_project_files`/`watcher._sync_deleted` -- backend deletes without cache invalidation; HTTP fallback scope hardcoded | Filter None before typed collections. Any delete path must invalidate cache. Scope `""` is "all scopes" sentinel. |
| 4 | `delete_stale_files` only queried Neo4j -- Qdrant orphans never cleaned; missing `reset_graph/vector_index()` | "Scan indexed paths" must union ALL stores. Every singleton needs a `reset_*` for testability. |
| 5 | Batch ingest chunk loops had no per-chunk error handling | Batch paths must fail at same granularity as single-item paths. |
| 6-9 | `invalidate_cache(pid, "")` only cleared `__all__` index; watcher `_sync_changed` cache not invalidated after delete | "Delete all for project" must clear ALL cache variants. Delete-then-reingest must invalidate cache AFTER delete. |
| 10-12 | `sync_project_files` bare `except: pass` swallowed connection errors; cache not invalidated after pre-delete when ingest fails; `"Successfully" in result` false-negative on "Skipped: duplicate" | Never `except: pass` in I/O paths. Use `"Error" not in result` for success checks. |
| 13-18 | No new bugs. Verified: sync.py path handling, qdrant/neo4j backends, E2E edge cases, retrieval/admin tools | All known failure modes handled after 18 rounds. |
| 19 | `ingest_project_directory` silent success on failure; `_parse_context_results` over-broad guard | Validate ingest return strings before updating counters. Anchor sentinel checks to string start/end. |
| 20 | `ingest_project_directory` empty extension matches all files; `ingest_document` silently ignored `text` when `file_path` given | `str.endswith("")` is always True -- validate extension lists. Warn when function has undocumented priority rules. |
| 21 | `answer_query` cached empty LLM responses; missing Ollama retry logic; `_dedup_cross_source` dropped empty passages silently; unbounded `max_context_chars`; batch ingest missing logging | Validate LLM responses before caching. Add retry for external calls. Log dropped items at DEBUG, WARN when ALL dropped. |
| 22 | Invalid retry env config (OLLAMA_RETRY_COUNT=0) could bypass retry; no retries for transient HTTP errors (429/503) | Clamp config values. Retry both transport failures and transient HTTP errors. |

### Watcher Simplified to CLAUDE.md-Only Tracking (2026-03-09)
- **Change:** Removed per-project core docs tracking (README.md, MEMORY.md, AGENTS.md, TODO.md) from watcher + sync. Now only tracks `CLAUDE.md` as agent persona.
- **Files changed:** `sync.py` v2.0 (removed `CORE_DOC_PATTERNS`, `PROJECT_MAPPINGS`, `_project_id_from_path()`), `watcher.py` v1.8, `tools.py` (stale cleanup simplified), `test_watcher.py` v2.0 (46 tests, all pass)
- **Database:** Full wipe via `delete_all_data` — clean slate for CLAUDE.md-only ingestion
- **Rationale:** Per-project core docs were low-signal noise in RAG stores. CLAUDE.md is the only file with durable agent instructions worth indexing.

> **Guideline:** Nexus RAG watcher tracks ONLY `CLAUDE.md`. Per-project documentation is local-only (4-file pattern) and should NOT be ingested into RAG stores.

### Watcher Bugs (2026-03-06 to 2026-03-09)

**Heartbeat stale in dashboard (2026-03-09):**
- Root Cause: `_sync_changed()` blocked the main loop for minutes during multi-file ingestion. Log mtime exceeded 180s threshold. Docker containers can't see host PIDs.
- Fix: Extracted heartbeat into concurrent `asyncio.create_task(_heartbeat_loop())`.
- Prevention: Periodic liveness signals MUST run as independent concurrent tasks. Docker containers cannot see host PIDs -- use file-based liveness (log mtime) as primary.

**Heartbeat log stale under nohup (2026-03-09):**
- Root Cause: Python uses full buffering (8KB) when stdout/stderr redirected to file via `nohup`. 60s heartbeat messages (~30 bytes) never filled buffer, so `mtime` not updated.
- Fix: Added `sys.stderr.flush()` after heartbeat + `PYTHONUNBUFFERED=1` to startup.
- Prevention: Any daemon started via `nohup` with log redirection MUST use `PYTHONUNBUFFERED=1`. Python's full buffering when not connected to a tty makes log files appear stale.

**Watcher processes offline -- root-owned venv (2026-03-06):**
- Root Cause: `~/code-graph-rag/.venv` created by root using `uv` -- turiya cannot access root-owned paths. Nexus watcher died silently with no auto-restart.
- Fix: Recreated venv as turiya. Removed `setsid` from startup (`nohup &` sufficient).
- Prevention: Never create venvs as root in user-owned directories. After service restart, verify watcher log freshness.

### HTTP Server Fixes (2026-03-08 to 2026-03-09)

**Retrieval timeouts (v2.1):**
- Root Cause: `/query` endpoint hung indefinitely when GPU saturated. `PropertyGraphIndex.aretrieve()` makes multiple sequential Ollama LLM calls with no timeout.
- Fix: `asyncio.wait_for()` -- 60s per retrieval, 90s for synthesis.
- Prevention: Any external service call from HTTP endpoint MUST have explicit timeout.

**Refactor (v2.0):**
- Root Cause: `http_query` had cyclomatic complexity 11, response `project_id` mismatch.
- Fix: Extracted `_resolve_scopes()`, `_collect_results()`, `_synthesize()`. Fixed `project_id` to use resolved value.
- Prevention: Keep endpoint handlers thin. Always return resolved values, not raw request values.

**Error check too broad (v1.6):**
- Root Cause: `"Error" not in result` matched document content containing "Error" (e.g., "Error Recovery:").
- Fix: Changed to `result.startswith("Error")`.
- Prevention: Use `startswith("Error")` for error detection in string results.

### Integrity and Path Fixes (2026-03-04)

**Path-format drift and duplicates:**
- Root Cause: Mixed absolute/relative `file_path` metadata across sync/watcher paths caused duplicate accumulation.
- Fix: Canonical workspace-relative path normalization in `sync.py`, `watcher.py`, `tools.py`. `ingest_document` normalizes absolute paths before forwarding.
- Prevention: All file paths in metadata must be workspace-relative. Normalize at ingestion entry point.

**Neo4j dedup query over-broad:**
- Root Cause: Duplicate detection matched all nodes, not just `:Chunk` nodes.
- Fix: Restrict dedup query to `:Chunk` nodes only.

**Integrity cleanup tooling:**
- Added `scripts/safe_cleanup.py` with dry-run/apply mode for Neo4j/Qdrant integrity cleanup.
- Guideline: Run periodic integrity checks for duplicate `content_hash` groups, unscoped graph nodes, watcher liveness, stale Memgraph file paths.

### Cache Architecture Fixes (2026-03-03)

**Cache invalidation was broken (cache.py v1.3):**
- Root Cause: `invalidate_cache()` used hash-prefix pattern scan that could never match full 16-char SHA-256 keys.
- Fix: Added secondary Redis Set index (`nexus:idx:{project_id}:{scope}` -> full cache keys). Both ingest functions call `invalidate_cache` after successful ingest.
- Prevention: Cache invalidation based on prefix-scanning hashed keys is always broken. Use a secondary index.

**Cache key collision between graph and vector tools:**
- Root Cause: No tool type discriminator in cache key. Graph results returned for vector queries with same params.
- Fix: Added `tool_type` parameter to cache key/get/set. Keys now `"{tool_type}:{project_id}:{scope}:{query}"`.
- Prevention: Any new tool using shared cache MUST pass a unique `tool_type` string.

**Cache bypass of max_chars:**
- Root Cause: Cache hits returned unconditionally, bypassing `max_chars` cap. Stale large entries served up to 10.6k tokens vs intended ~375.
- Fix: `_apply_cap()` applied to ALL cache hit return paths. Added `MAX_CONTEXT_CHARS` config constant.
- Prevention: Cache returns must pass through the same output-size guards as fresh results.

### Operational Configuration Fixes (2026-03-03 to 2026-03-09)

**Ollama MAX_LOADED_MODELS must be >= 2 (CRITICAL):**
- With `=1`, embedding calls evict LLM from VRAM, causing 200+s query times from model swapping.
- Fix: `OLLAMA_MAX_LOADED_MODELS=2`. Total VRAM: ~4.9 GB (595 MB + 4.3 GB).

**Docker Compose cleanup (v2.1):**
- Removed stale `OLLAMA_CONTEXT_LENGTH` and reranker model pull. Fixed `MAX_LOADED_MODELS`.
- Prevention: Ollama MUST run via Docker. Always set `OLLAMA_MAX_LOADED_MODELS=2`.

**LLM model switch: llama3.1:8b -> qwen2.5:3b:**
- VRAM: ~1.9 GB (qwen2.5:3b) vs ~4.7 GB (llama3.1:8b-q4_0).

**Shared reranker HTTP microservice (v3.0):**
- Extracted FlagEmbeddingReranker (~2 GB FP16) into `reranker_service.py` on port 8767.
- `RERANKER_MODE=remote` saves ~2 GB VRAM. Score mapping by `_original_index` in metadata.

**MCP consolidation:**
- Removed redundant `fetch_server.py` and `puppeteer` MCP (superseded by `playwright`).
- Added `searxng`, `tavily`, `brave-search` MCP servers.
- Prevention: Before adding custom MCP servers, check if existing npm/pip packages provide same functionality.

### Security and Validation Fixes (2026-03-03)

**Exception sanitization:**
- Root Cause: `return f"Error: {e}"` leaked internal paths/addresses to MCP clients.
- Fix: Log full error via `logger.error()`, return generic client-safe message.
- Prevention: Any `@mcp.tool()` MUST log full exception, return generic string. Never `return f"Error: {e}"`.

**CORS misconfiguration:**
- Root Cause: `allow_credentials=True` + wildcard origin is invalid per CORS spec.
- Fix: Changed to `allow_credentials=False`.
- Prevention: `allow_credentials=True` requires explicit origin allowlists (never `"*"`).

**.env not gitignored:**
- Fix: Added `.env` to `.gitignore`, created `.env.example` with all 22 env vars.
- Prevention: Every project reading `os.environ` MUST have `.env` in `.gitignore` AND `.env.example`.

**Production config validation:**
- Added `validate_config()` in config.py. Warns on unsafe defaults (default passwords, localhost in production).
- Prevention: Call `validate_config()` at startup for all service entry points.

### RAG Retrieval Fixes (2026-03-02 to 2026-03-03)

**Token cost optimized:**
- Root Cause: `MAX_DOCUMENT_SIZE=512KB` + `RERANKER_TOP_N=5` + no `max_chars` cap = ~10,000 tokens/call worst-case.
- Fix: `MAX_DOCUMENT_SIZE` -> 4KB, added `max_chars=3000` parameter, `RERANKER_TOP_N=2`.
- Prevention: Always add `max_chars` cap on retrieval tools. Never let raw document nodes flow to context without size guard.

**Reranker import path wrong:**
- Root Cause: `llama_index.postprocessor.flag_reranker` (non-existent) instead of `flag_embedding_reranker`.
- Prevention: Verify exact import path via `poetry show` before adding llama-index postprocessors.

**Redis cache never called:**
- Root Cause: `cache_module` imported but `get_cached()`/`set_cached()` never invoked in retrieval functions.
- Prevention: After integrating cache, verify with live cache hit test (call twice, confirm second doesn't hit backend).

**FlagEmbedding incompatible with transformers 5.x:**
- Root Cause: `is_torch_fx_available` removed in transformers 5.0.
- Fix: Pinned `transformers>=4.40.0,<5.0.0`.
- Prevention: Always pin `transformers<5.0` when using FlagEmbedding.

**answer_query complexity reduced:**
- Extracted `_fetch_graph_passages`, `_fetch_vector_passages`, `_dedup_cross_source` helpers. Complexity 21 -> ~7.
- Prevention: Inner async closures count toward enclosing function complexity. Extract as module-level helpers.

**scope parameter made optional:**
- Changed `scope: str` -> `scope: str = ""` on both retrieval tools. Empty scope queries all scopes.
- Prevention: Allow cross-scope queries as fallback.

### General Code Quality Guidelines (2026-03-01 to 2026-03-03)

- **Late imports:** All imports at module level. Only late-import optional heavy dependencies. (ruff E402)
- **Mutable defaults:** Never use mutable objects as default arguments. Use `None` + copy. (ruff B006)
- **Chunked ingest result strings:** Must use `"Error" not in result` semantics. All-chunks-failed is an error even when `ingested=0`.
- **Error messages:** Must not expose internal config values, file paths, or service details.
- **asyncio.gather(return_exceptions=True):** Always handle Exception case explicitly.
- **Bare except:** Never `except: pass` silently. Always log at least `logger.warning(str(e))`.

---

## Key Findings

### Architecture Strengths

- **Clean separation:** config.py, backends/, tools.py, indexes.py, dedup.py, chunking.py
- **Multi-tenant isolation:** `(project_id, tenant_scope)` tuple enforced at Memgraph and pgvector layers
- **Thread safety:** Double-checked locking in `setup_settings()` and index factories
- **Performance:** Index instance caching (20-50ms saved per call), batch ingestion 10-50x faster

### Security Model

- `ALLOWED_META_KEYS` frozenset prevents Cypher key injection
- Input validation on all entry points via `_validate_ingest_inputs()`
- Fail-open deduplication (availability > consistency)
- No external API calls -- all LLM/embed via local Ollama

### Deduplication Design

- Hash: `SHA-256(project_id \x00 scope \x00 text)`
- Same document in different projects/scopes is never treated as duplicate
- `doc_id = content_hash` ensures Qdrant upserts rather than appends

### pgvector Indexing Behavior

- Uses HNSW index with 768 dimensions (nomic-embed-text)
- Table name: `data_nexus_rag` (config: `PG_TABLE_NAME`)
- Metadata stored as JSONB in `metadata_` column (trailing underscore — SQLAlchemy convention)
- All backend queries use `psycopg2` with raw SQL (`metadata_->>` JSONB accessor)
- Connection caching via `_conn_cache` dict with auto-reconnect on closed connections
