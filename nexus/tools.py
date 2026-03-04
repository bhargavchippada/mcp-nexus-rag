# Version: v4.8
"""
nexus.tools — All @mcp.tool() decorated functions.

Imports are done from the nexus sub-modules; server.py is a thin wrapper
that imports this module to register the tools on the shared mcp instance.
"""

import asyncio
from typing import Optional
from datetime import datetime, timezone
import os
from pathlib import Path
import pathspec
import httpx

from llama_index.core import Document
from llama_index.core.schema import QueryBundle
from llama_index.core.vector_stores import ExactMatchFilter, MetadataFilters
from qdrant_client.http import models as qdrant_models

from nexus.config import (
    logger,
    mcp,
    DEFAULT_QDRANT_URL,
    DEFAULT_OLLAMA_URL,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_RERANKER_CANDIDATE_K,
    MAX_CONTEXT_CHARS,
    MAX_ANSWER_CONTEXT_LIMIT,
    RERANKER_ENABLED,
    OLLAMA_RETRY_COUNT,
    OLLAMA_RETRY_BASE_DELAY,
)
from nexus.dedup import content_hash
from nexus.backends import neo4j as neo4j_backend
from nexus.backends import qdrant as qdrant_backend
from nexus.indexes import get_graph_index, get_vector_index
from nexus.reranker import get_reranker
from nexus.chunking import needs_chunking, chunk_document
from nexus import sync as sync_module
from nexus import cache as cache_module


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _apply_cap(text: str, max_chars: int) -> str:
    """Truncate text to max_chars characters (0 = no cap).

    Applied to BOTH cache hits and fresh retrieval results so that the
    max_chars parameter is always honoured regardless of what is stored
    in the Redis cache.
    """
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + "… [truncated]"
    return text


async def _call_ollama_with_retry(
    url: str, payload: dict, timeout: float = DEFAULT_LLM_TIMEOUT
) -> dict:
    """Call Ollama API with exponential backoff retry on transient failures.

    Args:
        url: Full Ollama API endpoint URL.
        payload: JSON payload for the request.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response from Ollama.

    Retries:
        - Connection failures and timeouts.
        - Transient HTTP statuses: 429, 500, 502, 503, 504.

    Raises:
        httpx.HTTPStatusError: If all retries fail or a non-transient HTTP error occurs.
        httpx.ConnectError: If all retries fail to connect.
        httpx.TimeoutException: If all retries time out.
    """
    retry_count = max(1, OLLAMA_RETRY_COUNT)
    last_exception: Exception | None = None
    for attempt in range(retry_count):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                return response.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            last_exception = e
            if attempt < retry_count - 1:
                delay = OLLAMA_RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    f"Ollama request failed (attempt {attempt + 1}/{retry_count}): "
                    f"{type(e).__name__}. Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"Ollama request failed after {retry_count} attempts: {e}"
                )
                raise
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response is not None else None
            is_transient = status_code in {429, 500, 502, 503, 504}
            if not is_transient or attempt >= retry_count - 1:
                logger.error(
                    "Ollama HTTP error after %s attempt(s): %s",
                    attempt + 1,
                    status_code,
                )
                raise

            last_exception = e
            delay = OLLAMA_RETRY_BASE_DELAY * (2**attempt)
            logger.warning(
                f"Ollama transient HTTP {status_code} (attempt {attempt + 1}/{retry_count}), "
                f"retrying in {delay:.1f}s..."
            )
            await asyncio.sleep(delay)
    # Should not reach here, but satisfy type checker
    if last_exception is not None:
        raise last_exception
    raise RuntimeError("Ollama retry loop exited unexpectedly without an exception")


def _make_metadata(
    project_id: str,
    scope: str,
    source: str,
    content_hash: str,
    file_path: str = "",
) -> dict:
    """Create standard metadata dict with timestamps for all ingestion.

    Args:
        project_id: Tenant project ID.
        scope: Tenant scope.
        source: Source identifier.
        content_hash: SHA-256 hash of content.
        file_path: Optional file path.

    Returns:
        Metadata dict with created_at timestamp.
    """
    now = _utc_now_iso()
    return {
        "project_id": project_id,
        "tenant_scope": scope,
        "scope": scope,  # Duplicate for Qdrant compatibility
        "source": source,
        "content_hash": content_hash,
        "file_path": file_path,
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_ingest_inputs(text: str, project_id: str, scope: str) -> Optional[str]:
    """Return an error string if any ingest input is empty, None otherwise.

    Args:
        text: Document text.
        project_id: Tenant project ID.
        scope: Tenant scope.

    Returns:
        Error message string, or None if all inputs are valid.
    """
    if not text or not text.strip():
        return "Error: 'text' must not be empty."
    if not project_id or not project_id.strip():
        return "Error: 'project_id' must not be empty."
    if not scope or not scope.strip():
        return "Error: 'scope' must not be empty."
    return None


# ---------------------------------------------------------------------------
# Graph tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def ingest_graph_document(
    text: str,
    project_id: str,
    scope: str,
    source_identifier: str = "manual",
    auto_chunk: bool = True,
    file_path: str = "",
) -> str:
    """Ingest a document into the Multi-Tenant GraphRAG memory.

    Large documents exceeding MAX_DOCUMENT_SIZE (default 4KB) are automatically
    chunked into smaller pieces. Each chunk is ingested separately with its own
    content hash, preventing duplicates at the chunk level.

    Skips ingestion if identical content has already been stored for this
    project+scope combination.

    Args:
        text: The content of the document to ingest.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS').
        source_identifier: Optional identifier for the source of the document.
        auto_chunk: If True (default), automatically chunks large documents.
            Set to False to reject documents exceeding MAX_DOCUMENT_SIZE.

    Returns:
        Status: 'Successfully ingested', 'Skipped (duplicate)', or error.
        For chunked documents, returns count of chunks ingested.
    """
    err = _validate_ingest_inputs(text, project_id, scope)
    if err:
        return err

    # Handle large documents
    if needs_chunking(text):
        if not auto_chunk:
            return "Error: Document exceeds size limit. Set auto_chunk=True to split automatically."

        chunks = chunk_document(text)
        ingested = 0
        skipped = 0
        errors = 0

        for i, chunk in enumerate(chunks):
            chash = content_hash(chunk, project_id, scope)
            chunk_source = f"{source_identifier}:chunk_{i + 1}_of_{len(chunks)}"

            if neo4j_backend.is_duplicate(chash, project_id, scope):
                skipped += 1
                continue

            try:
                index = get_graph_index()
                doc = Document(
                    text=chunk,
                    doc_id=chash,
                    metadata=_make_metadata(
                        project_id, scope, chunk_source, chash, file_path
                    ),
                )
                index.insert(doc)
                ingested += 1
            except Exception as e:
                logger.error(f"Error ingesting Graph chunk {i + 1}: {e}")
                errors += 1

        logger.info(
            f"Chunked Graph ingest: {len(chunks)} chunks, ingested={ingested}, "
            f"skipped={skipped}, errors={errors}"
        )
        if ingested > 0:
            if file_path:
                updated = neo4j_backend.backfill_file_metadata(
                    project_id, scope, file_path
                )
                if updated:
                    logger.warning(
                        "Graph metadata backfill updated %d unscoped node(s) for %s",
                        updated,
                        file_path,
                    )
            cache_module.invalidate_cache(project_id, scope)
        # Bug fix: when ALL chunks fail, return an error string so callers
        # (watcher, sync) correctly detect failure via "Error" in result.
        if ingested == 0 and errors > 0:
            return (
                f"Error: All {len(chunks)} chunks failed to ingest into GraphRAG for "
                f"'{project_id}' in scope '{scope}' (skipped={skipped}, errors={errors})."
            )
        return (
            f"Successfully ingested {ingested} chunks into GraphRAG for "
            f"'{project_id}' in scope '{scope}' (skipped={skipped}, errors={errors})."
        )

    # Standard single-document path
    chash = content_hash(text, project_id, scope)
    logger.info(f"Graph ingest: project={project_id} scope={scope} hash={chash[:8]}")

    if neo4j_backend.is_duplicate(chash, project_id, scope):
        logger.info("Duplicate Graph document — skipping LLM extraction.")
        return (
            f"Skipped: duplicate content already exists in GraphRAG for "
            f"project '{project_id}', scope '{scope}'."
        )

    try:
        index = get_graph_index()
        doc = Document(
            text=text,
            doc_id=chash,
            metadata=_make_metadata(
                project_id, scope, source_identifier, chash, file_path
            ),
        )
        index.insert(doc)
        if file_path:
            updated = neo4j_backend.backfill_file_metadata(project_id, scope, file_path)
            if updated:
                logger.warning(
                    "Graph metadata backfill updated %d unscoped node(s) for %s",
                    updated,
                    file_path,
                )
        cache_module.invalidate_cache(project_id, scope)
        return f"Successfully ingested Graph document for '{project_id}' in scope '{scope}'."
    except Exception as e:
        logger.error(f"Error ingesting Graph document: {e}")
        return "Error: Graph document ingestion failed. Check server logs for details."


