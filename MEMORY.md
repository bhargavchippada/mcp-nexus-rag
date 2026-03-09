# MEMORY.md — MCP Nexus RAG

<!-- Logical state: known bugs, key findings, changelog -->

**Version:** v6.5

## Known Issues

### Low Priority

- **No per-tenant rate limiting**
  - Issue: Single tenant can flood ingestion pipeline
  - Recommendation: In-memory rate limiter keyed by `project_id`

- **tools.py is 1600+ lines**
  - Issue: Single file contains all MCP tools
  - Recommendation: Consider splitting into tools/ingest.py, tools/query.py, tools/admin.py

## Lessons Learned

### [2026-03-08] Shared Reranker HTTP Microservice (v3.0)

**What:** Extracted the FlagEmbeddingReranker (~2 GB FP16) into a shared HTTP microservice (`reranker_service.py` on port 8767). Both `server.py` (MCP stdio) and `http_server.py` (:8766) can use it via `RERANKER_MODE=remote`, saving ~2 GB VRAM.
**Files changed:** `nexus/config.py` (v3.0), `nexus/reranker.py` (v1.3), `reranker_service.py` (new), `scripts/start-services.sh` (v2.0), `tests/test_reranker.py` (v1.3)
**Key decisions:**
- Sync `httpx.Client` (not async) — `postprocess_nodes` is called synchronously; HTTP latency (~50-200ms) negligible vs inference time
- Score mapping by `_original_index` in metadata avoids fragile text matching
- `RERANKER_MODE=local` (default) = zero behavior change, all 452 tests pass unchanged
- No new dependencies — httpx, FastAPI, uvicorn already in poetry deps
**Test count:** 452 (37 in test_reranker.py, up from 18 — added 10 RemoteReranker + mode switch tests)

### [2026-03-08] LLM Model Switch: llama3.1:8b-instruct-q4_0 → qwen2.5:3b

**What:** Replaced LLM model for triplet extraction and answer synthesis.
**Changes:** `docker-compose.yml` ollama-init model list, `config.py` DEFAULT_LLM_MODEL. Redis cache invalidated.
**VRAM:** ~1.9 GB (qwen2.5:3b) vs ~4.7 GB (llama3.1:8b-q4_0). Auto-unloads after 10m idle.

### [2026-03-08] Full Docker Compose Migration — Ollama + Redis (COMPLETED)

