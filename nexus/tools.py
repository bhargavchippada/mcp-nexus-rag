# Version: v2.5
"""
nexus.tools — All @mcp.tool() decorated functions.

Imports are done from the nexus sub-modules; server.py is a thin wrapper
that imports this module to register the tools on the shared mcp instance.
"""

from typing import Optional

from llama_index.core import Document
from llama_index.core.schema import QueryBundle
from llama_index.core.vector_stores import ExactMatchFilter, MetadataFilters
from qdrant_client.http import models as qdrant_models

from nexus.config import (
    logger,
    mcp,
    DEFAULT_QDRANT_URL,
    DEFAULT_OLLAMA_URL,
    DEFAULT_RERANKER_CANDIDATE_K,
    RERANKER_ENABLED,
)
from nexus.dedup import content_hash
from nexus.backends import neo4j as neo4j_backend
from nexus.backends import qdrant as qdrant_backend
from nexus.indexes import get_graph_index, get_vector_index
from nexus.reranker import get_reranker
from nexus.chunking import needs_chunking, chunk_document


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
) -> str:
    """Ingest a document into the Multi-Tenant GraphRAG memory.

    Large documents exceeding MAX_DOCUMENT_SIZE (default 512KB) are automatically
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
            from nexus.config import MAX_DOCUMENT_SIZE
            return f"Error: Document exceeds {MAX_DOCUMENT_SIZE // 1024}KB limit. Set auto_chunk=True to split automatically."

        chunks = chunk_document(text)
        ingested = 0
        skipped = 0
        errors = 0

        for i, chunk in enumerate(chunks):
            chash = content_hash(chunk, project_id, scope)
            chunk_source = f"{source_identifier}:chunk_{i+1}_of_{len(chunks)}"

            if neo4j_backend.is_duplicate(chash, project_id, scope):
                skipped += 1
                continue

            try:
                index = get_graph_index()
                doc = Document(
                    text=chunk,
                    doc_id=chash,
                    metadata={
                        "project_id": project_id,
                        "tenant_scope": scope,
                        "source": chunk_source,
                        "content_hash": chash,
                    },
                )
                index.insert(doc)
                ingested += 1
            except Exception as e:
                logger.error(f"Error ingesting Graph chunk {i+1}: {e}")
                errors += 1

        logger.info(
            f"Chunked Graph ingest: {len(chunks)} chunks, ingested={ingested}, "
            f"skipped={skipped}, errors={errors}"
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
            metadata={
                "project_id": project_id,
                "tenant_scope": scope,
                "source": source_identifier,
                "content_hash": chash,
            },
        )
        index.insert(doc)
        return f"Successfully ingested Graph document for '{project_id}' in scope '{scope}'."
    except Exception as e:
        logger.error(f"Error ingesting Graph document: {e}")
        return f"Error ingesting Graph document: {e}"


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

    for doc_dict in documents:
        try:
            text = doc_dict.get("text", "")
            project_id = doc_dict.get("project_id", "")
            scope = doc_dict.get("scope", "")
            source_identifier = doc_dict.get("source_identifier", "batch")

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
                    chunk_source = f"{source_identifier}:chunk_{i+1}_of_{len(chunks)}"

                    if skip_duplicates and neo4j_backend.is_duplicate(chash, project_id, scope):
                        skipped += 1
                        continue

                    index = get_graph_index()
                    doc = Document(
                        text=chunk,
                        doc_id=chash,
                        metadata={
                            "project_id": project_id,
                            "tenant_scope": scope,
                            "source": chunk_source,
                            "content_hash": chash,
                        },
                    )
                    index.insert(doc)
                    ingested += 1
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
                metadata={
                    "project_id": project_id,
                    "tenant_scope": scope,
                    "source": source_identifier,
                    "content_hash": chash,
                },
            )
            index.insert(doc)
            ingested += 1

        except Exception as e:
            logger.error(f"Error in batch Graph ingest: {e}")
            errors += 1

    logger.info(
        f"Batch Graph ingest complete: ingested={ingested}, skipped={skipped}, "
        f"errors={errors}, chunks={chunks_created}"
    )
    return {"ingested": ingested, "skipped": skipped, "errors": errors, "chunks": chunks_created}


@mcp.tool()
async def get_graph_context(
    query: str,
    project_id: str,
    scope: str,
    rerank: bool = True,
) -> str:
    """Retrieve isolated context from the GraphRAG memory.

    Retrieves a candidate set of nodes and optionally reranks them using
    bge-reranker-v2-m3 before returning the top results.

    Args:
        query: The user's query.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS').
        rerank: If True (default) and RERANKER_ENABLED is set, applies the
            cross-encoder reranker to the candidate set before returning.

    Returns:
        Structured context relevant to the specific project and scope.
    """
    logger.info(
        f"Graph retrieve: project={project_id} scope={scope} "
        f"query={query!r} rerank={rerank}"
    )
    try:
        index = get_graph_index()
        filters = MetadataFilters(
            filters=[
                ExactMatchFilter(key="project_id", value=project_id),
                ExactMatchFilter(key="tenant_scope", value=scope),
            ]
        )
        nodes = await index.as_retriever(
            filters=filters,
            similarity_top_k=DEFAULT_RERANKER_CANDIDATE_K,
        ).aretrieve(query)
        if not nodes:
            return f"No Graph context found for {project_id} in scope {scope} for query: '{query}'"
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
                logger.warning(f"Reranker failed, using un-reranked results: {rerank_err}")
        context_str = "\n".join([f"- {n.node.get_content()}" for n in nodes])
        return (
            f"Graph Context retrieved for {project_id} in scope {scope}:\n{context_str}"
        )
    except Exception as e:
        logger.error(f"Error retrieving Graph context: {e}")
        return f"Error retrieving Graph context: {e}"


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
) -> str:
    """Ingest a document into the Multi-Tenant standard RAG (Vector) memory.

    Large documents exceeding MAX_DOCUMENT_SIZE (default 512KB) are automatically
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
            from nexus.config import MAX_DOCUMENT_SIZE
            return f"Error: Document exceeds {MAX_DOCUMENT_SIZE // 1024}KB limit. Set auto_chunk=True to split automatically."

        chunks = chunk_document(text)
        ingested = 0
        skipped = 0
        errors = 0

        for i, chunk in enumerate(chunks):
            chash = content_hash(chunk, project_id, scope)
            chunk_source = f"{source_identifier}:chunk_{i+1}_of_{len(chunks)}"

            if qdrant_backend.is_duplicate(chash, project_id, scope):
                skipped += 1
                continue

            try:
                index = get_vector_index()
                doc = Document(
                    text=chunk,
                    doc_id=chash,
                    metadata={
                        "project_id": project_id,
                        "tenant_scope": scope,
                        "source": chunk_source,
                        "content_hash": chash,
                    },
                )
                index.insert(doc)
                ingested += 1
            except Exception as e:
                logger.error(f"Error ingesting Vector chunk {i+1}: {e}")
                errors += 1

        logger.info(
            f"Chunked Vector ingest: {len(chunks)} chunks, ingested={ingested}, "
            f"skipped={skipped}, errors={errors}"
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
            metadata={
                "project_id": project_id,
                "tenant_scope": scope,
                "source": source_identifier,
                "content_hash": chash,
            },
        )
        index.insert(doc)
        return f"Successfully ingested Vector document for '{project_id}' in scope '{scope}'."
    except Exception as e:
        logger.error(f"Error ingesting Vector document: {e}")
        return f"Error ingesting Vector document: {e}"


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

    for doc_dict in documents:
        try:
            text = doc_dict.get("text", "")
            project_id = doc_dict.get("project_id", "")
            scope = doc_dict.get("scope", "")
            source_identifier = doc_dict.get("source_identifier", "batch")

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
                    chunk_source = f"{source_identifier}:chunk_{i+1}_of_{len(chunks)}"

                    if skip_duplicates and qdrant_backend.is_duplicate(chash, project_id, scope):
                        skipped += 1
                        continue

                    index = get_vector_index()
                    doc = Document(
                        text=chunk,
                        doc_id=chash,
                        metadata={
                            "project_id": project_id,
                            "tenant_scope": scope,
                            "source": chunk_source,
                            "content_hash": chash,
                        },
                    )
                    index.insert(doc)
                    ingested += 1
                continue

            # Standard single-document path
            chash = content_hash(text, project_id, scope)

            if skip_duplicates and qdrant_backend.is_duplicate(chash, project_id, scope):
                skipped += 1
                continue

            index = get_vector_index()
            doc = Document(
                text=text,
                doc_id=chash,
                metadata={
                    "project_id": project_id,
                    "tenant_scope": scope,
                    "source": source_identifier,
                    "content_hash": chash,
                },
            )
            index.insert(doc)
            ingested += 1

        except Exception as e:
            logger.error(f"Error in batch Vector ingest: {e}")
            errors += 1

    logger.info(
        f"Batch Vector ingest complete: ingested={ingested}, skipped={skipped}, "
        f"errors={errors}, chunks={chunks_created}"
    )
    return {"ingested": ingested, "skipped": skipped, "errors": errors, "chunks": chunks_created}