@mcp.tool()
async def ingest_graph_documents_batch(
    documents: list[dict[str, str]],
    skip_duplicates: bool = True,
    auto_chunk: bool = True,
) -> dict[str, int]:
    """Batch ingest multiple documents into GraphRAG memory.

    Processes multiple documents in a single call for improved performance.
    Each document must have 'text', 'project_id', and 'scope' keys.

    Large documents exceeding MAX_DOCUMENT_SIZE are automatically chunked
    when auto_chunk=True (default).

    Args:
        documents: List of document dicts, each with keys:
            - text: Document content (required)
            - project_id: Tenant project ID (required)
            - scope: Tenant scope (required)
            - source_identifier: Optional source identifier (defaults to 'batch')
        skip_duplicates: If True, skips documents that already exist (default: True).
        auto_chunk: If True (default), automatically chunks large documents.

    Returns:
        Dictionary with counts: {'ingested': N, 'skipped': M, 'errors': K, 'chunks': C}.

    Examples:
        >>> await ingest_graph_documents_batch([
        ...     {"text": "Auth uses JWT", "project_id": "WEB_APP", "scope": "ARCHITECTURE"},
        ...     {"text": "DB is PostgreSQL", "project_id": "WEB_APP", "scope": "ARCHITECTURE"}
        ... ])
        {"ingested": 2, "skipped": 0, "errors": 0, "chunks": 0}
    """
    logger.info(f"Batch Graph ingest: {len(documents)} documents")
    ingested = 0
    skipped = 0
    errors = 0
    chunks_created = 0
    # Track (project_id, scope) pairs with at least one successful ingestion
    invalidation_keys: set[tuple[str, str]] = set()
    # Track unique (project_id, scope, file_path) targets for post-ingest backfill
    backfill_targets: set[tuple[str, str, str]] = set()

    for doc_dict in documents:
        try:
            text = doc_dict.get("text", "")
            project_id = doc_dict.get("project_id", "")
            scope = doc_dict.get("scope", "")
            source_identifier = doc_dict.get("source_identifier", "batch")
            file_path = doc_dict.get("file_path", "")

            err = _validate_ingest_inputs(text, project_id, scope)
            if err:
                logger.warning(f"Validation error in batch item: {err}")
                errors += 1
                continue

            # Handle large documents
            if needs_chunking(text):
                if not auto_chunk:
                    logger.warning("Large document rejected (auto_chunk=False)")
                    errors += 1
                    continue

                chunks = chunk_document(text)
                chunks_created += len(chunks)

                for i, chunk in enumerate(chunks):
                    chash = content_hash(chunk, project_id, scope)
                    chunk_source = f"{source_identifier}:chunk_{i + 1}_of_{len(chunks)}"

                    if skip_duplicates and neo4j_backend.is_duplicate(
                        chash, project_id, scope
                    ):
                        skipped += 1
                        continue

                    try:
                        index = get_graph_index()
                        doc = Document(
                            text=chunk,
                            doc_id=chash,
                            metadata=_make_metadata(
                                project_id, scope, chunk_source, chash, file_path
                            ),
                        )
                        index.insert(doc)
                        ingested += 1
                        invalidation_keys.add((project_id, scope))
                        if file_path:
                            backfill_targets.add((project_id, scope, file_path))
                    except Exception as chunk_err:
                        logger.error(
                            f"Error in batch Graph chunk {i + 1}/{len(chunks)}: {chunk_err}"
                        )
                        errors += 1
                continue

            # Standard single-document path
            chash = content_hash(text, project_id, scope)

            if skip_duplicates and neo4j_backend.is_duplicate(chash, project_id, scope):
                skipped += 1
                continue

            index = get_graph_index()
            doc = Document(
                text=text,
                doc_id=chash,
                metadata=_make_metadata(
                    project_id, scope, source_identifier, chash, file_path
                ),
            )
            index.insert(doc)
            ingested += 1
            invalidation_keys.add((project_id, scope))
            if file_path:
                backfill_targets.add((project_id, scope, file_path))

        except Exception as e:
            logger.error(f"Error in batch Graph ingest: {e}")
            errors += 1

    # Invalidate cache for all (project_id, scope) pairs that received new data
    for pid, sc in invalidation_keys:
        cache_module.invalidate_cache(pid, sc)

    for pid, sc, fp in backfill_targets:
        updated = neo4j_backend.backfill_file_metadata(pid, sc, fp)
        if updated:
            logger.warning(
                "Graph metadata backfill updated %d unscoped node(s) for %s",
                updated,
                fp,
            )

    logger.info(
        f"Batch Graph ingest complete: ingested={ingested}, skipped={skipped}, "
        f"errors={errors}, chunks={chunks_created}"
    )
    return {
        "ingested": ingested,
        "skipped": skipped,
        "errors": errors,
        "chunks": chunks_created,
    }


