# Version: v1.0
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
DEFAULT_NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password123")
DEFAULT_QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
DEFAULT_EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
DEFAULT_LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.1:8b")

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
