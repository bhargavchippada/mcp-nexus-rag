# Version: v1.0
"""
nexus.tools — All @mcp.tool() decorated functions.

Imports are done from the nexus sub-modules; server.py is a thin wrapper
that imports this module to register the tools on the shared mcp instance.
"""
from typing import Optional

from llama_index.core import Document
from llama_index.core.vector_stores import ExactMatchFilter, MetadataFilters
from qdrant_client.http import models as qdrant_models

from nexus.config import logger, mcp
from nexus.dedup import content_hash
from nexus.backends import neo4j as neo4j_backend
from nexus.backends import qdrant as qdrant_backend
from nexus.indexes import get_graph_index, get_vector_index


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
) -> str:
    """Ingest a document into the Multi-Tenant GraphRAG memory.

    Skips ingestion if identical content has already been stored for this
    project+scope combination.

    Args:
        text: The content of the document to ingest.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS').
        source_identifier: Optional identifier for the source of the document.

    Returns:
        Status: 'Successfully ingested', 'Skipped (duplicate)', or error.
    """
    err = _validate_ingest_inputs(text, project_id, scope)
    if err:
        return err

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
async def get_graph_context(query: str, project_id: str, scope: str) -> str:
    """Retrieve isolated context from the GraphRAG memory.

    Args:
        query: The user's query.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS').

    Returns:
        Structured context relevant to the specific project and scope.
    """
    logger.info(f"Graph retrieve: project={project_id} scope={scope} query={query!r}")
    try:
        index = get_graph_index()
        filters = MetadataFilters(
            filters=[
                ExactMatchFilter(key="project_id", value=project_id),
                ExactMatchFilter(key="tenant_scope", value=scope),
            ]
        )
        nodes = index.as_retriever(filters=filters).retrieve(query)
        if not nodes:
            return f"No Graph context found for {project_id} in scope {scope} for query: '{query}'"
        context_str = "\n".join([f"- {n.node.get_content()}" for n in nodes])
        return f"Graph Context retrieved for {project_id} in scope {scope}:\n{context_str}"
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
) -> str:
    """Ingest a document into the Multi-Tenant standard RAG (Vector) memory.

    Skips ingestion if identical content has already been stored for this
    project+scope combination.

    Args:
        text: The content of the document to ingest.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS').
        source_identifier: Optional identifier for the source of the document.

    Returns:
        Status: 'Successfully ingested', 'Skipped (duplicate)', or error.
    """
    err = _validate_ingest_inputs(text, project_id, scope)
    if err:
        return err

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
async def get_vector_context(query: str, project_id: str, scope: str) -> str:
    """Retrieve isolated context from the standard RAG (Vector) memory.

    Args:
        query: The user's query.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS').

    Returns:
        Structured context relevant to the specific project and scope.
    """
    logger.info(f"Vector retrieve: project={project_id} scope={scope} query={query!r}")
    try:
        index = get_vector_index()
        filters = MetadataFilters(
            filters=[
                ExactMatchFilter(key="project_id", value=project_id),
                ExactMatchFilter(key="tenant_scope", value=scope),
            ]
        )
        nodes = index.as_retriever(filters=filters).retrieve(query)
        if not nodes:
            return f"No Vector context found for {project_id} in scope {scope} for query: '{query}'"
        context_str = "\n".join([f"- {n.node.get_content()}" for n in nodes])
        return f"Vector Context retrieved for {project_id} in scope {scope}:\n{context_str}"
    except Exception as e:
        logger.error(f"Error retrieving Vector context: {e}")
        return f"Error retrieving Vector context: {e}"


# ---------------------------------------------------------------------------
# Metadata / admin tools
# ---------------------------------------------------------------------------

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

    label = f"project '{project_id}'"
    if scope:
        label += f", scope '{scope}'"
    if errors:
        return f"Partial failure deleting {label}: {'; '.join(errors)}"
    return f"Successfully deleted data for {label}"