@mcp.tool()
async def get_graph_context(
    query: str,
    project_id: str,
    scope: str = "",
    rerank: bool = True,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> str:
    """Retrieve isolated context from the GraphRAG memory.

    Retrieves a candidate set of nodes and optionally reranks them using
    bge-reranker-v2-m3 before returning the top results.

    Args:
        query: The user's query.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS').
            If empty or omitted, retrieves from ALL scopes for the project.
        rerank: If True (default) and RERANKER_ENABLED is set, applies the
            cross-encoder reranker to the candidate set before returning.
        max_chars: Truncate the combined context string to this many characters
            before returning (default 3000 ≈ 750 tokens). Set to 0 to disable.

    Returns:
        Structured context relevant to the specific project and scope.
    """
    if not query or not query.strip():
        return "Error: 'query' must not be empty."
    if not project_id or not project_id.strip():
        return "Error: 'project_id' must not be empty."
    scope_label = scope if scope else "all scopes"
    logger.info(
        f"Graph retrieve: project={project_id} scope={scope_label} "
        f"query={query!r} rerank={rerank}"
    )
    cached = cache_module.get_cached(query, project_id, scope, tool_type="graph")
    if cached is not None:
        logger.info(f"Graph cache hit: project={project_id} scope={scope_label}")
        return _apply_cap(cached, max_chars)
    try:
        index = get_graph_index()
        filters_list = [ExactMatchFilter(key="project_id", value=project_id)]
        if scope:
            filters_list.append(ExactMatchFilter(key="tenant_scope", value=scope))
        filters = MetadataFilters(filters=filters_list)
        nodes = await index.as_retriever(
            filters=filters,
            similarity_top_k=DEFAULT_RERANKER_CANDIDATE_K,
        ).aretrieve(query)
        if not nodes:
            return f"No Graph context found for {project_id} in scope {scope_label} for query: '{query}'"
        # Post-retrieval dedup: remove nodes with identical content text
        seen_content: set[str] = set()
        unique_nodes = []
        for n in nodes:
            text = n.node.get_content()
            if text not in seen_content:
                seen_content.add(text)
                unique_nodes.append(n)
        nodes = unique_nodes
        logger.info(f"Graph dedup: {len(nodes)} unique nodes after dedup")
        if rerank and RERANKER_ENABLED:
            try:
                reranker = get_reranker()
                nodes = reranker.postprocess_nodes(
                    nodes, query_bundle=QueryBundle(query_str=query)
                )
                logger.info(f"Graph reranked: {len(nodes)} nodes returned")
            except Exception as rerank_err:
                logger.warning(
                    f"Reranker failed, using un-reranked results: {rerank_err}"
                )
        context_str = "\n".join(
            [
                f"- [score: {(n.score if n.score is not None else 0.0):.4f}] {n.node.get_content()}"
                for n in nodes
            ]
        )
        result = f"Graph Context retrieved for {project_id} in scope {scope_label}:\n{context_str}"
        cache_module.set_cached(query, project_id, scope, result, tool_type="graph")
        return _apply_cap(result, max_chars)
    except Exception as e:
        logger.error(f"Error retrieving Graph context: {e}")
        return "Error: Graph context retrieval failed. Check server logs for details."


# ---------------------------------------------------------------------------
# Vector tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def ingest_vector_document(
    text: str,
    project_id: str,
    scope: str,
    source_identifier: str = "manual",
    auto_chunk: bool = True,
    file_path: str = "",
) -> str:
    """Ingest a document into the Multi-Tenant standard RAG (Vector) memory.

    Large documents exceeding MAX_DOCUMENT_SIZE (default 4KB) are automatically
    chunked into smaller pieces. Each chunk is ingested separately with its own
    content hash, preventing duplicates at the chunk level.

    Skips ingestion if identical content has already been stored for this
    project+scope combination.

    Args:
        text: The content of the document to ingest.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS').
        source_identifier: Optional identifier for the source of the document.
        auto_chunk: If True (default), automatically chunks large documents.
            Set to False to reject documents exceeding MAX_DOCUMENT_SIZE.

    Returns:
        Status: 'Successfully ingested', 'Skipped (duplicate)', or error.
        For chunked documents, returns count of chunks ingested.
    """
    err = _validate_ingest_inputs(text, project_id, scope)
    if err:
        return err

    # Handle large documents
    if needs_chunking(text):
        if not auto_chunk:
            return "Error: Document exceeds size limit. Set auto_chunk=True to split automatically."

        chunks = chunk_document(text)
        ingested = 0
        skipped = 0
        errors = 0

        for i, chunk in enumerate(chunks):
            chash = content_hash(chunk, project_id, scope)
            chunk_source = f"{source_identifier}:chunk_{i + 1}_of_{len(chunks)}"

            if qdrant_backend.is_duplicate(chash, project_id, scope):
                skipped += 1
                continue

            try:
                index = get_vector_index()
                doc = Document(
                    text=chunk,
                    doc_id=chash,
                    metadata=_make_metadata(
                        project_id, scope, chunk_source, chash, file_path
                    ),
                )
                index.insert(doc)
                ingested += 1
            except Exception as e:
                logger.error(f"Error ingesting Vector chunk {i + 1}: {e}")
                errors += 1

        logger.info(
            f"Chunked Vector ingest: {len(chunks)} chunks, ingested={ingested}, "
            f"skipped={skipped}, errors={errors}"
        )
        if ingested > 0:
            cache_module.invalidate_cache(project_id, scope)
        # Bug fix: when ALL chunks fail, return an error string so callers
        # (watcher, sync) correctly detect failure via "Error" in result.
        if ingested == 0 and errors > 0:
            return (
                f"Error: All {len(chunks)} chunks failed to ingest into VectorRAG for "
                f"'{project_id}' in scope '{scope}' (skipped={skipped}, errors={errors})."
            )
        return (
            f"Successfully ingested {ingested} chunks into VectorRAG for "
            f"'{project_id}' in scope '{scope}' (skipped={skipped}, errors={errors})."
        )

    # Standard single-document path
    chash = content_hash(text, project_id, scope)
    logger.info(f"Vector ingest: project={project_id} scope={scope} hash={chash[:8]}")

    if qdrant_backend.is_duplicate(chash, project_id, scope):
        logger.info("Duplicate Vector document — skipping embedding call.")
        return (
            f"Skipped: duplicate content already exists in VectorRAG for "
            f"project '{project_id}', scope '{scope}'."
        )

    try:
        index = get_vector_index()
        doc = Document(
            text=text,
            doc_id=chash,
            metadata=_make_metadata(
                project_id, scope, source_identifier, chash, file_path
            ),
        )
        index.insert(doc)
        cache_module.invalidate_cache(project_id, scope)
        return f"Successfully ingested Vector document for '{project_id}' in scope '{scope}'."
    except Exception as e:
        logger.error(f"Error ingesting Vector document: {e}")
        return "Error: Vector document ingestion failed. Check server logs for details."


@mcp.tool()
async def ingest_vector_documents_batch(
    documents: list[dict[str, str]],
    skip_duplicates: bool = True,
    auto_chunk: bool = True,
) -> dict[str, int]:
    """Batch ingest multiple documents into VectorRAG memory.

    Processes multiple documents in a single call for improved performance.
    Each document must have 'text', 'project_id', and 'scope' keys.

    Large documents exceeding MAX_DOCUMENT_SIZE are automatically chunked
    when auto_chunk=True (default).

    Args:
        documents: List of document dicts, each with keys:
            - text: Document content (required)
            - project_id: Tenant project ID (required)
            - scope: Tenant scope (required)
            - source_identifier: Optional source identifier (defaults to 'batch')
        skip_duplicates: If True, skips documents that already exist (default: True).
        auto_chunk: If True (default), automatically chunks large documents.

    Returns:
        Dictionary with counts: {'ingested': N, 'skipped': M, 'errors': K, 'chunks': C}.

    Examples:
        >>> await ingest_vector_documents_batch([
        ...     {"text": "Auth uses JWT", "project_id": "WEB_APP", "scope": "CODE"},
        ...     {"text": "DB is PostgreSQL", "project_id": "WEB_APP", "scope": "CODE"}
        ... ])
        {"ingested": 2, "skipped": 0, "errors": 0, "chunks": 0}
    """
    logger.info(f"Batch Vector ingest: {len(documents)} documents")
    ingested = 0
    skipped = 0
    errors = 0
    chunks_created = 0
    # Track (project_id, scope) pairs with at least one successful ingestion
    invalidation_keys: set[tuple[str, str]] = set()

    for doc_dict in documents:
        try:
            text = doc_dict.get("text", "")
            project_id = doc_dict.get("project_id", "")
            scope = doc_dict.get("scope", "")
            source_identifier = doc_dict.get("source_identifier", "batch")
            file_path = doc_dict.get("file_path", "")

            err = _validate_ingest_inputs(text, project_id, scope)
            if err:
                logger.warning(f"Validation error in batch item: {err}")
                errors += 1
                continue

            # Handle large documents
            if needs_chunking(text):
                if not auto_chunk:
                    logger.warning("Large document rejected (auto_chunk=False)")
                    errors += 1
                    continue

                chunks = chunk_document(text)
                chunks_created += len(chunks)

                for i, chunk in enumerate(chunks):
                    chash = content_hash(chunk, project_id, scope)
                    chunk_source = f"{source_identifier}:chunk_{i + 1}_of_{len(chunks)}"

                    if skip_duplicates and qdrant_backend.is_duplicate(
                        chash, project_id, scope
                    ):
                        skipped += 1
                        continue

                    try:
                        index = get_vector_index()
                        doc = Document(
                            text=chunk,
                            doc_id=chash,
                            metadata=_make_metadata(
                                project_id, scope, chunk_source, chash, file_path
                            ),
                        )
                        index.insert(doc)
                        ingested += 1
                        invalidation_keys.add((project_id, scope))
                    except Exception as chunk_err:
                        logger.error(
                            f"Error in batch Vector chunk {i + 1}/{len(chunks)}: {chunk_err}"
                        )
                        errors += 1
                continue

            # Standard single-document path
            chash = content_hash(text, project_id, scope)

            if skip_duplicates and qdrant_backend.is_duplicate(
                chash, project_id, scope
            ):
                skipped += 1
                continue

            index = get_vector_index()
            doc = Document(
                text=text,
                doc_id=chash,
                metadata=_make_metadata(
                    project_id, scope, source_identifier, chash, file_path
                ),
            )
            index.insert(doc)
            ingested += 1
            invalidation_keys.add((project_id, scope))

        except Exception as e:
            logger.error(f"Error in batch Vector ingest: {e}")
            errors += 1

    # Invalidate cache for all (project_id, scope) pairs that received new data
    for pid, sc in invalidation_keys:
        cache_module.invalidate_cache(pid, sc)

    logger.info(
        f"Batch Vector ingest complete: ingested={ingested}, skipped={skipped}, "
        f"errors={errors}, chunks={chunks_created}"
    )
    return {
        "ingested": ingested,
        "skipped": skipped,
        "errors": errors,
        "chunks": chunks_created,
    }


