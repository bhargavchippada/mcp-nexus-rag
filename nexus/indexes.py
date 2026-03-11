# Version: v3.0
"""
nexus.indexes — LlamaIndex settings bootstrap and index factories.

v3.0: Migrated from Neo4j + Qdrant to Memgraph + pgvector.
"""

import threading

import nest_asyncio
from llama_index.core import PropertyGraphIndex, Settings, VectorStoreIndex
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.graph_stores.memgraph import MemgraphPropertyGraphStore
from llama_index.llms.ollama import Ollama
from llama_index.vector_stores.postgres import PGVectorStore

from nexus.config import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_EMBED_MODEL,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_MEMGRAPH_PASSWORD,
    DEFAULT_MEMGRAPH_URL,
    DEFAULT_MEMGRAPH_USER,
    DEFAULT_OLLAMA_URL,
    DEFAULT_PG_DATABASE,
    DEFAULT_PG_HOST,
    DEFAULT_PG_PASSWORD,
    DEFAULT_PG_PORT,
    DEFAULT_PG_USER,
    PG_TABLE_NAME,
    logger,
)

nest_asyncio.apply()

# ---------------------------------------------------------------------------
# Singleton LLM / Embed settings
# ---------------------------------------------------------------------------
_settings_initialized = False
_settings_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Index caching — reuse connections across calls
# ---------------------------------------------------------------------------
_graph_index_cache = None
_vector_index_cache = None
# Separate locks so graph and vector index initialisation can proceed in
# parallel; a shared lock would serialise them unnecessarily.
_graph_index_lock = threading.Lock()
_vector_index_lock = threading.Lock()


def setup_settings() -> None:
    """Initialize LLM and embedding model settings once (thread-safe).

    Uses double-checked locking so concurrent callers block only on the
    very first initialization.
    """
    global _settings_initialized
    if _settings_initialized:
        return
    with _settings_lock:
        if _settings_initialized:
            return
        Settings.llm = Ollama(
            model=DEFAULT_LLM_MODEL,
            base_url=DEFAULT_OLLAMA_URL,
            request_timeout=DEFAULT_LLM_TIMEOUT,
            context_window=DEFAULT_CONTEXT_WINDOW,
        )
        Settings.embed_model = OllamaEmbedding(
            model_name=DEFAULT_EMBED_MODEL,
            base_url=DEFAULT_OLLAMA_URL,
        )
        Settings.node_parser = SentenceSplitter(
            chunk_size=DEFAULT_CHUNK_SIZE,
            chunk_overlap=DEFAULT_CHUNK_OVERLAP,
        )
        _settings_initialized = True


def get_graph_index() -> PropertyGraphIndex:
    """Return a PropertyGraphIndex connected to the local Memgraph instance.

    Uses a cached instance for performance. Thread-safe via double-checked locking.

    Returns:
        PropertyGraphIndex instance.
    """
    global _graph_index_cache
    if _graph_index_cache is not None:
        return _graph_index_cache

    with _graph_index_lock:
        if _graph_index_cache is not None:
            return _graph_index_cache

        setup_settings()
        graph_store = MemgraphPropertyGraphStore(
            username=DEFAULT_MEMGRAPH_USER,
            password=DEFAULT_MEMGRAPH_PASSWORD,
            url=DEFAULT_MEMGRAPH_URL,
        )
        try:
            _graph_index_cache = PropertyGraphIndex.from_existing(
                property_graph_store=graph_store,
                embed_model=Settings.embed_model,
                llm=Settings.llm,
            )
        except Exception as e:
            logger.warning(
                f"Could not load existing Graph index: {e}. Creating empty index."
            )
            _graph_index_cache = PropertyGraphIndex.from_documents(
                [],
                property_graph_store=graph_store,
                embed_model=Settings.embed_model,
                llm=Settings.llm,
            )
        return _graph_index_cache


def get_vector_index() -> VectorStoreIndex:
    """Return a VectorStoreIndex backed by pgvector in PostgreSQL.

    Uses a cached instance for performance. Thread-safe via double-checked locking.

    Returns:
        VectorStoreIndex instance.
    """
    global _vector_index_cache
    if _vector_index_cache is not None:
        return _vector_index_cache

    with _vector_index_lock:
        if _vector_index_cache is not None:
            return _vector_index_cache

        setup_settings()
        vector_store = PGVectorStore.from_params(
            host=DEFAULT_PG_HOST,
            port=str(DEFAULT_PG_PORT),
            database=DEFAULT_PG_DATABASE,
            user=DEFAULT_PG_USER,
            password=DEFAULT_PG_PASSWORD,
            table_name=PG_TABLE_NAME,
            embed_dim=768,  # nomic-embed-text dimension
            hnsw_kwargs={
                "hnsw_m": 16,
                "hnsw_ef_construction": 64,
                "hnsw_ef_search": 40,
                "hnsw_dist_method": "vector_cosine_ops",
            },
            use_jsonb=True,
        )
        _vector_index_cache = VectorStoreIndex.from_vector_store(
            vector_store=vector_store
        )
        return _vector_index_cache


def reset_graph_index() -> None:
    """Clear the cached graph index so the next call re-creates it."""
    global _graph_index_cache
    with _graph_index_lock:
        _graph_index_cache = None


def reset_vector_index() -> None:
    """Clear the cached vector index so the next call re-creates it."""
    global _vector_index_cache
    with _vector_index_lock:
        _vector_index_cache = None
