# Version: v1.1
"""
Nexus RAG MCP Server
Provides strict multi-tenant GraphRAG and Standard RAG retrieval isolated by project_id and tenant_scope.
"""
import asyncio
import logging
from typing import Optional
from mcp.server.fastmcp import FastMCP

import qdrant_client
from llama_index.core import Document, PropertyGraphIndex, VectorStoreIndex, Settings
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore
from llama_index.llms.ollama import Ollama
from llama_index.core.vector_stores import ExactMatchFilter, MetadataFilters
from llama_index.vector_stores.qdrant import QdrantVectorStore

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_NEO4J_URL = "bolt://localhost:7687"
DEFAULT_NEO4J_USER = "neo4j"
DEFAULT_NEO4J_PASSWORD = "password123"
DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_LLM_MODEL = "llama3.1:8b"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-nexus-rag")

import nest_asyncio
nest_asyncio.apply()

mcp = FastMCP("mcp-nexus-rag")

def setup_settings():
    llm = Ollama(
        model=DEFAULT_LLM_MODEL,
        base_url=DEFAULT_OLLAMA_URL,
        request_timeout=300.0,
        context_window=8192,
    )
    embed_model = OllamaEmbedding(
        model_name=DEFAULT_EMBED_MODEL,
        base_url=DEFAULT_OLLAMA_URL,
    )
    
    Settings.llm = llm
    Settings.embed_model = embed_model
    Settings.node_parser = SentenceSplitter(
        chunk_size=1024,
        chunk_overlap=128,
    )

def get_graph_index() -> PropertyGraphIndex:
    setup_settings()
    graph_store = Neo4jPropertyGraphStore(
        username=DEFAULT_NEO4J_USER,
        password=DEFAULT_NEO4J_PASSWORD,
        url=DEFAULT_NEO4J_URL,
    )
    
    try:
        index = PropertyGraphIndex.from_existing(
            property_graph_store=graph_store,
            embed_model=Settings.embed_model,
            llm=Settings.llm,
        )
        return index
    except Exception as e:
        logger.warning(f"Could not load existing Graph index: {e}. Will create empty index.")
        return PropertyGraphIndex.from_documents(
            [],
            property_graph_store=graph_store,
            embed_model=Settings.embed_model,
            llm=Settings.llm,
        )

def get_vector_index() -> VectorStoreIndex:
    setup_settings()
    client = qdrant_client.QdrantClient(url=DEFAULT_QDRANT_URL)
    vector_store = QdrantVectorStore(client=client, collection_name="nexus_rag")
    
    try:
        index = VectorStoreIndex.from_vector_store(vector_store=vector_store)
        return index
    except Exception as e:
        logger.warning(f"Could not load existing Vector index: {e}. Will create empty index.")
        return VectorStoreIndex.from_documents([], vector_store=vector_store)

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
                ExactMatchFilter(key="tenant_scope", value=scope)
            ]
        )
        
        retriever = index.as_retriever(filters=filters)
        nodes = retriever.retrieve(query)
        
        if not nodes:
            return f"No Graph context found for {project_id} in scope {scope} for query: '{query}'"
            
        context_str = "\n".join([f"- {n.node.get_content()}" for n in nodes])
        return f"Graph Context retrieved for {project_id} in scope {scope}:\n{context_str}"
    except Exception as e:
        logger.error(f"Error retrieving Graph context: {e}")
        return f"Error retrieving Graph context: {e}"

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
                ExactMatchFilter(key="tenant_scope", value=scope)
            ]
        )
        
        retriever = index.as_retriever(filters=filters)
        nodes = retriever.retrieve(query)
        
        if not nodes:
            return f"No Vector context found for {project_id} in scope {scope} for query: '{query}'"
            
        context_str = "\n".join([f"- {n.node.get_content()}" for n in nodes])
        return f"Vector Context retrieved for {project_id} in scope {scope}:\n{context_str}"
    except Exception as e:
        logger.error(f"Error retrieving Vector context: {e}")
        return f"Error retrieving Vector context: {e}"

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

def main():
    """Run the MCP server via standard stdio transport."""
    mcp.run()

if __name__ == "__main__":
    main()
