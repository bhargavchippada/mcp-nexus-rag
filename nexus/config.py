# Version: v2.9
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
DEFAULT_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
DEFAULT_EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
DEFAULT_LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.1:8b")

# ---------------------------------------------------------------------------
# LLM & Text processing defaults
# ---------------------------------------------------------------------------
DEFAULT_LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "300.0"))
DEFAULT_CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "32768"))
DEFAULT_CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "1024"))
DEFAULT_CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "128"))

# Ollama retry settings for transient network failures
# Clamp to safe minimums to avoid invalid runtime behavior from env config.
OLLAMA_RETRY_COUNT = max(1, int(os.environ.get("OLLAMA_RETRY_COUNT", "3")))
OLLAMA_RETRY_BASE_DELAY = max(
    0.0, float(os.environ.get("OLLAMA_RETRY_BASE_DELAY", "1.0"))
)

# ---------------------------------------------------------------------------
# Document ingestion limits
# ---------------------------------------------------------------------------
# Documents exceeding MAX_DOCUMENT_SIZE (bytes) are automatically chunked.
# Default 4KB so all project docs (README, MEMORY, AGENTS) are chunked into
# focused 1024-char pieces, preventing single giant nodes from flooding Claude's
# context window when retrieved.
MAX_DOCUMENT_SIZE = int(os.environ.get("MAX_DOCUMENT_SIZE", str(4 * 1024)))  # 4KB
# Chunk size/overlap for large document splitting (uses CHUNK_SIZE/OVERLAP if not set)
INGEST_CHUNK_SIZE = int(os.environ.get("INGEST_CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE)))
INGEST_CHUNK_OVERLAP = int(
    os.environ.get("INGEST_CHUNK_OVERLAP", str(DEFAULT_CHUNK_OVERLAP))
)

# ---------------------------------------------------------------------------
# Reranker defaults
# ---------------------------------------------------------------------------
DEFAULT_RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
DEFAULT_RERANKER_TOP_N = int(os.environ.get("RERANKER_TOP_N", "5"))
DEFAULT_RERANKER_CANDIDATE_K = int(os.environ.get("RERANKER_CANDIDATE_K", "20"))
RERANKER_ENABLED = os.environ.get("RERANKER_ENABLED", "true").lower() != "false"

# ---------------------------------------------------------------------------
# Context retrieval output size limit
# ---------------------------------------------------------------------------
# Server-side hard cap on chars returned by get_vector_context / get_graph_context.
# Applied to BOTH fresh retrievals AND cache hits.
# 1500 chars ≈ 375 tokens — keeps retrieval tool responses small.
# Set to 0 to disable (not recommended in production).
MAX_CONTEXT_CHARS = int(os.environ.get("MAX_CONTEXT_CHARS", "1500"))

# Maximum allowed value for answer_query's max_context_chars parameter.
# Prevents callers from requesting excessively large contexts that could
# blow up memory or token budgets. 24000 chars ≈ 6000 tokens.
MAX_ANSWER_CONTEXT_LIMIT = int(os.environ.get("MAX_ANSWER_CONTEXT_LIMIT", "24000"))

# ---------------------------------------------------------------------------
# Allowlist — prevents Cypher key injection in dynamic MATCH clauses
# ---------------------------------------------------------------------------
ALLOWED_META_KEYS = frozenset(
    {"project_id", "tenant_scope", "source", "content_hash", "file_path"}
)

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


# ---------------------------------------------------------------------------
# Startup config validation
# ---------------------------------------------------------------------------
_PROD_ENVS = {"production", "prod"}


def validate_config() -> list[str]:
    """Return a list of configuration warnings for operator review.

    Checks for obviously unsafe defaults that should be changed before
    deploying in production. Issues are logged at WARNING level by the
    server entry point at startup.

    In strict mode (``NEXUS_ENV=production``), the server will raise
    ``RuntimeError`` on the first critical issue rather than just logging.

    Returns:
        List of human-readable warning strings (empty = config is clean).
    """
    warnings: list[str] = []

    # Detect default Neo4j password — most likely misconfiguration in production
    if DEFAULT_NEO4J_PASSWORD == "password123":  # nosec B105
        warnings.append(
            "NEO4J_PASSWORD is using the insecure default value 'password123'. "
            "Set a strong password via the NEO4J_PASSWORD environment variable."
        )

    # Localhost service URLs in a production-flagged environment
    is_production = os.environ.get("NEXUS_ENV", "").lower() in _PROD_ENVS
    if is_production:
        for label, url in [
            ("NEO4J_URL", DEFAULT_NEO4J_URL),
            ("QDRANT_URL", DEFAULT_QDRANT_URL),
            ("OLLAMA_URL", DEFAULT_OLLAMA_URL),
        ]:
            if "localhost" in url or "127.0.0.1" in url:
                warnings.append(
                    f"{label}={url!r} points to localhost — "
                    "production deployments should use remote service URLs."
                )

    return warnings
