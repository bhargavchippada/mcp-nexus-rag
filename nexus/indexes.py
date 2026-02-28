# Version: v2.1
"""
nexus.indexes — LlamaIndex settings bootstrap and index factories.
"""

import threading

from llama_index.core import PropertyGraphIndex, VectorStoreIndex, Settings
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.graph_stores.neo4j import Neo4jPropertyGraphStore
from llama_index.llms.ollama import Ollama
from llama_index.vector_stores.qdrant import QdrantVectorStore

from nexus.backends.qdrant import get_client as get_qdrant_client, get_async_client as get_async_qdrant_client

from nexus.config import (
    DEFAULT_OLLAMA_URL,
    DEFAULT_NEO4J_URL,
    DEFAULT_NEO4J_USER,
    DEFAULT_NEO4J_PASSWORD,
    DEFAULT_QDRANT_URL,
    DEFAULT_EMBED_MODEL,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNK_OVERLAP,
    COLLECTION_NAME,
    logger,
)

import nest_asyncio

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
_index_cache_lock = threading.Lock()


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
    """Return a PropertyGraphIndex connected to the local Neo4j instance.

    Uses a cached instance for performance. Thread-safe via double-checked locking.
    Loads an existing index if available, otherwise creates an empty one.

    Returns:
        PropertyGraphIndex instance.
    """
    global _graph_index_cache
    if _graph_index_cache is not None:
        return _graph_index_cache

    with _index_cache_lock:
        if _graph_index_cache is not None:
            return _graph_index_cache

        setup_settings()
        graph_store = Neo4jPropertyGraphStore(
            username=DEFAULT_NEO4J_USER,
            password=DEFAULT_NEO4J_PASSWORD,
            url=DEFAULT_NEO4J_URL,
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
    """Return a VectorStoreIndex backed by the local Qdrant collection.

    Uses a cached instance for performance. Thread-safe via double-checked locking.

    Returns:
        VectorStoreIndex instance.
    """
    global _vector_index_cache
    if _vector_index_cache is not None:
        return _vector_index_cache

    with _index_cache_lock:
        if _vector_index_cache is not None:
            return _vector_index_cache

        setup_settings()
        client = get_qdrant_client(url=DEFAULT_QDRANT_URL)
        aclient = get_async_qdrant_client(url=DEFAULT_QDRANT_URL)
        vector_store = QdrantVectorStore(
            client=client, aclient=aclient, collection_name=COLLECTION_NAME
        )
        _vector_index_cache = VectorStoreIndex.from_vector_store(vector_store=vector_store)
        return _vector_index_cache
