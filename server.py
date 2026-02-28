# Version: v1.5
"""
Nexus RAG MCP Server
Provides strict multi-tenant GraphRAG and Standard RAG retrieval isolated by project_id and tenant_scope.
"""
import logging
import threading
from typing import Optional

import qdrant_client
from qdrant_client.http import models as qdrant_models
from llama_index.core import Document, PropertyGraphIndex, VectorStoreIndex, Settings
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore
from llama_index.llms.ollama import Ollama
from llama_index.core.vector_stores import ExactMatchFilter, MetadataFilters
from llama_index.vector_stores.qdrant import QdrantVectorStore
from mcp.server.fastmcp import FastMCP
from neo4j import GraphDatabase

import nest_asyncio
nest_asyncio.apply()

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_NEO4J_URL = "bolt://localhost:7687"
DEFAULT_NEO4J_USER = "neo4j"
DEFAULT_NEO4J_PASSWORD = "password123"
DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_LLM_MODEL = "llama3.1:8b"

# Allowed metadata keys for safe Cypher queries — prevents key injection
_ALLOWED_META_KEYS = frozenset({"project_id", "tenant_scope", "source"})

COLLECTION_NAME = "nexus_rag"  # single source-of-truth for the Qdrant collection name

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-nexus-rag")

mcp = FastMCP("mcp-nexus-rag")

# --- Singleton LLM / Embed settings ---
_settings_initialized = False
_settings_lock = threading.Lock()

def setup_settings():
    """Initialize LLM and embedding model settings once (thread-safe)."""
    global _settings_initialized
    if _settings_initialized:
        return
    with _settings_lock:
        if _settings_initialized:  # double-checked locking
            return
        Settings.llm = Ollama(
            model=DEFAULT_LLM_MODEL,
            base_url=DEFAULT_OLLAMA_URL,
            request_timeout=300.0,
            context_window=8192,
        )
        Settings.embed_model = OllamaEmbedding(
            model_name=DEFAULT_EMBED_MODEL,
            base_url=DEFAULT_OLLAMA_URL,
        )
        Settings.node_parser = SentenceSplitter(chunk_size=1024, chunk_overlap=128)
        _settings_initialized = True


# --- Index factories ---

def get_graph_index() -> PropertyGraphIndex:
    setup_settings()
    graph_store = Neo4jPropertyGraphStore(
        username=DEFAULT_NEO4J_USER,
        password=DEFAULT_NEO4J_PASSWORD,
        url=DEFAULT_NEO4J_URL,
    )
    try:
        return PropertyGraphIndex.from_existing(
            property_graph_store=graph_store,
            embed_model=Settings.embed_model,
            llm=Settings.llm,
        )
    except Exception as e:
        logger.warning(f"Could not load existing Graph index: {e}. Creating empty index.")
        return PropertyGraphIndex.from_documents(
            [],
            property_graph_store=graph_store,
            embed_model=Settings.embed_model,
            llm=Settings.llm,
        )


def get_vector_index() -> VectorStoreIndex:
    setup_settings()
    client = qdrant_client.QdrantClient(url=DEFAULT_QDRANT_URL)
    vector_store = QdrantVectorStore(client=client, collection_name=COLLECTION_NAME)
    return VectorStoreIndex.from_vector_store(vector_store=vector_store)


# --- Neo4j helpers ---

def _neo4j_driver():
    """Return a new Neo4j driver configured for use as a context manager."""
    return GraphDatabase.driver(DEFAULT_NEO4J_URL, auth=(DEFAULT_NEO4J_USER, DEFAULT_NEO4J_PASSWORD))


def get_distinct_metadata_neo4j(key: str) -> list[str]:
    """Return distinct values for *key* across all Neo4j nodes.

    Args:
        key: A metadata key (must be in _ALLOWED_META_KEYS).

    Returns:
        List of unique string values.
    """
    if key not in _ALLOWED_META_KEYS:
        raise ValueError(f"Disallowed metadata key: {key!r}")
    try:
        with _neo4j_driver() as driver:
            with driver.session() as session:
                result = session.run(
                    f"MATCH (n) WHERE n.{key} IS NOT NULL RETURN DISTINCT n.{key} AS value"
                )
                return [record["value"] for record in result]
    except Exception as e:
        logger.warning(f"Neo4j distinct '{key}' error: {e}")
        return []