@mcp.tool()
async def ingest_document(
    project_id: str,
    scope: str,
    text: str = "",
    file_path: str = "",
    source_identifier: str = "manual",
    auto_chunk: bool = True,
) -> str:
    """Ingest into both graph AND vector databases in one call.

    Accepts either text content or a file path (reads automatically).
    Replaces the manual double-call of ingest_graph_document + ingest_vector_document.

    Args:
        project_id: Tenant project ID (e.g., 'ANTIGRAVITY', 'MCP_NEXUS_RAG').
        scope: Tenant scope (e.g., 'ARCHITECTURE', 'CORE_CODE', 'USER_SESSIONS').
        text: Document content. Mutually exclusive with file_path.
        file_path: Absolute path to a file to read. Mutually exclusive with text.
            When provided, the file contents are read and used as the document text.
            source_identifier defaults to the file_path value if not overridden.
        source_identifier: Source label stored as metadata (default: 'manual').
        auto_chunk: If True (default), automatically chunks large documents.

    Returns:
        Combined status string: "Graph: <result>. Vector: <result>"
    """
    # Resolve content — file_path takes priority; warn if caller passes both
    if file_path:
        if text:
            logger.warning(
                "ingest_document: both 'text' and 'file_path' provided — "
                "file_path takes priority, 'text' is ignored"
            )
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except FileNotFoundError:
            return f"Error: File not found: '{file_path}'."
        except PermissionError:
            return f"Error: Permission denied reading: '{file_path}'."
        except Exception:
            logger.exception("ingest_document: failed to read %s", file_path)
            return f"Error: Failed to read '{file_path}'. Check server logs."
        effective_text = content
        effective_source = (
            source_identifier if source_identifier != "manual" else file_path
        )
    elif text:
        effective_text = text
        effective_source = source_identifier
    else:
        return "Error: Either 'text' or 'file_path' must be provided."

    graph_result = await ingest_graph_document(
        text=effective_text,
        project_id=project_id,
        scope=scope,
        source_identifier=effective_source,
        auto_chunk=auto_chunk,
        file_path=file_path,
    )
    vector_result = await ingest_vector_document(
        text=effective_text,
        project_id=project_id,
        scope=scope,
        source_identifier=effective_source,
        auto_chunk=auto_chunk,
        file_path=file_path,
    )
    return f"Graph: {graph_result}. Vector: {vector_result}"


@mcp.tool()
async def ingest_document_batches(
    documents: list[dict[str, str]],
    skip_duplicates: bool = True,
    auto_chunk: bool = True,
) -> dict:
    """Batch-ingest into both graph AND vector databases.

    Each document dict requires 'project_id', 'scope', and one of 'text'/'file_path'.
    Optional key: 'source_identifier'.

    File paths are read from disk automatically before batching. Unreadable files
    increment the 'file_read_errors' counter and are omitted from ingestion.

    Args:
        documents: List of dicts, each with:
            - project_id: Tenant project ID (required)
            - scope: Tenant scope (required)
            - text: Document content (required unless file_path is given)
            - file_path: Path to file to read (used when text is absent)
            - source_identifier: Optional label (defaults to file_path when reading a file)
        skip_duplicates: If True (default), skips documents already ingested.
        auto_chunk: If True (default), automatically chunks large documents.

    Returns:
        {"graph": {ingested, skipped, errors, chunks}, "vector": {...}, "file_read_errors": N}
    """
    resolved: list[dict] = []
    file_read_errors = 0
    for doc in documents:
        fp = doc.get("file_path", "")
        txt = doc.get("text", "")
        if fp and not txt:
            try:
                with open(fp, "r", encoding="utf-8") as fh:
                    txt = fh.read()
                resolved.append(
                    {
                        **doc,
                        "text": txt,
                        "source_identifier": doc.get("source_identifier", fp),
                    }
                )
            except Exception:
                logger.exception("ingest_document_batches: failed to read %s", fp)
                file_read_errors += 1
        elif txt:
            resolved.append(doc)
        else:
            # Document has neither text nor file_path — log for debugging
            logger.warning(
                "ingest_document_batches: document missing both 'text' and 'file_path', "
                f"project_id={doc.get('project_id', 'N/A')}, scope={doc.get('scope', 'N/A')}"
            )
            file_read_errors += 1

    graph = await ingest_graph_documents_batch(
        resolved, skip_duplicates=skip_duplicates, auto_chunk=auto_chunk
    )
    vector = await ingest_vector_documents_batch(
        resolved, skip_duplicates=skip_duplicates, auto_chunk=auto_chunk
    )
    return {"graph": graph, "vector": vector, "file_read_errors": file_read_errors}


