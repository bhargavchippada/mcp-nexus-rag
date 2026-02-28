# Version: v2.3
"""
nexus.config — All constants, logging, and the shared FastMCP instance.
"""

import logging
import os

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Service defaults
# ---------------------------------------------------------------------------
DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_NEO4J_URL = os.environ.get("NEO4J_URL", "bolt://localhost:7687")
DEFAULT_NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
# WARNING: Default password for development only. Set NEO4J_PASSWORD env var in production.
DEFAULT_NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password123")
DEFAULT_QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
DEFAULT_EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
DEFAULT_LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.1:8b")

# ---------------------------------------------------------------------------
# LLM & Text processing defaults
# ---------------------------------------------------------------------------
DEFAULT_LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "300.0"))
DEFAULT_CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "8192"))
DEFAULT_CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "1024"))
DEFAULT_CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "128"))

# ---------------------------------------------------------------------------
# Document ingestion limits
# ---------------------------------------------------------------------------
# Documents exceeding MAX_DOCUMENT_SIZE (bytes) are automatically chunked
MAX_DOCUMENT_SIZE = int(os.environ.get("MAX_DOCUMENT_SIZE", str(512 * 1024)))  # 512KB
# Chunk size/overlap for large document splitting (uses CHUNK_SIZE/OVERLAP if not set)
INGEST_CHUNK_SIZE = int(os.environ.get("INGEST_CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE)))
INGEST_CHUNK_OVERLAP = int(os.environ.get("INGEST_CHUNK_OVERLAP", str(DEFAULT_CHUNK_OVERLAP)))

# ---------------------------------------------------------------------------
# Reranker defaults
# ---------------------------------------------------------------------------
DEFAULT_RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
DEFAULT_RERANKER_TOP_N = int(os.environ.get("RERANKER_TOP_N", "5"))
DEFAULT_RERANKER_CANDIDATE_K = int(os.environ.get("RERANKER_CANDIDATE_K", "20"))
RERANKER_ENABLED = os.environ.get("RERANKER_ENABLED", "true").lower() != "false"

# ---------------------------------------------------------------------------
# Allowlist — prevents Cypher key injection in dynamic MATCH clauses
# ---------------------------------------------------------------------------
ALLOWED_META_KEYS = frozenset({"project_id", "tenant_scope", "source", "content_hash"})

# ---------------------------------------------------------------------------
# Qdrant collection name — single source of truth
# ---------------------------------------------------------------------------
COLLECTION_NAME = "nexus_rag"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-nexus-rag")

# ---------------------------------------------------------------------------
# Shared FastMCP application instance
# ---------------------------------------------------------------------------
mcp = FastMCP("mcp-nexus-rag")