@mcp.tool()
async def get_vector_context(
    query: str,
    project_id: str,
    scope: str,
    rerank: bool = True,
) -> str:
    """Retrieve isolated context from the standard RAG (Vector) memory.

    Retrieves a candidate set of nodes and optionally reranks them using
    bge-reranker-v2-m3 before returning the top results.

    Args:
        query: The user's query.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS').
        rerank: If True (default) and RERANKER_ENABLED is set, applies the
            cross-encoder reranker to the candidate set before returning.

    Returns:
        Structured context relevant to the specific project and scope.
    """
    logger.info(
        f"Vector retrieve: project={project_id} scope={scope} "
        f"query={query!r} rerank={rerank}"
    )
    try:
        index = get_vector_index()
        filters = MetadataFilters(
            filters=[
                ExactMatchFilter(key="project_id", value=project_id),
                ExactMatchFilter(key="tenant_scope", value=scope),
            ]
        )
        nodes = await index.as_retriever(
            filters=filters,
            similarity_top_k=DEFAULT_RERANKER_CANDIDATE_K,
        ).aretrieve(query)
        if not nodes:
            return f"No Vector context found for {project_id} in scope {scope} for query: '{query}'"
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
                logger.warning(f"Reranker failed, using un-reranked results: {rerank_err}")
        context_str = "\n".join([f"- {n.node.get_content()}" for n in nodes])
        return f"Vector Context retrieved for {project_id} in scope {scope}:\n{context_str}"
    except Exception as e:
        logger.error(f"Error retrieving Vector context: {e}")
        return f"Error retrieving Vector context: {e}"


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
    import asyncio
    import httpx

    from nexus.config import DEFAULT_LLM_MODEL, DEFAULT_OLLAMA_URL, DEFAULT_LLM_TIMEOUT

    if not query or not query.strip():
        return "Error: 'query' must not be empty."
    if not project_id or not project_id.strip():
        return "Error: 'project_id' must not be empty."

    llm_model = model.strip() if model.strip() else DEFAULT_LLM_MODEL
    scope_msg = scope if (scope and scope.strip()) else "all scopes"
    logger.info(
        f"answer_query: project={project_id} scope={scope_msg} "
        f"model={llm_model} query={query!r}"
    )

    # ── 1. Retrieve from both backends concurrently ──────────────────────────
    async def _fetch_graph() -> list[str]:
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

    async def _fetch_vector() -> list[str]:
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

    graph_passages, vector_passages = await asyncio.gather(_fetch_graph(), _fetch_vector())

    logger.info(
        f"answer_query: {len(graph_passages)} graph passages, "
        f"{len(vector_passages)} vector passages before dedup"
    )

    # ── 2. Deduplicate across both sources, preserve attribution ─────────────
    seen: set[str] = set()
    context_parts: list[str] = []

    for passage in graph_passages:
        key = passage.strip()
        if key and key not in seen:
            seen.add(key)
            context_parts.append(f"[graph] {passage.strip()}")

    for passage in vector_passages:
        key = passage.strip()
        if key and key not in seen:
            seen.add(key)
            context_parts.append(f"[vector] {passage.strip()}")

    if not context_parts:
        return (
            f"No context found for project '{project_id}' scope '{scope_msg}'. "
            f"Please ingest relevant documents before querying."
        )

    logger.info(f"answer_query: {len(context_parts)} unique passages after dedup")

    # ── 3. Build prompt ───────────────────────────────────────────────────────
    combined_context = "\n\n".join(context_parts)
    if len(combined_context) > max_context_chars:
        combined_context = combined_context[:max_context_chars] + "\n...[context truncated]"

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
        async with httpx.AsyncClient(timeout=DEFAULT_LLM_TIMEOUT) as http_client:
            response = await http_client.post(
                f"{DEFAULT_OLLAMA_URL}/api/chat",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            answer: str = data["message"]["content"].strip()
            logger.info(
                f"answer_query: answer generated ({len(answer)} chars) "
                f"via {llm_model}"
            )
            return answer
    except httpx.HTTPStatusError as e:
        err = f"Ollama HTTP error {e.response.status_code}: {e.response.text[:200]}"
        logger.error(err)
        return err
    except Exception as e:
        err = f"Error generating answer: {e}"
        logger.error(err)
        return err


# ---------------------------------------------------------------------------
# Health & admin tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def health_check() -> dict[str, str]:
    """Check connectivity to all backend services (Neo4j, Qdrant, Ollama).

    Returns:
        Dictionary with status of each service: "ok" or error message.
    """
    import httpx

    status = {}

    # Check Neo4j
    try:
        with neo4j_backend.neo4j_driver() as driver:
            with driver.session() as session:
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
            vector_scopes = set(qdrant_backend.scroll_field(
                "tenant_scope",
                qdrant_filter=qdrant_models.Filter(
                    must=[
                        qdrant_models.FieldCondition(
                            key="project_id",
                            match=qdrant_models.MatchValue(value=project_id),
                        )
                    ]
                ),
            ))
        except Exception as e:
            logger.warning(f"Qdrant scopes error: {e}")
            vector_scopes = set()
        return sorted(set(graph_scopes) | vector_scopes)
    else:
        graph_scopes = neo4j_backend.get_distinct_metadata("tenant_scope")
        vector_scopes2 = qdrant_backend.get_distinct_metadata("tenant_scope")
        return sorted(set(graph_scopes) | set(vector_scopes2))


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

    label = f"project '{project_id}'"
    if scope:
        label += f", scope '{scope}'"
    if errors:
        return f"Partial failure deleting {label}: {'; '.join(errors)}"
    return f"Successfully deleted data for {label}"


@mcp.tool()
async def get_tenant_stats(project_id: str, scope: str = "") -> dict[str, int]:
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
    """
    if not project_id or not project_id.strip():
        raise ValueError("project_id must not be empty")

    logger.info(f"Getting stats: project_id={project_id!r} scope={scope!r}")

    graph_total   = neo4j_backend.get_document_count(project_id, scope)
    graph_chunks  = neo4j_backend.get_chunk_node_count(project_id, scope)
    graph_entities = neo4j_backend.get_entity_node_count(project_id, scope)
    vector_count  = qdrant_backend.get_document_count(project_id, scope)

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
        except Exception:
            vector_scopes = set()

        all_scopes = sorted(graph_scopes | vector_scopes)

        if not all_scopes:
            graph_total = neo4j_backend.get_document_count(project_id, "")
            graph_chunks = neo4j_backend.get_chunk_node_count(project_id, "")
            graph_entities = neo4j_backend.get_entity_node_count(project_id, "")
            vector_count = qdrant_backend.get_document_count(project_id, "")
            rows.append((project_id, "(all)", graph_total, graph_chunks, graph_entities, vector_count))
        else:
            for scope in all_scopes:
                graph_total = neo4j_backend.get_document_count(project_id, scope)
                graph_chunks = neo4j_backend.get_chunk_node_count(project_id, scope)
                graph_entities = neo4j_backend.get_entity_node_count(project_id, scope)
                vector_count = qdrant_backend.get_document_count(project_id, scope)
                rows.append((project_id, scope, graph_total, graph_chunks, graph_entities, vector_count))

    # Column widths
    col_project  = max(len("PROJECT_ID"), max(len(r[0]) for r in rows))
    col_scope    = max(len("SCOPE"),      max(len(r[1]) for r in rows))
    col_graph    = max(len("GRAPH"),      max(len(str(r[2])) for r in rows))
    col_chunks   = max(len("CHUNKS"),     max(len(str(r[3])) for r in rows))
    col_entities = max(len("ENTITIES"),   max(len(str(r[4])) for r in rows))
    col_vector   = max(len("VECTOR"),     max(len(str(r[5])) for r in rows))
    col_total    = max(len("TOTAL"),      max(len(str(r[2] + r[5])) for r in rows))

    def _sep() -> str:
        return (
            "+" + "-" * (col_project  + 2)
            + "+" + "-" * (col_scope    + 2)
            + "+" + "-" * (col_graph    + 2)
            + "+" + "-" * (col_chunks   + 2)
            + "+" + "-" * (col_entities + 2)
            + "+" + "-" * (col_vector   + 2)
            + "+" + "-" * (col_total    + 2)
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

    for project_id, scope, graph_total, graph_chunks, graph_entities, vector_count in rows:
        row_total = graph_total + vector_count
        total_graph    += graph_total
        total_chunks   += graph_chunks
        total_entities += graph_entities
        total_vector   += vector_count
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

    if errors:
        return f"Partial failure deleting all data: {'; '.join(errors)}"
    return "Successfully deleted ALL data from GraphRAG (Neo4j) and VectorRAG (Qdrant)."