@mcp.tool()
async def get_vector_context(
    query: str,
    project_id: str,
    scope: str = "",
    rerank: bool = True,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> str:
    """Retrieve isolated context from the standard RAG (Vector) memory.

    Retrieves a candidate set of nodes and optionally reranks them using
    bge-reranker-v2-m3 before returning the top results.

    Args:
        query: The user's query.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS').
            If empty or omitted, retrieves from ALL scopes for the project.
        rerank: If True (default) and RERANKER_ENABLED is set, applies the
            cross-encoder reranker to the candidate set before returning.
        max_chars: Truncate the combined context string to this many characters
            before returning (default 3000 ≈ 750 tokens). Set to 0 to disable.

    Returns:
        Structured context relevant to the specific project and scope.
    """
    if not query or not query.strip():
        return "Error: 'query' must not be empty."
    if not project_id or not project_id.strip():
        return "Error: 'project_id' must not be empty."
    scope_label = scope if scope else "all scopes"
    logger.info(
        f"Vector retrieve: project={project_id} scope={scope_label} "
        f"query={query!r} rerank={rerank}"
    )
    cached = cache_module.get_cached(query, project_id, scope, tool_type="vector")
    if cached is not None:
        logger.info(f"Vector cache hit: project={project_id} scope={scope_label}")
        return _apply_cap(cached, max_chars)
    try:
        index = get_vector_index()
        filters_list = [ExactMatchFilter(key="project_id", value=project_id)]
        if scope:
            filters_list.append(ExactMatchFilter(key="tenant_scope", value=scope))
        filters = MetadataFilters(filters=filters_list)
        nodes = await index.as_retriever(
            filters=filters,
            similarity_top_k=DEFAULT_RERANKER_CANDIDATE_K,
        ).aretrieve(query)
        if not nodes:
            return f"No Vector context found for {project_id} in scope {scope_label} for query: '{query}'"
        # Post-retrieval dedup: remove nodes with identical content text
        seen_content: set[str] = set()
        unique_nodes = []
        for n in nodes:
            text = n.node.get_content()
            if text not in seen_content:
                seen_content.add(text)
                unique_nodes.append(n)
        nodes = unique_nodes
        logger.info(f"Vector dedup: {len(nodes)} unique nodes after dedup")
        if rerank and RERANKER_ENABLED:
            try:
                reranker = get_reranker()
                nodes = reranker.postprocess_nodes(
                    nodes, query_bundle=QueryBundle(query_str=query)
                )
                logger.info(f"Vector reranked: {len(nodes)} nodes returned")
            except Exception as rerank_err:
                logger.warning(
                    f"Reranker failed, using un-reranked results: {rerank_err}"
                )
        context_str = "\n".join(
            [
                f"- [score: {(n.score if n.score is not None else 0.0):.4f}] {n.node.get_content()}"
                for n in nodes
            ]
        )
        result = f"Vector Context retrieved for {project_id} in scope {scope_label}:\n{context_str}"
        cache_module.set_cached(query, project_id, scope, result, tool_type="vector")
        return _apply_cap(result, max_chars)
    except Exception as e:
        logger.error(f"Error retrieving Vector context: {e}")
        return "Error: Vector context retrieval failed. Check server logs for details."


# ---------------------------------------------------------------------------
# answer_query helpers (module-level to keep answer_query under C901 limit)
# ---------------------------------------------------------------------------


async def _fetch_graph_passages(
    query: str, project_id: str, scope: str, rerank: bool
) -> list[str]:
    """Retrieve graph RAG passages for answer_query.

    Returns a list of content strings, or an empty list on any error.
    """
    try:
        index = get_graph_index()
        filters_list = [ExactMatchFilter(key="project_id", value=project_id)]
        if scope and scope.strip():
            filters_list.append(ExactMatchFilter(key="tenant_scope", value=scope))
        filters = MetadataFilters(filters=filters_list)
        nodes = await index.as_retriever(
            filters=filters,
            similarity_top_k=DEFAULT_RERANKER_CANDIDATE_K,
        ).aretrieve(query)
        if rerank and RERANKER_ENABLED and nodes:
            try:
                reranker = get_reranker()
                nodes = reranker.postprocess_nodes(
                    nodes, query_bundle=QueryBundle(query_str=query)
                )
            except Exception as e:
                logger.warning(f"Graph reranker failed: {e}")
        return [n.node.get_content() for n in nodes]
    except Exception as e:
        logger.warning(f"Graph retrieval failed in answer_query: {e}")
        return []


async def _fetch_vector_passages(
    query: str, project_id: str, scope: str, rerank: bool
) -> list[str]:
    """Retrieve vector RAG passages for answer_query.

    Returns a list of content strings, or an empty list on any error.
    """
    try:
        index = get_vector_index()
        filters_list = [ExactMatchFilter(key="project_id", value=project_id)]
        if scope and scope.strip():
            filters_list.append(ExactMatchFilter(key="tenant_scope", value=scope))
        filters = MetadataFilters(filters=filters_list)
        nodes = await index.as_retriever(
            filters=filters,
            similarity_top_k=DEFAULT_RERANKER_CANDIDATE_K,
        ).aretrieve(query)
        if rerank and RERANKER_ENABLED and nodes:
            try:
                reranker = get_reranker()
                nodes = reranker.postprocess_nodes(
                    nodes, query_bundle=QueryBundle(query_str=query)
                )
            except Exception as e:
                logger.warning(f"Vector reranker failed: {e}")
        return [n.node.get_content() for n in nodes]
    except Exception as e:
        logger.warning(f"Vector retrieval failed in answer_query: {e}")
        return []


def _dedup_cross_source(
    graph_passages: list[str], vector_passages: list[str]
) -> list[str]:
    """Deduplicate passages across graph and vector sources, preserving attribution.

    Each unique passage is prefixed with its origin: ``[graph]`` or ``[vector]``.
    Passages that appear in both sources are attributed to graph (first seen).
    Empty passages are dropped with debug logging; warns if ALL passages from a
    source are empty (may indicate a backend issue).
    """
    seen: set[str] = set()
    parts: list[str] = []
    dropped_graph = 0
    dropped_vector = 0

    for passage in graph_passages:
        key = passage.strip()
        if not key:
            dropped_graph += 1
            continue
        if key not in seen:
            seen.add(key)
            parts.append(f"[graph] {passage.strip()}")

    for passage in vector_passages:
        key = passage.strip()
        if not key:
            dropped_vector += 1
            continue
        if key not in seen:
            seen.add(key)
            parts.append(f"[vector] {passage.strip()}")

    # Log dropped passages (debug) and warn if ALL from a source are empty
    if dropped_graph > 0:
        logger.debug(
            f"_dedup_cross_source: dropped {dropped_graph} empty graph passages"
        )
    if dropped_vector > 0:
        logger.debug(
            f"_dedup_cross_source: dropped {dropped_vector} empty vector passages"
        )
    if dropped_graph == len(graph_passages) and graph_passages:
        logger.warning(
            "_dedup_cross_source: ALL graph passages were empty — check graph backend"
        )
    if dropped_vector == len(vector_passages) and vector_passages:
        logger.warning(
            "_dedup_cross_source: ALL vector passages were empty — check vector backend"
        )

    return parts


# ---------------------------------------------------------------------------
# Combined RAG + GraphRAG answer tool
# ---------------------------------------------------------------------------


@mcp.tool()
async def answer_query(
    query: str,
    project_id: str,
    scope: str = "",
    rerank: bool = True,
    model: str = "",
    max_context_chars: int = 6000,
) -> str:
    """Answer a user query using both Vector RAG and GraphRAG context combined.

    Retrieves context from both backends **concurrently**, deduplicates passages
    across sources, builds a structured prompt that attributes each passage to its
    origin (vector / graph), and generates a grounded answer with the local Ollama
    LLM (default: ``llama3.1:8b``).

    Falls back gracefully if either backend returns no hits — the answer is still
    generated using whichever context is available.

    Args:
        query: Natural-language question to answer.
        project_id: Tenant project ID (e.g., ``'TRADING_BOT'``).
        scope: Retrieval scope (e.g., ``'CORE_CODE'``). If empty,
            answers across all project scopes.
        rerank: Apply bge-reranker cross-encoder before combining (default True).
        model: Ollama model name override. Defaults to ``DEFAULT_LLM_MODEL``
            (``llama3.1:8b`` unless ``LLM_MODEL`` env var is set).
        max_context_chars: Truncate combined context to this many chars to avoid
            exceeding the model context window (default 6000).

    Returns:
        LLM-generated answer string, or an error message if generation fails.
    """
    if not query or not query.strip():
        return "Error: 'query' must not be empty."
    if not project_id or not project_id.strip():
        return "Error: 'project_id' must not be empty."

    # Clamp max_context_chars to configured limit to prevent excessive memory/token usage
    if max_context_chars > MAX_ANSWER_CONTEXT_LIMIT:
        logger.warning(
            f"answer_query: max_context_chars={max_context_chars} exceeds limit "
            f"{MAX_ANSWER_CONTEXT_LIMIT}, clamping"
        )
        max_context_chars = MAX_ANSWER_CONTEXT_LIMIT

    llm_model = model.strip() if model.strip() else DEFAULT_LLM_MODEL
    scope_msg = scope if (scope and scope.strip()) else "all scopes"
    logger.info(
        f"answer_query: project={project_id} scope={scope_msg} "
        f"model={llm_model} query={query!r}"
    )
    cached = cache_module.get_cached(
        f"answer:{query}", project_id, scope, tool_type="answer"
    )
    if cached is not None:
        logger.info(f"answer_query cache hit: project={project_id} scope={scope_msg}")
        return (
            cached  # answer is LLM output — max_context_chars limits input, not output
        )

    # ── 1. Retrieve from both backends concurrently ──────────────────────────
    graph_passages, vector_passages = await asyncio.gather(
        _fetch_graph_passages(query, project_id, scope, rerank),
        _fetch_vector_passages(query, project_id, scope, rerank),
    )

    logger.info(
        f"answer_query: {len(graph_passages)} graph passages, "
        f"{len(vector_passages)} vector passages before dedup"
    )

    # ── 2. Deduplicate across both sources, preserve attribution ─────────────
    context_parts = _dedup_cross_source(graph_passages, vector_passages)

    if not context_parts:
        return (
            f"No context found for project '{project_id}' scope '{scope_msg}'. "
            f"Please ingest relevant documents before querying."
        )

    logger.info(f"answer_query: {len(context_parts)} unique passages after dedup")

    # ── 3. Build prompt ───────────────────────────────────────────────────────
    combined_context = "\n\n".join(context_parts)
    if len(combined_context) > max_context_chars:
        combined_context = (
            combined_context[:max_context_chars] + "\n...[context truncated]"
        )

    system_prompt = (
        "You are Ari's core identity processor. Answer the user's question using "
        "ONLY the provided context passages. Provide a professional, natural, and "
        "concise summary in flowing prose. Each passage is prefixed with its "
        "source ([graph] or [vector]). Use this attribution internally to ensure "
        "accuracy, but do not mimic relationship arrows (e.g., 'A -> B') or "
        "internal data formats in your final response. If the answer cannot be "
        "found in the context, say so explicitly. Do not hallucinate."
    )
    user_prompt = (
        f"Context passages for project '{project_id}' / scope '{scope_msg}':\n\n"
        f"{combined_context}\n\n"
        f"Question: {query}\n\n"
        "Answer based solely on the context above:"
    )

    # ── 4. Call Ollama /api/chat (non-streaming) ──────────────────────────────
    payload = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.1},
    }

    try:
        data = await _call_ollama_with_retry(f"{DEFAULT_OLLAMA_URL}/api/chat", payload)
        answer: str = data["message"]["content"].strip()

        # Validate answer before caching - prevent caching empty/malformed responses
        if not answer or len(answer) < 10:
            logger.warning(
                f"answer_query: LLM returned empty/short response ({len(answer)} chars), "
                "skipping cache"
            )
            return "Error: LLM returned empty response. Please retry."

        logger.info(
            f"answer_query: answer generated ({len(answer)} chars) via {llm_model}"
        )
        cache_module.set_cached(
            f"answer:{query}", project_id, scope, answer, tool_type="answer"
        )
        return answer
    except httpx.HTTPStatusError as e:
        logger.error(
            f"Ollama HTTP error {e.response.status_code}: {e.response.text[:200]}"
        )
        return f"Error: LLM service returned HTTP {e.response.status_code}. Check server logs."
    except Exception as e:
        logger.error(f"Error generating answer: {e}")
        return "Error: Answer generation failed. Check server logs for details."