**What:** Migrated Ollama and Redis from systemd services to Docker Compose. Switched LLM model from `llama3.1:8b` to quantized `llama3.1:8b-instruct-q4_0` for VRAM savings.
**Changes:**
- `docker-compose.yml`: Added Ollama (GPU passthrough, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KEEP_ALIVE=10m`) and Redis services; ollama-init pulls 3 models
- `config.py`: `DEFAULT_LLM_MODEL` → `llama3.1:8b-instruct-q4_0`
- Disabled systemd `ollama.service` and `redis-server.service` (replaced by Docker)
- NVIDIA Container Toolkit installed for Docker GPU access
- Full RAG reindex: 22/23 tracked files synced (touch-triggered), all 5 projects + AGENT in both Neo4j (2432 nodes) and Qdrant (248 points)

**VRAM impact:** ~4.7 GB for LLM (q4_0) vs ~8 GB (default Q8); models auto-unload after 10m idle.
**Prevention Guidelines:**
- Docker GPU requires `nvidia-container-toolkit` + `nvidia-ctk runtime configure --runtime=docker` + Docker restart
- Ollama in Docker must have `OLLAMA_HOST=0.0.0.0` for inter-container access
- Watcher has no initial scan — must `touch` tracked files to trigger ingestion after data wipe/reindex
- After any Ollama restart, verify watcher connectivity (in-flight ingestion will fail)

### [2026-03-08] fetch_server.py Cleanup + MCP Consolidation (COMPLETED)

**What:** Removed redundant `fetch_server.py` (custom MCP fetch server) and `mcp-nexus-fetch-url` config from `.mcp.json`. Also removed `puppeteer` MCP (superseded by `playwright`).
**Why:** `fetch_server.py` duplicated functionality provided by `fetch` (npm), `searxng` (`web_url_read`), and `tavily` (`tavily-extract`). The file was already deleted on `main` branch.
**Changes:** Removed from `.mcp.json`, cleaned AGENTS.md references, added 3 new web search MCP servers (`searxng`, `tavily`, `brave-search`), updated `start-services.sh` v1.9 with SearXNG container management.
**Prevention Guideline:** Before adding custom MCP servers, check if an existing npm/pip package already provides the same functionality.

### [2026-03-06] Watcher Processes Offline — Root-Owned venv + Stale Logs (FIXED)

**Root Cause 1: Code-Graph Watcher failed to start — root-owned uv venv**
- `~/code-graph-rag/.venv` was created by `root` using `uv`, which symlinked python to `/root/.local/share/uv/python/cpython-3.12.12-linux-x86_64-gnu/bin/python3.12`.
- `setsid .venv/bin/python` in `start-services.sh` failed with "Permission denied" because turiya cannot access root-owned paths.
- **Fix:** Recreated venv as turiya using `uv venv --python python3.12 .venv && uv pip install -e ".[treesitter-full]"`.
- **Fix:** Removed `setsid` from watcher startup in `start-services.sh` v1.5→v1.6 — `nohup &` already handles detachment.

**Root Cause 2: Nexus RAG Watcher died silently**
- Process was running (idle heartbeats in log) but died at ~05:31 with no error message in the log.
- The `nexus.watcher` daemon has no auto-restart or supervisor mechanism.
- **Fix:** Manually restarted. Auto-restart guard is an existing TODO item.

**Prevention Guidelines:**
- Never create venvs as root in user-owned directories — always use turiya's `uv` (at `~/.local/bin/uv`)
- After any service restart (`start-services.sh`), verify watcher log freshness: `stat /tmp/cgr-watcher.log /tmp/rag-sync-watcher.log`
- Consider adding systemd user services or a process supervisor for long-running watchers

### [2026-03-04] Follow-up Verification Round: E2E + Manual Watcher Validation (FIXED)

**Bug F1: `ingest_document` leaked absolute file paths into metadata**
- `ingest_document(file_path='/home/turiya/antigravity/...')` passed the absolute path through to graph/vector ingestion.
- This could reintroduce mixed path formats despite watcher/sync canonicalization.
- **Fix:** normalize workspace-absolute paths to workspace-relative in `ingest_document` before forwarding to backends.

**Bug F2: `safe_cleanup.py` Neo4j dedup query was over-broad**
- Duplicate detection matched all nodes with `(project_id, tenant_scope, content_hash)`, including non-Chunk graph nodes.
- This reported false positives and risked over-deletion.
- **Fix:** restrict Neo4j dedup query and delete to `:Chunk` nodes only.

**Verification (automated + manual):**
- Full unit suite: `437 passed, 13 deselected`
- Integration suite: `13 passed`
- Manual watcher probes:
  - Core-doc change sync completed without integrity drift
  - Absolute path ingest probe no longer creates absolute metadata paths
  - Post-check integrity: duplicate groups `0`, unscoped chunks `0`, absolute paths `0`

### [2026-03-04] Integrity + Watcher Root-Cause Fixes (FIXED)

**Root Cause 1: Path-format drift created duplicate persistence paths**
- `file_path` metadata was mixed (absolute and relative) across sync/watcher paths.
- Pre-delete by filepath could miss previously ingested variants, allowing duplicate accumulation.
- **Fix:** Added canonical workspace-relative path normalization in `nexus/sync.py`, `nexus/watcher.py`, and `nexus/tools.py`.

**Root Cause 2: No explicit integrity cleanup workflow**
- Duplicate hash groups, unscoped chunks, and absolute file paths needed operational cleanup.
- **Fix:** Added `scripts/safe_cleanup.py` with dry-run/apply mode for Neo4j/Qdrant integrity cleanup and file-path normalization.

**Root Cause 3: Watcher startup fragility and code-graph noise ingestion**
- Detached watcher processes were not startup-verified, and code graph indexed transient files.
- **Fix:** `start-services.sh` now uses stable-process confirmation for both watchers.
- **Fix:** Code-Graph filters now exclude `.playwright-mcp/*.log`, `.coverage`, and `sed*` temp files; stale nodes were purged and graph reindexed cleanly.

**Verification Snapshot (2026-03-04):**
- `mcp-nexus-rag` tests: `432 passed, 13 deselected`
- Post-cleanup audit: Neo4j/Qdrant duplicate groups `0`, unscoped Neo4j chunks `0`, absolute paths `0`
- Live watcher probes: both watchers stayed up; noise-file probe returned zero indexed nodes in Memgraph

### [2026-03-04] Database + Watcher + Code-Graph Audit (INSPECTION)

**Finding A: Large duplicate `content_hash` groups in Neo4j and Qdrant**
- Neo4j duplicate hash groups are high across core docs (e.g. `MCP_NEXUS_RAG/CORE_DOCS` has repeated hashes for `README.md`, `MEMORY.md`, `AGENTS.md`, `TODO.md`; some groups count > 20).
- Qdrant also has duplicate hash groups (lower cardinality, mostly x2 per hash in `MCP_NEXUS_RAG/CORE_DOCS`).
- Cross-store mismatch remains significant for chunk-level counts.

**Evidence snapshot (2026-03-04):**
- `MCP_NEXUS_RAG | CORE_DOCS` -> Neo4j chunk nodes: `150`, Qdrant docs: `25`
- Sample duplicated hash: `608ad8729119...` for `README.md:chunk_1_of_9` appears repeatedly in Neo4j.

**Finding B: Unscoped Neo4j chunk nodes exist**
- Query on nodes missing `project_id` or `tenant_scope` returned:
  - labels `['__Node__', 'Chunk']`: `52` nodes
- This is a tenant-isolation integrity concern for graph content.

**Finding C: RAG watcher logic works in foreground, but daemonization is unreliable**
- Foreground smoke test (`timeout ... python -m nexus.watcher`) starts and processes change events correctly.
- Live event test (touch `TODO.md`) showed full sync completion:
  - `Watcher: synced projects/mcp-nexus-rag/TODO.md (MCP_NEXUS_RAG/CORE_DOCS)`
- However, detached/background attempts in this environment frequently exit shortly after startup (no persistent watcher process visible).

**Finding D: Code-Graph-RAG indexing includes stale/unwanted files**
- Memgraph `File` nodes include stale missing-on-disk entries:
  - `projects/mcp-nexus-rag/tests/sed*` and `projects/mission-control/*/sed*`
- Also indexes unwanted log/artifact-like files:
  - `.playwright-mcp/console-*.log`
  - `projects/web-scrapers/.coverage`
- `missing_on_disk` from Memgraph file audit: `8` paths.

> **Guideline:** Add periodic integrity checks for (1) duplicate `content_hash` groups per `(project_id, scope)`, (2) unscoped graph nodes, (3) watcher liveness, and (4) stale/unwanted Memgraph file paths.

### [2026-03-04] Code Review Round 22: Retry Hardening + E2E Verification (FIXED)

**Bug R22-1: Invalid retry env config could break retry loop assumptions (`config.py` v2.9, `tools.py` v4.6)**
`OLLAMA_RETRY_COUNT=0` is an invalid runtime config that can bypass retry iteration and
produce undefined behavior in downstream retry logic.
**Fix:** Clamp `OLLAMA_RETRY_COUNT` to at least 1 in config, and add a local safety guard
in `_call_ollama_with_retry` (`retry_count = max(1, OLLAMA_RETRY_COUNT)`).

**Bug R22-2: No retries for transient Ollama HTTP errors (`tools.py` v4.6)**
Retry logic previously handled only network/connectivity exceptions, not transient HTTP
status failures (e.g. 429/503) that are recoverable.
**Fix:** Retry `HTTPStatusError` for transient statuses (`429, 500, 502, 503, 504`) with
the same exponential backoff path. Non-transient HTTP errors still fail fast.

**Verification:** Full suite pass + integration pass with healthy services:
`432 passed, 13 deselected` (unit/full), `13 passed` (integration).

> **Guideline:** Retry wrappers should handle both transport failures and transient HTTP
> server errors. Always guard retry-loop config values against invalid environment input.

### [2026-03-04] Code Review Round 21: 5 Bugs Fixed + 1 Feature + ~20 Tests (FIXED)

**Bug R21-1: `answer_query` cached empty/short LLM responses (tools.py v4.5)**
Empty or malformed Ollama responses (< 10 chars) were cached for 24 hours. Subsequent
identical queries would return the error from cache instead of retrying the LLM.
**Fix:** Validate answer length before caching. Return error without caching if response
is empty or too short (< 10 chars).

**Bug R21-2: Missing Ollama retry logic (tools.py v4.5, config.py v2.8)**
Single HTTP call to Ollama failed on transient network blips with no recovery.
**Fix:** Added `_call_ollama_with_retry()` helper with exponential backoff. Configurable
via `OLLAMA_RETRY_COUNT` (default 3) and `OLLAMA_RETRY_BASE_DELAY` (default 1.0s).

**Bug R21-3: `_dedup_cross_source` silently dropped empty passages (tools.py v4.5)**
Empty passages from either backend were filtered without logging. If ALL passages from
a source were empty (backend issue), there was no indication in logs.
**Fix:** Log dropped passages at DEBUG level. WARN if ALL passages from a source are empty.

**Bug R21-4: Unbounded `max_context_chars` in `answer_query` (config.py v2.8, tools.py v4.5)**
Callers could pass arbitrarily large `max_context_chars`, potentially exhausting memory
or exceeding LLM token limits.
**Fix:** Added `MAX_ANSWER_CONTEXT_LIMIT` (default 24000) constant. Clamp parameter value
and log warning when clamping occurs.

**Bug R21-5: `ingest_document_batches` missing logging (tools.py v4.5)**
Documents with neither `text` nor `file_path` incremented `file_read_errors` counter but
didn't log any message — made debugging difficult.
**Fix:** Log WARNING with project_id and scope when a document has neither field.

**Feature: Cache hit rate monitoring (cache.py v1.6)**
Added session-level hit/miss counters with thread-safe tracking. New functions:
`get_cache_hit_rate()`, `reset_cache_hit_stats()`. `cache_stats()` now includes hit rate.

> **Guideline:** Always validate LLM responses before caching. Add retry logic for external
> service calls. Log dropped/filtered items at DEBUG; WARN when ALL items are dropped.

### [2026-03-03] Code Review Round 20: 2 Bugs Fixed + 5 Tests (FIXED)

**Bug R20-1: `ingest_project_directory` empty extension matches all files (tools.py v4.4)**
`str.endswith("")` is always `True` in Python. If a caller passed `include_extensions=[""]`
or any list containing empty/whitespace strings, every file in the directory would be
ingested regardless of extension.
**Fix:** Normalise extensions at function entry: strip whitespace, skip empty entries, prefix
with `.` if missing. Return early with error if the normalised list is empty.

**Bug R20-2: `ingest_document` silently ignored `text` when `file_path` also given**
Both parameters being provided was undocumented and produced silent data loss (the
passed `text` was discarded). This was also an inconsistency with `ingest_document_batches`,
which prioritises `text` when both are given.
**Fix:** Log a `WARNING` when both are provided so callers detect the conflict immediately.
`file_path` still takes priority (documented behavior preserved, now made explicit).

> **Guideline:** Python's `str.endswith("")` always returns `True` — validate user-supplied
> extension lists before use. Warn explicitly when a function has undocumented priority rules.

### [2026-03-03] Code Review Round 19: 3 Bugs Fixed (FIXED)

**Bug R19-1: `ingest_project_directory` silent success on ingest failure (tools.py v4.2)**
`count` was incremented unconditionally after calling `ingest_graph_document` and
`ingest_vector_document` — even if both returned `"Error: ..."` strings. The result
message claimed N files ingested when N files actually failed silently.
**Fix:** Capture return values and only increment `count` when neither ingest contains
`"Error"`. Failures are appended to the `errors` list instead.

**Bug R19-2: `_parse_context_results` over-broad "no results" guard (http_server.py v1.9)**
Guard was `"No " in context_str and "context found" in context_str` — substring match
anywhere in the string. If retrieved document content contained those phrases, the
function returned empty results for a real hit.
**Fix:** Changed to `context_str.startswith("No ") and "context found" in context_str`.

**Bug R19-3: `get_all_tenant_scopes` stale variable name (tools.py v4.2)**
`vector_scopes2` renamed to `vector_scopes` for clarity.

> **Guideline:** For directory-ingestion tools, always capture and validate the return
> string before updating counters. Anchor sentinel-value checks to string start/end
> rather than substring-matching when the sentinel and real content could overlap.

### [2026-03-03] project-check: .env not gitignored, .env.example missing (FIXED via project-check)

**Root Cause:** `.gitignore` had no `.env` entry — a committed `.env` would expose `NEO4J_PASSWORD` and other credentials. `.env.example` was absent, leaving new contributors without guidance on required env vars.

**Fix Applied:** Added `.env` to `.gitignore`; created `.env.example` covering all 22 env vars across `config.py`, `cache.py`, and `watcher.py` with safe placeholder values and inline comments.

> **Guideline:** Every project that reads from `os.environ` MUST have `.env` in `.gitignore` AND a matching `.env.example`. Run `comm -23 <(grep -E "^[A-Z_]+=") .env <.env.example)` as part of CI to detect drift.

### [2026-03-03] Bug Fixes: Chunked Ingest All-Fail + get_tenant_stats (FIXED)

**Bug: Chunked ingest all-fail returns "Successfully" (Root Cause)**
Both `ingest_graph_document` and `ingest_vector_document` returned `"Successfully ingested 0 chunks (errors=N)"` when ALL chunks failed. The watcher's `"Error" not in result` check evaluated True → logged "synced" for a completely failed ingest. Only applies when `needs_chunking=True` AND every chunk insert throws.

**Fix:** Added `if ingested == 0 and errors > 0: return "Error: All N chunks failed..."` before the "Successfully..." return in both functions (`tools.py` v4.1). All-skipped (duplicate) with `errors=0` correctly returns "Successfully ingested 0 chunks" — not an error.

**Bug: get_tenant_stats raises ValueError (Root Cause)**
`get_tenant_stats` raised `ValueError` on empty `project_id` — inconsistent with all other tools that return `"Error: ..."` strings. MCP converts exceptions to error responses either way, but consistency prevents surprise in callers that expect string returns.

**Fix:** Changed to `return "Error: project_id must not be empty"`, updated type annotation to `str | dict[str, int]`. Updated existing test that expected `pytest.raises(ValueError)` to assert on the error string (`test_new_features.py` v2.1).

> **Guideline:** Chunked ingest result strings must use `"Error" not in result` semantics. All-chunks-failed is an error even when `ingested=0` — the "Successfully ingested 0" message pattern is only safe for all-duplicate (skipped) cases. Confirm `errors` counter before returning "Successfully".

### [2026-03-03] Deep Code Review Loops 16–18 — No New Bugs (exhaustive final verification)

**Loop 16: dedup.py, indexes.py, reranker.py — verified correct**
SHA-256 content hash uses `\x00` separators (no collision possible even with adversarial inputs). All singletons use double-checked locking. `reset_graph_index`/`reset_vector_index` symmetric with `reset_reranker`. `reranker.reset_reranker()` unlocked — acceptable for test-only use.

**Loop 17: config.py, chunking.py — verified correct**
`ALLOWED_META_KEYS` frozenset prevents Cypher key injection. `needs_chunking` uses byte-length (`.encode("utf-8")`) not char count — correct for `MAX_DOCUMENT_SIZE` in bytes. `int(os.environ.get(...))` crashes clearly at startup on bad env vars — acceptable.

**Loop 18: Retrieval and admin tools in tools.py — verified correct, 1 LOW inconsistency**
`get_vector_context`: cache hit cap, post-retrieval dedup, fail-open reranker all correct. `answer_query`: `asyncio.gather` for concurrent retrieval, `max_context_chars` on prompt (not output). `print_all_stats`: column widths safe (rows non-empty when `all_project_ids` non-empty). `ingest_project_directory`: `.copy()` on default list, `dirs[:]=[]` correct.
**LOW inconsistency (not fixed):** `get_tenant_stats` raises `ValueError` for validation failure instead of returning `"Error: ..."` string like all other tools. MCP framework converts exceptions to error responses either way — practical impact nil.

> **Guideline:** After 18 rounds of deep review (14 bugs fixed total, 279 → 379 tests), all known failure modes are handled. Remaining LOWs are stylistic (`get_tenant_stats` ValueError) or future features (tools.py split, JSONL logging, async parallelism, rate limiting).

### [2026-03-03] Deep Code Review Loops 13–15 — No New Bugs (final verification)

**Loop 13: sync.py — verified correct**
All 4 functions reviewed: `_classify_file` (path-only, no I/O, len==3 guard prevents nested files), `check_file_changed` (AND semantics — self-healing for partial ingests, fail-open on connection error), `get_files_needing_sync` (delegates correctly), `delete_stale_files` (unions both stores, handles absolute/relative paths via Python Path semantics, delete_by_filepath uses the same stored format). No bugs.

**Loop 14: qdrant.py + neo4j.py — verified correct**
`neo4j.get_all_filepaths` doesn't filter empty strings (vs qdrant which does), but `sync_deleted_files` has `if not rel_path: continue` guard. `delete_by_filepath` uses `DETACH DELETE` which leaves entity nodes as orphans, but this is a LlamaIndex design limitation — entity nodes without `project_id`/`content_hash` are not returned in retrieval queries. No actionable bugs.

**Loop 15: E2E edge cases — verified correct**
- `ingest_project_directory` (relative paths) + `sync_deleted_files` form one pipeline; watcher/`sync_project_files` (absolute paths) form another. Python Path handles `base_path / "/abs/path"` by discarding base_path, so both pipelines work correctly in `sync_deleted_files`.
- Concurrent watcher + MCP sync_project_files: idempotent deletes + dedup → safe.
- Partial ingest (graph ok, vector fails): AND semantics in `check_file_changed` → self-heals on next event.
- `delete_stale_files` + watcher concurrent delete: second delete is a no-op.

> **Guideline:** After 15 rounds of deep review (14 bugs fixed total), all known failure modes are handled. The remaining TODOs are low-priority improvements (tools.py splitting, async batch parallelism, JSONL logging). The codebase is production-ready.

### [2026-03-03] Deep Code Review Loops 10–12 — 3 Bugs Fixed (tools.py v4.0, watcher.py v1.3, test_unit.py v3.0, test_watcher.py v1.3)

**Bug L10-1 (MEDIUM): `sync_project_files` — bare `except Exception: pass` on pre-delete silently swallowed connection errors**
**Root Cause:** In `nexus/tools.py sync_project_files`, the inner `try/except` around `neo4j_backend.delete_by_filepath` + `qdrant_backend.delete_by_filepath` used a bare `except Exception: pass` with the comment "Ignore if not exists". A real connection error (e.g., `RuntimeError: connection refused`) was swallowed identically to a "document not found" no-op. This left old chunks alive in both stores, then the ingest ran anyway, creating duplicate content.
**Fix Applied:** Changed to `except Exception as e: logger.warning(...); errors.append(...); continue`. A pre-delete failure now skips the ingest for that file and is reported in the tool result.
**Prevention Guideline:** Never use bare `except: pass` in backend I/O paths. "Not found" is a no-op for delete operations and does NOT raise — backends return silently. Any exception IS a real error. Always log + skip on pre-delete failures.

**Bug L10-2 (MEDIUM): `sync_project_files` — cache not invalidated after pre-delete when ingest fails**
**Root Cause:** Same delete-then-ingest pattern as Bug L7-4 (watcher `_sync_changed`), but in `sync_project_files`. After pre-delete succeeded but ingest returned "Error:", the cache was never invalidated. Queries continued serving stale cached results pointing at data that had just been deleted from the backends.
**Fix Applied:** Added `cache_module.invalidate_cache(f["project_id"], f["scope"])` immediately after the pre-delete try/except block, before the ingest calls.
**Prevention Guideline:** The "delete-then-reingest" pattern occurs in three places: `watcher._sync_changed`, `sync_project_files`, and (implicitly) ingest tools via dedup. All three now invalidate the cache right after deletion. Any future code using this pattern must do the same.

**Bug L12-4 (LOW): `watcher._sync_changed` — `"Successfully" in result` check false-negative on "Skipped: duplicate"**
**Root Cause:** The success check at line 193 in `nexus/watcher.py` used `"Successfully" in graph_result and "Successfully" in vector_result`. If a concurrent MCP tool had just ingested the same content, both ingest functions return "Skipped: duplicate content already exists for project…" — a valid non-error outcome. The watcher treated this as a partial failure and logged a misleading WARNING. The identical bug was fixed in `sync_project_files` in Round 1 but was missed in `_sync_changed`.
**Fix Applied:** Changed to `"Error" not in graph_result and "Error" not in vector_result`, consistent with all other success checks in the codebase.
**Prevention Guideline:** Use `"Error" not in result` (not `"Successfully" in result`) for all ingest success checks. "Successfully" is only one of several valid success messages; other valid outcomes ("Skipped: duplicate") don't contain "Successfully". Apply consistently across ALL code paths that call ingest functions.

### [2026-03-03] Deep Code Review Rounds 6–9 — 2 Bugs Fixed (cache.py v1.5, watcher.py v1.2, test_unit.py v2.9, test_watcher.py v1.2)

**Bug L3-1 (MEDIUM): `invalidate_cache(project_id, scope="")` only cleared `__all__` index**
**Root Cause:** `invalidate_cache` in `nexus/cache.py` treated `scope=""` as "clear all-scopes queries only" but not per-scope queries. After `delete_tenant_data("PROJ", "")`, which calls `invalidate_cache("PROJ", "")`, cached results for `get_graph_context(scope="CORE_DOCS")` or `get_vector_context(scope="CORE_DOCS")` remained in Redis. Subsequent queries returned stale (deleted) data from cache until TTL expired.
**Fix Applied:** When `scope=""`, `invalidate_cache` now scans `nexus:idx:{safe_pid}:*` and clears all per-scope indices in addition to `__all__`.
**Prevention Guideline:** "Delete all for project" operations must invalidate ALL cache variants — both cross-scope and every per-scope index. Verify `invalidate_cache(project_id, "")` clears everything after any data wipe.

**Bug L7-4 (MEDIUM): `_sync_changed` in watcher.py — cache not invalidated after `_delete_from_rag`**
**Root Cause:** `nexus/watcher.py:_sync_changed` called `_delete_from_rag` (which removes all chunks for a file from both stores) but only invalidated the cache inside `ingest_graph_document` / `ingest_vector_document`. If both ingest calls raised an exception or returned an error, the cache retained stale entries for data that had already been deleted — queries would return cached results referencing non-existent documents.
**Fix Applied:** Added `cache_module.invalidate_cache(project_id, scope)` immediately after `_delete_from_rag`, before the ingest calls. This ensures cache is cleared as soon as data is removed, regardless of ingest outcome.
**Prevention Guideline:** Any delete-then-reingest pattern must invalidate the cache AFTER the delete, not only after the re-ingest. Assume the reingest can fail; the cache must not serve data for deleted content.

### [2026-03-03] Loop 9 E2E Scenario Review — 17 Scenarios Verified

All E2E scenarios for watcher + sync + cache interactions verified correct:
- New/update/delete/move (tracked→tracked, untracked→tracked, tracked→untracked)
- Rapid saves (debounce coalesces correctly)
- Partial ingest recovery (check_file_changed uses AND across both stores — heals partial failures)
- delete_tenant_data full-project invalidation (Loop 6 fix: all per-scope indices cleared)
- Concurrent watcher + MCP tool ingest (idempotent upserts: MERGE in Cypher, upsert in Qdrant)
- Cache key invalidation chain (ingest → invalidate → miss on next query → fresh retrieval)

> **Guideline:** After all fixes in rounds 3–9, the codebase correctly handles all known failure modes. The remaining TODOs are cosmetic/low-priority items (0-chunk success message, rate limiting, tools.py splitting).

### [2026-03-03] Deep Code Review Round 5 — 1 Bug Fixed (tools.py v3.9, test_unit.py v2.8)

**Bug L2-1 (MEDIUM): Batch ingest chunk loops had no per-chunk error handling**
**Root Cause:** `ingest_graph_documents_batch` and `ingest_vector_documents_batch` in `nexus/tools.py` had no `try/except` around individual chunk inserts in the batch chunk loop. An exception on chunk N (e.g., Neo4j/Qdrant connection failure mid-batch) would propagate to the outer document-level `try/except`, causing: (1) remaining chunks N+1..end to be silently skipped, (2) error count incremented by 1 for the entire document even though multiple chunks failed, (3) `invalidation_keys` not updated for partially-succeeded documents. The single-doc ingest path already had per-chunk try/except — the batch path was inconsistently missing it.
**Fix Applied:** Added per-chunk `try/except Exception as chunk_err` in both batch chunk loops, symmetric with the single-doc path.
**Prevention Guideline:** When copying logic from a single-item path to a batch path, always verify exception granularity. Batch paths should fail at the same level as single-item paths — typically per-item (or per-chunk for chunked items), not per-batch.

### [2026-03-03] Deep Code Review Round 4 — 2 Bugs Fixed + 2 Enhancements (qdrant.py v2.4, sync.py v1.2, tools.py v3.8, indexes.py v2.3)

**Bug L1-1 (MEDIUM): `delete_stale_files` + `sync_deleted_files` only queried Neo4j — Qdrant-only orphans never cleaned**
**Root Cause:** `nexus/sync.py delete_stale_files()` and `nexus/tools.py sync_deleted_files()` both called `neo4j_backend.get_all_filepaths()` to build the list of indexed paths to check against disk. If a document was partially ingested (e.g., Neo4j ingest succeeded but process crashed before Qdrant ingest, or vice versa), the surviving store's entries became permanent orphans — never detected, never deleted.
**Fix Applied:** Both functions now union Neo4j and Qdrant file path sets (`neo4j_paths | qdrant_paths`). Added `qdrant_backend.get_all_filepaths()` symmetric with the Neo4j counterpart.
**Prevention Guideline:** Any "scan indexed paths" operation must query ALL stores and union results. Never assume one store is the authoritative "source of truth" — partial ingest failures can leave each store in a different state.

**Enhancement L1-2 (LOW): `indexes.py` lacked `reset_graph_index()` / `reset_vector_index()`**
**Root Cause:** `nexus/reranker.py` had `reset_reranker()` for clearing its singleton, but `indexes.py` had no equivalent. Tests that needed a clean index state had to manipulate the private `_*_cache` globals directly — fragile and brittle.
**Fix Applied:** Added `reset_graph_index()` and `reset_vector_index()` using the same lock-protected pattern as `reset_reranker()`.
**Prevention Guideline:** Every module-level singleton with a `get_*` factory should also have a `reset_*` function for testability. Apply symmetrically.

### [2026-03-03] Deep Code Review Round 3 — 5 Bugs Fixed + New Tool (qdrant.py v2.3, tools.py v3.7, watcher.py v1.1, http_server.py v1.8)

**Bug 1 (CRASH): `scroll_field` None values crash `sorted()`**
**Root Cause:** `nexus/backends/qdrant.py scroll_field()` added `record.payload[key]` directly to `set[str]` without checking for `None`. When any record has a `None` payload value, the resulting set contains `None`. Callers like `get_all_tenant_scopes` and `print_all_stats` then call `sorted(graph_scopes | vector_scopes)` which raises `TypeError: '<' not supported between instances of 'str' and 'NoneType'` — a complete crash.
**Fix Applied:** Added `if val is not None: values.add(val)` guard in `scroll_field`.
**Prevention Guideline:** External data can always have unexpected None values. Always filter None before adding to typed collections, and before calling `sorted()` on mixed-type sets.

**Bug 2 (MEDIUM): `sync_deleted_files` no cache invalidation after deletion**
**Root Cause:** The `sync_deleted_files` MCP tool called `neo4j_backend.delete_by_filepath` / `qdrant_backend.delete_by_filepath` directly (bypassing the tool layer), so `cache_module.invalidate_cache` was never called. Cached results for queries served stale content from deleted files.
**Fix Applied:** Added `cache_module.invalidate_cache(project_id, scope)` after the deletion loop when `removed_count > 0`.
**Prevention Guideline:** Any code path that calls backend delete functions directly (not via ingest tools) must explicitly call `cache_module.invalidate_cache` — the tool-layer ingest functions handle this, but raw backend calls do not.

**Bug 3 (MEDIUM): `sync_project_files` stale cleanup no cache invalidation**
**Root Cause:** `sync_project_files` called `sync_module.delete_stale_files()` which removes documents from Neo4j and Qdrant backends but has no knowledge of the Redis cache. The deletion left stale cache entries for the affected project/scope.
**Fix Applied:** After `delete_stale_files` returns non-empty `deleted`, call `cache_module.invalidate_cache(project_id, scope)` for that pair.
**Prevention Guideline:** Same as Bug 2 — anytime data is removed from backends outside the ingest tool layer, the cache must be manually invalidated.

**Bug 4 (MEDIUM): `watcher._sync_deleted` no cache invalidation after backend deletion**
**Root Cause:** `watcher.py _sync_deleted` called `_delete_from_rag()` which deletes directly from Neo4j/Qdrant. After a file is deleted from disk and its RAG entries removed, the Redis cache still held the old retrieval results. Future queries within the TTL window would return content from deleted files.
**Fix Applied:** Added `from nexus import cache as cache_module` import to watcher.py. Added `cache_module.invalidate_cache(project_id, scope)` after each successful `_delete_from_rag` call.
**Prevention Guideline:** All watcher delete paths (file deletions, moves) must mirror the cache invalidation done by the ingest tool layer.

**Bug 5 (LOW): `http_server.py` fallback scope hardcoded to `"CORE_CODE"`**
**Root Cause:** When no scopes were found for a project in the HTTP `/query` endpoint, the code fell back to `["CORE_CODE"]` instead of `[""]`. An empty scope string passed to `get_vector_context`/`get_graph_context` means "query all scopes" — but `"CORE_CODE"` would only match that specific scope, returning no results for projects with different scope names.
**Fix Applied:** Changed fallback from `["CORE_CODE"]` to `[""]` in `http_server.py`.
**Prevention Guideline:** Scope `""` is the "all scopes" sentinel in this system. Never hardcode a specific scope as a fallback — use `""` to let the system search everything.

**New Tool: `invalidate_project_cache`**
Added as `@mcp.tool()` in tools.py. Exposes targeted Redis cache invalidation (by project_id + optional scope) without deleting any backend data. Useful for forcing fresh results after external data modifications or debugging cache issues.

### [2026-03-03] Deep Code Review Round 2 — 4 Bugs Fixed (tools.py v3.6, neo4j.py v2.2, cache.py v1.4, indexes.py v2.2)

**Bug 1 (CRITICAL PERF): `neo4j_driver()` created a new connection pool per call**
**Root Cause:** Every function in `nexus/backends/neo4j.py` called `with neo4j_driver() as driver:`. The `with` statement invokes `driver.__exit__()` which calls `driver.close()` — destroying the entire connection pool on every single query. This caused constant pool setup/teardown overhead at Neo4j ingestion/query rate.
**Fix Applied:** Added `get_driver()` singleton with `_driver_instance`/`_driver_lock` (double-checked locking). All 10 internal functions changed from `with neo4j_driver() as driver: with driver.session() as session:` to `with get_driver().session() as session:`. Updated `health_check` in tools.py from `neo4j_backend.neo4j_driver()` to `neo4j_backend.get_driver()`. Kept `neo4j_driver()` as deprecated backward-compat function.
**Prevention Guideline:** Never use `GraphDatabase.driver()` as a context manager for short-lived calls — it's expensive to create and the `with` block destroys the pool on exit. Use a process-level singleton accessed via `get_driver()`. The Qdrant backend already had this pattern right.

**Bug 2 (MEDIUM): Empty `project_id` silently passed through to Neo4j/Qdrant**
**Root Cause:** `get_graph_context` and `get_vector_context` validated `query` but not `project_id`. An empty string `project_id=""` passed through to Neo4j filters and returned empty results without any error indication — callers had no signal that the request was malformed.
**Fix Applied:** Added `if not project_id or not project_id.strip(): return "Error: 'project_id' must not be empty."` in both functions, symmetric with the query validation already present.
**Prevention Guideline:** Validate all required string parameters at the entry of every public tool. Check both `query` and `project_id` together when both are required.

**Bug 3 (MEDIUM): `delete_all_data` never invalidated Redis cache**
**Root Cause:** `delete_all_data` wiped Neo4j and Qdrant but never called any cache invalidation. After a full wipe, `get_vector_context`/`get_graph_context` could serve stale cached results from before the wipe.
**Fix Applied:** Added `invalidate_all_cache()` to `cache.py` (scans/deletes all `nexus:*` keys via `scan_iter`). Added `cache_module.invalidate_all_cache()` call in `delete_all_data` in tools.py after both backend deletes.
**Prevention Guideline:** Any operation that modifies data must invalidate the cache. `delete_all_data` is more destructive than `delete_tenant_data` — use `invalidate_all_cache()` for global wipes, `invalidate_cache(project_id, scope)` for targeted wipes.

**Bug 4 (LOW): Shared `_index_cache_lock` for both graph and vector index init**
**Root Cause:** `nexus/indexes.py` used a single `_index_cache_lock` for both `get_graph_index()` and `get_vector_index()`. If two coroutines tried to initialise both indexes concurrently, one would block waiting for the other's lock even though the two are fully independent.
**Fix Applied:** Split into `_graph_index_lock` and `_vector_index_lock`.
**Prevention Guideline:** Use per-resource locks, not shared locks, unless there is a true dependency between the resources. Independent singletons need independent locks.

### [2026-03-03] Deep Code Review — 11 Bugs Fixed (v3.5/v1.2/v1.7)

**Bug 1 (HIGH): `delete_tenant_data` missing cache invalidation**
**Root Cause:** `delete_tenant_data` deleted data from Neo4j + Qdrant but never called `cache_module.invalidate_cache()`. Cache entries served stale data after deletion.
**Fix Applied:** Added `cache_module.invalidate_cache(project_id, scope)` unconditionally after both backend delete calls — even on partial failure, stale entries must be purged.
**Prevention Guideline:** Any function that deletes OR modifies tenant data must invalidate the cache. "Write" includes delete. Always invalidate after data mutations, not just ingest.

**Bug 2 (HIGH): Empty query accepted by `get_graph_context` / `get_vector_context`**
**Root Cause:** Both functions had no `query` validation. `answer_query` had it; the query functions didn't. Empty queries caused expensive backend calls with meaningless results.
**Fix Applied:** Added `if not query or not query.strip(): return "Error: 'query' must not be empty."` at the start of both functions.
**Prevention Guideline:** Validate all required string inputs at the entry point of every public tool, not just some. Apply the same validation pattern that `answer_query` uses.

**Bug 3 (HIGH): CORS misconfiguration — `allow_credentials=True` + wildcard origin**
**Root Cause:** `http_server.py` had `allow_origins=["*"]` with `allow_credentials=True`. Per the CORS spec, a wildcard origin with credentials is invalid — browsers reject such responses.
**Fix Applied:** Changed `allow_credentials=True` → `allow_credentials=False`.
**Prevention Guideline:** `allow_credentials=True` requires explicit origin allowlists (never `"*"`). Wildcard origin means `allow_credentials` must be `False`.

**Bug 4 (HIGH): Reranker singleton has no thread lock — race condition on init**
**Root Cause:** `nexus/reranker.py` `get_reranker()` used a bare `if _reranker is None:` check without a lock. Two concurrent coroutines could both pass the check and both call `FlagEmbeddingReranker(...)` — loading the model twice.
**Fix Applied:** Added `_reranker_lock = threading.Lock()` and double-checked locking pattern: outer check → lock → inner check → init.
**Prevention Guideline:** All process-level singletons backed by expensive initialisation must use double-checked locking (`threading.Lock`).

**Bug 5 (MEDIUM): Late imports inside `answer_query` and `_fetch_graph_passages`**
**Root Cause:** `import asyncio` and `from nexus.config import DEFAULT_LLM_MODEL, DEFAULT_LLM_TIMEOUT` were inside the function body. This adds overhead on every call and obscures dependencies.
**Fix Applied:** Moved all late imports to module level in `nexus/tools.py`.
**Prevention Guideline:** Only use function-level imports for optional heavy dependencies (e.g., ML models under `TYPE_CHECKING`). Standard library and project config must import at module top.

**Bug 6 (MEDIUM): Late imports in `http_server.py` endpoint handlers**
**Root Cause:** `from datetime import datetime, timezone` in `http_query`, `import re` in `_parse_context_results`, `http_get_projects`, `http_get_scopes`, `http_query`.
**Fix Applied:** Moved `import re` and `from datetime import datetime, timezone` to module level.

**Bug 7 (MEDIUM): `print_all_stats` silently swallowed Qdrant scope errors**
**Root Cause:** `except Exception: vector_scopes = set()` — no logging, making Qdrant failures invisible.
**Fix Applied:** Changed to `except Exception as e: logger.warning(...)`.
**Prevention Guideline:** Never bare-except silently. Always log at least `logger.warning(str(e))`.

**Bug 8 (MEDIUM): `sync_project_files` counts "Skipped: duplicate" as error**
**Root Cause:** `if "Successfully" in graph_result and "Successfully" in vector_result:` — documents already ingested return "Skipped: duplicate content...", which was counted as an error.
**Fix Applied:** Changed to `if "Error" not in graph_result and "Error" not in vector_result:`.
**Prevention Guideline:** Success-check by absence of error, not presence of success keyword, when multiple valid success messages exist ("Successfully", "Skipped").

**Bug 9 (MEDIUM): `auto_chunk=False` error message leaked internal `MAX_DOCUMENT_SIZE` value**
**Root Cause:** `f"Error: Document exceeds {MAX_DOCUMENT_SIZE // 1024}KB limit..."` exposed the internal config value.
**Fix Applied:** Changed to generic `"Error: Document exceeds size limit. Set auto_chunk=True to split automatically."`.
**Prevention Guideline:** Error messages returned to callers must not expose internal config values, file paths, or service details.

**Bug 10 (MEDIUM): `asyncio.gather(return_exceptions=True)` exceptions silently dropped**
**Root Cause:** Result parsing in `http_query` only checked `isinstance(result, str)` — `Exception` objects in `all_results` were skipped silently.
**Fix Applied:** Added `if isinstance(result, Exception): logger.warning(...); continue` before the string check.
**Prevention Guideline:** When using `return_exceptions=True`, always handle the `Exception` case explicitly. Do not assume all results are the expected type.

**Bug 11 (LOW): Dead `isinstance(result, str)` branches in `http_get_projects` / `http_get_scopes`**
**Root Cause:** Both `get_all_project_ids` and `get_all_tenant_scopes` always return lists. The string-parsing fallback code was dead and added confusion.
**Fix Applied:** Simplified both handlers to `list(result)` directly.

### [2026-03-03] Code Review — 4 Bugs Fixed

**Bug 1: `n.score=None` crashes `:.4f` format (HIGH)**
**Root Cause:** Nodes returned from `PropertyGraphIndex.as_retriever().aretrieve()` can have `score=None` when the reranker fails and falls back to un-reranked nodes. The f-string `{n.score:.4f}` raises `TypeError`.
**Fix Applied:** Changed to `{(n.score if n.score is not None else 0.0):.4f}` in both `get_graph_context` and `get_vector_context`. None scores format as "0.0000".
**Prevention Guideline:** Guard against None when using format specifiers on library-returned values.

**Bug 2: Batch ingest missing cache invalidation (HIGH)**
**Root Cause:** `ingest_graph_documents_batch` and `ingest_vector_documents_batch` never called `cache_module.invalidate_cache()`. Single-doc paths did; batch paths didn't.
**Fix Applied:** Both batch functions now track `set[tuple[str, str]]` of dirty `(project_id, scope)` pairs and call `invalidate_cache` for each at the end.
**Prevention Guideline:** Any function writing new data must invalidate affected cache entries. Collect dirty tenant keys and invalidate in bulk at end of batch loops.

**Bug 3: `answer_query` cache hit wrongly truncates LLM answer with `max_context_chars` (MEDIUM)**
**Root Cause:** `max_context_chars` limits LLM input context, not the output answer. `_apply_cap(cached, max_context_chars)` on cache hits truncated the answer at 6000 chars. Fresh results were returned without truncation.
**Fix Applied:** Cache hits return `cached` directly (no `_apply_cap`). `max_context_chars` only bounds `combined_context` (the prompt input).
**Prevention Guideline:** Clearly distinguish input-size parameters (`max_context_chars`) from output-size parameters (`max_chars`). Never apply an input-size cap to the output.

**Bug 4: Fresh results cached TRUNCATED — `max_chars` became cache-state-dependent (MEDIUM)**
**Root Cause:** `get_graph/vector_context` stored a `max_chars`-truncated result in cache. A subsequent caller with a larger `max_chars` still received the smaller result from cache.
**Fix Applied:** Both functions now cache the FULL untruncated result and apply `_apply_cap(result, max_chars)` at return time for both fresh and cache paths.
**Prevention Guideline:** Cache full/canonical results. Apply output caps at retrieval time. Truncation parameters must be part of the cache key if truncation is applied before caching.

### [2026-03-03] HTTP Server Error Check Too Broad — Zero Results Returned (FIXED)
**Root Cause:** In `http_server.py`, the condition `"Error" not in result` was used to filter out error responses. However, document content containing the word "Error" (e.g., "Error Recovery:", "Error handling") triggered this check, causing valid results to be skipped and returning 0 results.
**Fix Applied:** Changed `"Error" not in result` to `result.startswith("Error")` so only actual error responses (which start with "Error:") are filtered out, not document content that happens to contain the word "Error".
**Prevention Guideline:** When checking for error responses in string results, use `startswith("Error")` or a more specific pattern like `re.match(r"^Error:", result)`. Never use `"Error" in result` which matches document content.

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

### v3.8 — 2026-03-03 (Deep Code Review Rounds 6–9)

- **FIXED (MEDIUM):** `invalidate_cache(project_id, "")` only cleared `__all__` index — per-scope indices not cleared after full-project delete; scoped queries returned stale data (cache.py v1.4→v1.5)
- **FIXED (MEDIUM):** `watcher._sync_changed` — cache not invalidated after `_delete_from_rag`; if ingest failed, stale cache remained for deleted content (watcher.py v1.1→v1.2)
- **Loop 8:** Full review of neo4j.py, qdrant.py, config.py, indexes.py, dedup.py, chunking.py, reranker.py, server.py — no new bugs found
- **Loop 9:** 17 E2E scenarios verified (new/update/delete/move, rapid saves, partial ingest recovery, concurrent access, cache chains)
- Tests: 363→371 passed (8 new), lint clean (ruff)

### v3.7 — 2026-03-03 (Deep Code Review Rounds 2–5)

- **FIXED (CRITICAL PERF):** `neo4j_driver()` created new connection pool per call → `get_driver()` singleton (neo4j.py v2.1→v2.2)
- **FIXED (MEDIUM):** Empty `project_id` silently passed to Neo4j/Qdrant with no error (tools.py v3.5→v3.6)
- **FIXED (MEDIUM):** `delete_all_data` never invalidated Redis cache; added `invalidate_all_cache()` (cache.py v1.3→v1.4, tools.py v3.5→v3.6)
- **FIXED (LOW):** Shared `_index_cache_lock` for both graph and vector indexes → split into separate locks (indexes.py v2.1→v2.2)
- **FIXED (CRASH):** `scroll_field` None payload → `sorted()` TypeError in `get_all_tenant_scopes` / `print_all_stats` (qdrant.py v2.2→v2.3)
- **FIXED (MEDIUM):** `sync_deleted_files`, `sync_project_files`, `watcher._sync_deleted` — backend deletes without cache invalidation (tools.py v3.6→v3.7, watcher.py v1.0→v1.1)
- **FIXED (LOW):** `http_server.py` fallback scope hardcoded `"CORE_CODE"` → `""` (all scopes) (http_server.py v1.7→v1.8)
- **NEW:** `invalidate_project_cache` MCP tool; `reset_graph_index()` + `reset_vector_index()` in indexes.py
- **FIXED (MEDIUM):** Batch ingest chunk loops had no per-chunk error handling — chunk N failure skipped N+1..end (tools.py v3.8→v3.9)
- **FIXED (MEDIUM):** `delete_stale_files` / `sync_deleted_files` only queried Neo4j for orphans → now unions Neo4j + Qdrant paths (qdrant.py v2.3→v2.4, sync.py v1.1→v1.2)
- Tests: 279→363 passed (84 new across rounds 2–5), lint clean (ruff)

### v3.6 — 2026-03-03

- **http_server.py v1.6:** Fixed error check too broad — `"Error" not in result` → `result.startswith("Error")`
- Document content containing "Error" (e.g., "Error Recovery:") was incorrectly filtered out
- Results now sorted by reranker score descending (highest relevance first)
- Fixed parsing to only treat `- [score: X.XXXX]` prefix lines as separate results
- Added `max_chars=0` to disable truncation in HTTP server (UI handles display)

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