def delete_data_neo4j(project_id: str, scope: str = "") -> None:
    """Delete Neo4j nodes matching project_id (and optionally scope).

    Args:
        project_id: Tenant project ID to target.
        scope: If non-empty, restricts deletion to this tenant_scope.
    """
    if scope:
        cypher = "MATCH (n {project_id: $project_id, tenant_scope: $scope}) DETACH DELETE n"
        params = {"project_id": project_id, "scope": scope}
    else:
        cypher = "MATCH (n {project_id: $project_id}) DETACH DELETE n"
        params = {"project_id": project_id}
    try:
        with _neo4j_driver() as driver:
            with driver.session() as session:
                session.run(cypher, **params)
    except Exception as e:
        logger.error(f"Neo4j delete error: {e}")


# --- Qdrant helpers ---

def _scroll_qdrant_field(
    key: str,
    qdrant_filter: Optional[qdrant_models.Filter] = None,
) -> set[str]:
    """Scroll the entire nexus_rag collection and collect distinct values for *key*.

    Args:
        key: Payload field name to collect.
        qdrant_filter: Optional Qdrant filter to restrict which points are scanned.

    Returns:
        Set of unique string values found in the payload.
    """
    values: set[str] = set()
    client = qdrant_client.QdrantClient(url=DEFAULT_QDRANT_URL)
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=qdrant_filter,
            limit=1000,
            with_payload=[key],
            offset=offset,
        )
        for record in records:
            if record.payload and key in record.payload:
                values.add(record.payload[key])
        if offset is None:
            break
    return values


def get_distinct_metadata_qdrant(key: str) -> list[str]:
    """Return distinct payload values for *key* across the Qdrant collection.

    Args:
        key: Payload field name (must be in _ALLOWED_META_KEYS).

    Returns:
        List of unique string values.
    """
    if key not in _ALLOWED_META_KEYS:
        raise ValueError(f"Disallowed metadata key: {key!r}")
    try:
        return list(_scroll_qdrant_field(key))
    except Exception as e:
        logger.warning(f"Qdrant distinct '{key}' error: {e}")
        return []


def delete_data_qdrant(project_id: str, scope: str = "") -> None:
    """Delete Qdrant points matching project_id (and optionally scope).

    Args:
        project_id: Tenant project ID to target.
        scope: If non-empty, restricts deletion to this tenant_scope.
    """
    try:
        client = qdrant_client.QdrantClient(url=DEFAULT_QDRANT_URL)
        must_conditions: list = [
            qdrant_models.FieldCondition(
                key="project_id",
                match=qdrant_models.MatchValue(value=project_id),
            )
        ]
        if scope:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="tenant_scope",
                    match=qdrant_models.MatchValue(value=scope),
                )
            )
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=qdrant_models.FilterSelector(
                filter=qdrant_models.Filter(must=must_conditions)
            ),
        )
    except Exception as e:
        logger.error(f"Qdrant delete error: {e}")
        raise


# --- MCP Tools ---

@mcp.tool()
async def ingest_graph_document(text: str, project_id: str, scope: str, source_identifier: str = "manual") -> str:
    """
    Ingest a document into the Multi-Tenant GraphRAG memory.

    Args:
        text: The content of the document to ingest.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT', 'WEB_PORTAL').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS', 'WEB_RESEARCH').
        source_identifier: An optional identifier for the source of the document.

    Returns:
        Status message about the ingestion.
    """
    logger.info(f"Ingesting Graph document for project: {project_id}, scope: {scope}")
    try:
        index = get_graph_index()
        doc = Document(
            text=text,
            metadata={
                "project_id": project_id,
                "tenant_scope": scope,
                "source": source_identifier,
            }
        )
        index.insert(doc)
        return f"Successfully ingested Graph document for {project_id} in scope {scope}."
    except Exception as e:
        logger.error(f"Error ingesting Graph document: {e}")
        return f"Error ingesting Graph document: {e}"


@mcp.tool()
async def get_graph_context(query: str, project_id: str, scope: str) -> str:
    """
    Retrieve isolated context from the GraphRAG memory.

    Args:
        query: The user's query.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS').

    Returns:
        Structured context relevant to the specific project and scope.
    """
    logger.info(f"Retrieving Graph context for project: {project_id}, scope: {scope}, query: {query}")
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