# ---------------------------------------------------------------------------
# Health & admin tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def health_check() -> dict[str, str]:
    """Check connectivity to all backend services (Neo4j, Qdrant, Ollama).

    Returns:
        Dictionary with status of each service: "ok" or error message.
    """
    status = {}

    # Check Neo4j
    try:
        with neo4j_backend.get_driver().session() as session:
            session.run("RETURN 1")
        status["neo4j"] = "ok"
    except Exception as e:
        status["neo4j"] = f"error: {str(e)[:100]}"

    # Check Qdrant
    try:
        client = qdrant_backend.get_client(DEFAULT_QDRANT_URL)
        client.get_collections()
        status["qdrant"] = "ok"
    except Exception as e:
        status["qdrant"] = f"error: {str(e)[:100]}"

    # Check Ollama
    try:
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            response = await http_client.get(f"{DEFAULT_OLLAMA_URL}/api/tags")
            if response.status_code == 200:
                status["ollama"] = "ok"
            else:
                status["ollama"] = f"error: HTTP {response.status_code}"
    except Exception as e:
        status["ollama"] = f"error: {str(e)[:100]}"

    logger.info(f"Health check: {status}")
    return status


@mcp.tool()
async def get_all_project_ids() -> list[str]:
    """Retrieve a sorted list of all distinct project IDs across both databases.

    Returns:
        A sorted list of project_id strings.
    """
    logger.info("Retrieving all project IDs")
    graph_ids = neo4j_backend.get_distinct_metadata("project_id")
    vector_ids = qdrant_backend.get_distinct_metadata("project_id")
    return sorted(set(graph_ids) | set(vector_ids))


@mcp.tool()
async def get_all_tenant_scopes(project_id: Optional[str] = None) -> list[str]:
    """Retrieve a sorted list of all distinct tenant scopes across both databases.

    If project_id is provided, only scopes belonging to that project are returned.

    Args:
        project_id: Optional. Filter scopes to a specific project.

    Returns:
        A sorted list of tenant_scope strings.
    """
    logger.info(f"Retrieving all tenant scopes (project_id={project_id})")
    if project_id:
        graph_scopes = neo4j_backend.get_scopes_for_project(project_id)
        try:
            vector_scopes = set(
                qdrant_backend.scroll_field(
                    "tenant_scope",
                    qdrant_filter=qdrant_models.Filter(
                        must=[
                            qdrant_models.FieldCondition(
                                key="project_id",
                                match=qdrant_models.MatchValue(value=project_id),
                            )
                        ]
                    ),
                )
            )
        except Exception as e:
            logger.warning(f"Qdrant scopes error: {e}")
            vector_scopes = set()
        return sorted(set(graph_scopes) | vector_scopes)
    else:
        graph_scopes = neo4j_backend.get_distinct_metadata("tenant_scope")
        vector_scopes = qdrant_backend.get_distinct_metadata("tenant_scope")
        return sorted(set(graph_scopes) | set(vector_scopes))


@mcp.tool()
async def delete_tenant_data(project_id: str, scope: str = "") -> str:
    """Delete all data (both Graph and Vector) for a given project_id.

    If scope is provided, only data matching BOTH project_id and scope is deleted.

    Args:
        project_id: The target tenant project ID. Must not be empty.
        scope: Optional. Restricts deletion to this scope.

    Returns:
        Confirmation message, or error message if a backend failed.
    """
    if not project_id or not project_id.strip():
        return "Error: 'project_id' must not be empty."

    logger.info(f"Deleting data: project_id={project_id!r} scope={scope!r}")
    errors: list[str] = []
    try:
        neo4j_backend.delete_data(project_id, scope)
    except Exception as e:
        errors.append(f"Neo4j: {e}")
    try:
        qdrant_backend.delete_data(project_id, scope)
    except Exception as e:
        errors.append(f"Qdrant: {e}")

    # Always invalidate cache — even on partial failure, cached results are stale
    cache_module.invalidate_cache(project_id, scope)

    label = f"project '{project_id}'"
    if scope:
        label += f", scope '{scope}'"
    if errors:
        return f"Partial failure deleting {label}: {'; '.join(errors)}"
    return f"Successfully deleted data for {label}"


@mcp.tool()
async def get_tenant_stats(project_id: str, scope: str = "") -> str | dict[str, int]:
    """Get statistics for a project (and optionally a specific scope).

    Returns document counts from both GraphRAG and VectorRAG backends,
    including a breakdown of Neo4j chunk nodes (ingested source documents)
    versus entity nodes (LLM-extracted concepts and relationships).

    Args:
        project_id: The target tenant project ID.
        scope: Optional. If provided, returns stats for this specific scope only.

    Returns:
        Dictionary with keys:
        - ``graph_nodes_total``: all Neo4j nodes for this project/scope
        - ``graph_chunk_nodes``: source doc nodes (have content_hash)
        - ``graph_entity_nodes``: LLM-extracted entity nodes (no content_hash)
        - ``vector_docs``: Qdrant points
        - ``total_docs``: graph_nodes_total + vector_docs
        Or an error string on invalid input.
    """
    if not project_id or not project_id.strip():
        return "Error: project_id must not be empty"

    logger.info(f"Getting stats: project_id={project_id!r} scope={scope!r}")

    graph_total = neo4j_backend.get_document_count(project_id, scope)
    graph_chunks = neo4j_backend.get_chunk_node_count(project_id, scope)
    graph_entities = neo4j_backend.get_entity_node_count(project_id, scope)
    vector_count = qdrant_backend.get_document_count(project_id, scope)

    return {
        "graph_nodes_total": graph_total,
        "graph_chunk_nodes": graph_chunks,
        "graph_entity_nodes": graph_entities,
        "vector_docs": vector_count,
        "total_docs": graph_total + vector_count,
    }


@mcp.tool()
async def print_all_stats() -> str:
    """Print a comprehensive table of all projects, scopes, and document counts.

    Displays statistics across all tenants including:
    - Project ID and scope
    - Graph chunk node count (source docs ingested into Neo4j)
    - Graph entity node count (LLM-extracted concept/entity nodes)
    - Vector document count (Qdrant)
    - Total per row
    - Summary totals at the bottom

    Returns:
        Formatted ASCII table string with all statistics.
    """
    logger.info("Generating comprehensive stats table")

    # Gather all project IDs
    graph_project_ids = set(neo4j_backend.get_distinct_metadata("project_id"))
    vector_project_ids = set(qdrant_backend.get_distinct_metadata("project_id"))
    all_project_ids = sorted(graph_project_ids | vector_project_ids)

    if not all_project_ids:
        return "No data found. Both GraphRAG and VectorRAG are empty."

    # Build rows: [(project_id, scope, graph_total, graph_chunks, graph_entities, vector_count)]
    rows: list[tuple[str, str, int, int, int, int]] = []

    for project_id in all_project_ids:
        graph_scopes = set(neo4j_backend.get_scopes_for_project(project_id))
        try:
            vector_scopes = qdrant_backend.scroll_field(
                "tenant_scope",
                qdrant_filter=qdrant_models.Filter(
                    must=[
                        qdrant_models.FieldCondition(
                            key="project_id",
                            match=qdrant_models.MatchValue(value=project_id),
                        )
                    ]
                ),
            )
        except Exception as e:
            logger.warning(f"Qdrant scopes error for project '{project_id}': {e}")
            vector_scopes = set()

        all_scopes = sorted(graph_scopes | vector_scopes)

        if not all_scopes:
            graph_total = neo4j_backend.get_document_count(project_id, "")
            graph_chunks = neo4j_backend.get_chunk_node_count(project_id, "")
            graph_entities = neo4j_backend.get_entity_node_count(project_id, "")
            vector_count = qdrant_backend.get_document_count(project_id, "")
            rows.append(
                (
                    project_id,
                    "(all)",
                    graph_total,
                    graph_chunks,
                    graph_entities,
                    vector_count,
                )
            )
        else:
            for scope in all_scopes:
                graph_total = neo4j_backend.get_document_count(project_id, scope)
                graph_chunks = neo4j_backend.get_chunk_node_count(project_id, scope)
                graph_entities = neo4j_backend.get_entity_node_count(project_id, scope)
                vector_count = qdrant_backend.get_document_count(project_id, scope)
                rows.append(
                    (
                        project_id,
                        scope,
                        graph_total,
                        graph_chunks,
                        graph_entities,
                        vector_count,
                    )
                )

    # Column widths
    col_project = max(len("PROJECT_ID"), max(len(r[0]) for r in rows))
    col_scope = max(len("SCOPE"), max(len(r[1]) for r in rows))
    col_graph = max(len("GRAPH"), max(len(str(r[2])) for r in rows))
    col_chunks = max(len("CHUNKS"), max(len(str(r[3])) for r in rows))
    col_entities = max(len("ENTITIES"), max(len(str(r[4])) for r in rows))
    col_vector = max(len("VECTOR"), max(len(str(r[5])) for r in rows))
    col_total = max(len("TOTAL"), max(len(str(r[2] + r[5])) for r in rows))

    def _sep() -> str:
        return (
            "+"
            + "-" * (col_project + 2)
            + "+"
            + "-" * (col_scope + 2)
            + "+"
            + "-" * (col_graph + 2)
            + "+"
            + "-" * (col_chunks + 2)
            + "+"
            + "-" * (col_entities + 2)
            + "+"
            + "-" * (col_vector + 2)
            + "+"
            + "-" * (col_total + 2)
            + "+"
        )

    sep = _sep()
    header = (
        f"| {'PROJECT_ID':<{col_project}} | {'SCOPE':<{col_scope}} | "
        f"{'GRAPH':>{col_graph}} | {'CHUNKS':>{col_chunks}} | {'ENTITIES':>{col_entities}} | "
        f"{'VECTOR':>{col_vector}} | {'TOTAL':>{col_total}} |"
    )

    lines = [sep, header, sep]

    total_graph = total_chunks = total_entities = total_vector = 0

    for (
        project_id,
        scope,
        graph_total,
        graph_chunks,
        graph_entities,
        vector_count,
    ) in rows:
        row_total = graph_total + vector_count
        total_graph += graph_total
        total_chunks += graph_chunks
        total_entities += graph_entities
        total_vector += vector_count
        line = (
            f"| {project_id:<{col_project}} | {scope:<{col_scope}} | "
            f"{graph_total:>{col_graph}} | {graph_chunks:>{col_chunks}} | "
            f"{graph_entities:>{col_entities}} | {vector_count:>{col_vector}} | "
            f"{row_total:>{col_total}} |"
        )
        lines.append(line)

    lines.append(sep)

    grand_total = total_graph + total_vector
    summary = (
        f"| {'TOTAL':<{col_project}} | {'':<{col_scope}} | "
        f"{total_graph:>{col_graph}} | {total_chunks:>{col_chunks}} | "
        f"{total_entities:>{col_entities}} | {total_vector:>{col_vector}} | "
        f"{grand_total:>{col_total}} |"
    )
    lines.append(summary)
    lines.append(sep)

    lines.append(
        f"\nProjects: {len(all_project_ids)} | Rows: {len(rows)} | "
        f"Graph nodes: {total_graph} (chunks={total_chunks}, entities={total_entities}) | "
        f"Vector docs: {total_vector} | Total: {grand_total}"
    )

    return "\n".join(lines)


@mcp.tool()
async def delete_all_data() -> str:
    """Delete ALL data from both GraphRAG (Neo4j) and VectorRAG (Qdrant).

    This is a destructive, irreversible operation that removes every document
    across ALL project IDs and scopes. Use only for full database resets.

    Returns:
        Confirmation message, or partial-failure message if a backend failed.
    """
    logger.warning("delete_all_data called — wiping ALL data from both backends")
    errors: list[str] = []
    try:
        neo4j_backend.delete_all_data()
    except Exception as e:
        errors.append(f"Neo4j: {e}")
    try:
        qdrant_backend.delete_all_data()
    except Exception as e:
        errors.append(f"Qdrant: {e}")

    # Invalidate the entire Redis cache — all entries are now stale
    cache_module.invalidate_all_cache()

    if errors:
        return f"Partial failure deleting all data: {'; '.join(errors)}"
    return "Successfully deleted ALL data from GraphRAG (Neo4j) and VectorRAG (Qdrant)."


DEFAULT_INCLUDE_EXTENSIONS = [".py", ".ts", ".js", ".md", ".txt", ".json"]


@mcp.tool()
async def ingest_project_directory(
    directory_path: str,
    project_id: str,
    scope: str,
    include_extensions: Optional[list[str]] = None,
    auto_chunk: bool = True,
) -> str:
    """Recursively ingest a project directory into both GraphRAG and VectorRAG.

    Respects .gitignore rules using pathspec. This is the recommended way
    to feed an entire codebase into the system.

    Args:
        directory_path: Absolute path to the directory.
        project_id: Tenant project ID.
        scope: Tenant scope.
        include_extensions: List of file extensions to include (e.g. ['.py', '.ts']).
            Defaults to ['.py', '.ts', '.js', '.md', '.txt', '.json'].
        auto_chunk: Whether to chunk large files.
    """
    if include_extensions is None:
        include_extensions = DEFAULT_INCLUDE_EXTENSIONS.copy()

    # Validate extensions: empty string matches every file (str.endswith("") is always True).
    # Normalise so callers can pass "py" or ".py" interchangeably.
    normalised_exts: list[str] = []
    for ext in include_extensions:
        ext = ext.strip()
        if not ext:
            logger.warning(
                "ingest_project_directory: ignoring empty extension in include_extensions"
            )
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        normalised_exts.append(ext)
    if not normalised_exts:
        return "Error: include_extensions contains no valid extensions after normalisation."
    include_extensions = normalised_exts

    base_path = Path(directory_path)
    if not base_path.is_dir():
        return f"Error: {directory_path} is not a directory."

    # Load gitignore
    gitignore_path = base_path / ".gitignore"
    spec = None
    if gitignore_path.exists():
        with open(gitignore_path, "r", encoding="utf-8") as f:
            spec = pathspec.PathSpec.from_lines("gitwildmatch", f)

    count = 0
    errors = []

    for root, dirs, files in os.walk(directory_path):
        # Filter directories based on gitignore
        if spec:
            rel_root = os.path.relpath(root, directory_path)
            if rel_root != ".":
                if spec.match_file(rel_root + os.sep):
                    dirs[:] = []  # Don't recurse into ignored dirs
                    continue

        for file in files:
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, directory_path)

            # Check gitignore
            if spec and spec.match_file(rel_path):
                continue

            # Check extension
            if not any(file.endswith(ext) for ext in include_extensions):
                continue

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()

                # Ingest into GraphRAG
                graph_result = await ingest_graph_document(
                    text=content,
                    project_id=project_id,
                    scope=scope,
                    source_identifier=rel_path,
                    auto_chunk=auto_chunk,
                    file_path=rel_path,
                )

                # Ingest into VectorRAG
                vector_result = await ingest_vector_document(
                    text=content,
                    project_id=project_id,
                    scope=scope,
                    source_identifier=rel_path,
                    auto_chunk=auto_chunk,
                    file_path=rel_path,
                )

                # Only count as success if neither ingest returned an error
                if "Error" not in graph_result and "Error" not in vector_result:
                    count += 1
                else:
                    errors.append(
                        f"{rel_path}: graph={graph_result[:80]!r}, "
                        f"vector={vector_result[:80]!r}"
                    )
            except Exception as e:
                errors.append(f"{rel_path}: {e}")

    res = f"Successfully ingested {count} files into GraphRAG and VectorRAG."
    if errors:
        res += f"\nErrors occurred in {len(errors)} files:\n" + "\n".join(errors[:10])
    return res