@mcp.tool()
async def ingest_vector_document(text: str, project_id: str, scope: str, source_identifier: str = "manual") -> str:
    """
    Ingest a document into the Multi-Tenant standard RAG (Vector) memory.

    Args:
        text: The content of the document to ingest.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT', 'WEB_PORTAL').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS', 'WEB_RESEARCH').
        source_identifier: An optional identifier for the source of the document.

    Returns:
        Status message about the ingestion.
    """
    logger.info(f"Ingesting Vector document for project: {project_id}, scope: {scope}")
    try:
        index = get_vector_index()
        doc = Document(
            text=text,
            metadata={
                "project_id": project_id,
                "tenant_scope": scope,
                "source": source_identifier,
            }
        )
        index.insert(doc)
        return f"Successfully ingested Vector document for {project_id} in scope {scope}."
    except Exception as e:
        logger.error(f"Error ingesting Vector document: {e}")
        return f"Error ingesting Vector document: {e}"


@mcp.tool()
async def get_vector_context(query: str, project_id: str, scope: str) -> str:
    """
    Retrieve isolated context from the standard RAG (Vector) memory.

    Args:
        query: The user's query.
        project_id: The target tenant project ID (e.g., 'TRADING_BOT').
        scope: The retrieval scope (e.g., 'CORE_CODE', 'SYSTEM_LOGS').

    Returns:
        Structured context relevant to the specific project and scope.
    """
    logger.info(f"Retrieving Vector context for project: {project_id}, scope: {scope}, query: {query}")
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


@mcp.tool()
async def get_all_project_ids() -> list[str]:
    """
    Retrieve a sorted list of all distinct project IDs across both databases.

    Returns:
        A list of project_id strings.
    """
    logger.info("Retrieving all project IDs")
    graph_ids = get_distinct_metadata_neo4j("project_id")
    vector_ids = get_distinct_metadata_qdrant("project_id")
    return sorted(set(graph_ids + vector_ids))


@mcp.tool()
async def get_all_tenant_scopes(project_id: Optional[str] = None) -> list[str]:
    """
    Retrieve a sorted list of all distinct tenant scopes across both databases.
    If project_id is provided, only scopes belonging to that project are returned.

    Args:
        project_id: Optional. Filter scopes to a specific project.

    Returns:
        A list of tenant_scope strings.
    """
    logger.info(f"Retrieving all tenant scopes (project_id={project_id})")
    if project_id:
        # Filtered: only scopes for this project
        try:
            with _neo4j_driver() as driver:
                with driver.session() as session:
                    result = session.run(
                        "MATCH (n {project_id: $project_id}) WHERE n.tenant_scope IS NOT NULL "
                        "RETURN DISTINCT n.tenant_scope AS value",
                        project_id=project_id,
                    )
                    graph_scopes = [record["value"] for record in result]
        except Exception as e:
            logger.warning(f"Neo4j scopes error: {e}")
            graph_scopes = []

        try:
            vector_scopes = _scroll_qdrant_field(
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
        graph_scopes = get_distinct_metadata_neo4j("tenant_scope")
        vector_scopes = get_distinct_metadata_qdrant("tenant_scope")
        return sorted(set(graph_scopes + vector_scopes))


@mcp.tool()
async def delete_tenant_data(project_id: str, scope: str = "") -> str:
    """
    Delete all data (both Graph and Vector) for a given project_id.
    If scope is provided, only data matching BOTH project_id and scope is deleted.

    Args:
        project_id: The target tenant project ID.
        scope: Optional. Restricts deletion to this scope. Empty means delete the full project.

    Returns:
        Confirmation message, or error message if a backend failed.
    """
    logger.info(f"Deleting data for project_id={project_id}, scope={scope!r}")
    errors: list[str] = []
    try:
        delete_data_neo4j(project_id, scope)
    except Exception as e:
        errors.append(f"Neo4j: {e}")
    try:
        delete_data_qdrant(project_id, scope)
    except Exception as e:
        errors.append(f"Qdrant: {e}")

    label = f"project '{project_id}'"
    if scope:
        label += f", scope '{scope}'"
    if errors:
        return f"Partial failure deleting {label}: {'; '.join(errors)}"
    return f"Successfully deleted data for {label}"


def main():
    """Run the MCP server via standard stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