@mcp.tool()
async def sync_deleted_files(
    directory_path: str,
    project_id: str,
    scope: str,
) -> str:
    """Synchronize RAG databases by removing files that no longer exist on disk.

    Useful for cleaning up stale data after file deletions or refactorings.

    Args:
        directory_path: Absolute path to the local project root.
        project_id: Tenant project ID.
        scope: Tenant scope.
    """
    base_path = Path(directory_path)
    if not base_path.is_dir():
        return f"Error: {directory_path} is not a directory."

    # Union Neo4j + Qdrant — catch orphans in either store
    neo4j_paths = set(neo4j_backend.get_all_filepaths(project_id, scope))
    qdrant_paths = set(qdrant_backend.get_all_filepaths(project_id, scope))
    stored_paths = neo4j_paths | qdrant_paths
    if not stored_paths:
        return "No files found in database to sync."

    removed_count = 0
    errors = []

    for rel_path in stored_paths:
        if not rel_path:
            continue
        full_path = base_path / rel_path
        if not full_path.exists():
            try:
                # Delete from Neo4j
                neo4j_backend.delete_by_filepath(project_id, rel_path, scope)
                # Delete from Qdrant
                qdrant_backend.delete_by_filepath(project_id, rel_path, scope)
                removed_count += 1
                logger.info(f"Sync: Removed stale file {rel_path} from database")
            except Exception as e:
                errors.append(f"{rel_path}: {e}")

    # Invalidate cache for this project/scope if any files were removed
    if removed_count > 0:
        cache_module.invalidate_cache(project_id, scope)

    res = f"Synchronized databases. Removed {removed_count} stale file entries."
    if errors:
        res += "\nErrors occurred during sync:\n" + "\n".join(errors[:10])
    return res


@mcp.tool()
async def sync_project_files(
    workspace_root: str = "/home/turiya/antigravity",
    dry_run: bool = False,
) -> str:
    """Sync all core documentation files from the workspace into RAG.

    Scans the workspace for core documentation files (README.md, MEMORY.md,
    AGENTS.md, TODO.md) in each project and persona files at workspace level.
    Uses content hashing to skip unchanged files.

    Args:
        workspace_root: Path to antigravity workspace root (default: /home/turiya/antigravity).
        dry_run: If True, only report what would be synced without actually ingesting.

    Returns:
        Summary of files synced or needing sync.
    """
    workspace_root_path = Path(workspace_root)
    files_to_sync = sync_module.get_files_needing_sync(workspace_root)

    if not files_to_sync:
        return "All core documentation files are up to date. Nothing to sync."

    if dry_run:
        lines = ["Files needing sync (dry run):"]
        for f in files_to_sync:
            lines.append(f"  - {f['source']} ({f['project_id']}/{f['scope']})")
        return "\n".join(lines)

    # Perform sync
    ingested = 0
    errors = []

    for f in files_to_sync:
        filepath = f["filepath"]
        try:
            content = filepath.read_text(encoding="utf-8")
            canonical_path = sync_module.canonical_file_path(filepath, workspace_root_path)

            # Delete old version first (by filepath).
            # Bug L10-1 fix: log + skip on connection error (bare pass hid failures,
            # leaving old chunks alongside newly ingested ones = duplicate content).
            try:
                neo4j_backend.delete_by_filepath(
                    f["project_id"], canonical_path, f["scope"]
                )
                qdrant_backend.delete_by_filepath(
                    f["project_id"], canonical_path, f["scope"]
                )
            except Exception as e:
                logger.warning(f"Pre-delete error for {f['source']}: {e}")
                errors.append(f"{f['source']}: pre-delete failed: {e}")
                continue

            # Bug L10-2 fix: invalidate cache immediately after pre-delete and before
            # ingest so stale results aren't served if ingest fails (fail-open: empty > stale).
            cache_module.invalidate_cache(f["project_id"], f["scope"])

            # Ingest to both stores
            graph_result = await ingest_graph_document(
                text=content,
                project_id=f["project_id"],
                scope=f["scope"],
                source_identifier=f["source"],
                file_path=canonical_path,
            )
            vector_result = await ingest_vector_document(
                text=content,
                project_id=f["project_id"],
                scope=f["scope"],
                source_identifier=f["source"],
                file_path=canonical_path,
            )

            if "Error" not in graph_result and "Error" not in vector_result:
                ingested += 1
                logger.info(f"Synced: {f['source']}")
            else:
                errors.append(
                    f"{f['source']}: graph={graph_result[:50]}, vector={vector_result[:50]}"
                )

        except Exception as e:
            errors.append(f"{f['source']}: {e}")
            logger.error(f"Sync error for {f['source']}: {e}")

    # Delete stale RAG documents (files deleted from disk since last sync)
    stale_deleted: list[str] = []
    stale_scopes = [("AGENT", "PERSONA")] + [
        (pid, "CORE_DOCS") for pid in sync_module.PROJECT_MAPPINGS.values()
    ]
    for project_id, scope in stale_scopes:
        try:
            deleted = sync_module.delete_stale_files(workspace_root, project_id, scope)
            stale_deleted.extend(deleted)
            if deleted:
                cache_module.invalidate_cache(project_id, scope)
        except Exception as e:
            logger.warning(f"Stale cleanup error for {project_id}/{scope}: {e}")

    result = f"Synced {ingested} of {len(files_to_sync)} files."
    if stale_deleted:
        result += f"\nDeleted {len(stale_deleted)} stale document(s) for removed files."
    if errors:
        result += f"\n\nErrors ({len(errors)}):\n" + "\n".join(errors[:10])

    return result


@mcp.tool()
async def list_core_doc_files(
    workspace_root: str = "/home/turiya/antigravity",
) -> str:
    """List all core documentation files that would be tracked for sync.

    Args:
        workspace_root: Path to antigravity workspace root.

    Returns:
        List of files with their project_id and scope.
    """
    files = sync_module.get_core_doc_files(workspace_root)

    if not files:
        return "No core documentation files found."

    lines = [f"Found {len(files)} core documentation files:"]
    for f in files:
        lines.append(f"  - {f['source']} ({f['project_id']}/{f['scope']})")

    return "\n".join(lines)


@mcp.tool()
async def invalidate_project_cache(project_id: str, scope: str = "") -> str:
    """Invalidate Redis cache entries for a specific project without deleting data.

    Clears cached query results (get_vector_context, get_graph_context, answer_query)
    for the given project/scope. Use after external data modifications or when you
    need to force fresh retrieval results.

    Args:
        project_id: Tenant project ID. Must not be empty.
        scope: Optional tenant scope. If empty, invalidates ALL cached queries
            for the project (both per-scope and cross-scope queries). If provided,
            invalidates cache for that specific scope plus cross-scope queries,
            since scoped additions make them stale.

    Returns:
        Confirmation message with count of cache keys cleared.
    """
    if not project_id or not project_id.strip():
        return "Error: 'project_id' must not be empty."

    count = cache_module.invalidate_cache(project_id, scope)
    label = f"project '{project_id}'"
    if scope:
        label += f", scope '{scope}'"
    return f"Invalidated {count} cache key(s) for {label}."


@mcp.tool()
async def cache_stats() -> str:
    """Get Redis cache statistics for the semantic cache.

    Returns:
        Cache status including key count, memory usage, and TTL settings.
    """
    stats = cache_module.cache_stats()

    lines = ["Redis Cache Stats:"]
    lines.append(f"  Enabled: {stats.get('enabled', False)}")
    lines.append(f"  TTL: {stats.get('ttl_seconds', 0)} seconds")
    lines.append(f"  Nexus Keys: {stats.get('nexus_keys', 0)}")
    lines.append(f"  Memory: {stats.get('used_memory_human', 'unknown')}")

    if "error" in stats:
        lines.append(f"  Error: {stats['error']}")

    return "\n".join(lines)
