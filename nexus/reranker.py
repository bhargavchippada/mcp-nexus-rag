# Version: v1.3
"""
nexus.reranker — Singleton reranker with local and remote modes.

Local mode: Loads FlagEmbeddingReranker (bge-reranker-v2-m3) in-process.
Remote mode: Delegates to a shared reranker HTTP microservice (reranker_service.py).

Mode controlled by RERANKER_MODE env var (default: "local" = no behavior change).
Use reset_reranker() in tests to clear the singleton between test cases.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import httpx
from llama_index.core.schema import NodeWithScore, QueryBundle

from nexus.config import (
    DEFAULT_RERANKER_MODEL,
    DEFAULT_RERANKER_TOP_N,
    RERANKER_MODE,
    RERANKER_SERVICE_URL,
    logger,
)

if TYPE_CHECKING:
    from llama_index.postprocessor.flag_embedding_reranker import FlagEmbeddingReranker


class RemoteReranker:
    """Reranker proxy that delegates to the shared reranker HTTP service.

    Implements the same ``postprocess_nodes(nodes, query_bundle)`` interface
    as ``FlagEmbeddingReranker`` so call sites in ``tools.py`` need zero changes.
    """

    def __init__(self, service_url: str, top_n: int = DEFAULT_RERANKER_TOP_N) -> None:
        self._service_url = service_url.rstrip("/")
        self._top_n = top_n
        self._client = httpx.Client(timeout=30.0)

    def postprocess_nodes(
        self,
        nodes: list[NodeWithScore],
        query_bundle: QueryBundle | None = None,
    ) -> list[NodeWithScore]:
        """Rerank nodes via the remote service, mapping scores back by index."""
        if not nodes:
            return []

        query_str = ""
        if query_bundle is not None:
            query_str = query_bundle.query_str

        documents: list[str] = []
        for node in nodes:
            documents.append(node.node.get_content())

        response = self._client.post(
            f"{self._service_url}/rerank",
            json={
                "query": query_str,
                "documents": documents,
                "top_n": self._top_n,
            },
        )
        response.raise_for_status()
        data = response.json()

        results: list[NodeWithScore] = []
        for item in data["results"]:
            idx = item["index"]
            score = item["score"]
            if 0 <= idx < len(nodes):
                original = nodes[idx]
                results.append(NodeWithScore(node=original.node, score=score))

        return results

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()


_reranker: "FlagEmbeddingReranker | RemoteReranker | None" = None
_reranker_lock = threading.Lock()


def get_reranker() -> "FlagEmbeddingReranker | RemoteReranker":
    """Return the process-level reranker singleton.

    In ``local`` mode (default), lazy-loads the FlagEmbeddingReranker model.
    In ``remote`` mode, returns a RemoteReranker proxy that calls the shared
    reranker HTTP service.

    Returns:
        Configured reranker instance (local or remote).

    Raises:
        ImportError: If llama-index-postprocessor-flag-embedding-reranker is not
            installed (local mode only).
        RuntimeError: If the model cannot be loaded (local mode only).
    """
    global _reranker
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:
                if RERANKER_MODE == "remote":
                    logger.info(f"Using remote reranker at {RERANKER_SERVICE_URL}")
                    _reranker = RemoteReranker(
                        service_url=RERANKER_SERVICE_URL,
                        top_n=DEFAULT_RERANKER_TOP_N,
                    )
                else:
                    from llama_index.postprocessor.flag_embedding_reranker import (
                        FlagEmbeddingReranker,
                    )

                    logger.info(
                        f"Loading reranker model: {DEFAULT_RERANKER_MODEL} "
                        f"(top_n={DEFAULT_RERANKER_TOP_N}, fp16=True)"
                    )
                    _reranker = FlagEmbeddingReranker(
                        model=DEFAULT_RERANKER_MODEL,
                        top_n=DEFAULT_RERANKER_TOP_N,
                        use_fp16=True,
                    )
                    logger.info("Reranker model loaded.")
    return _reranker


def reset_reranker() -> None:
    """Clear the cached reranker singleton.

    Intended for use in tests only. Allows each test to inject a fresh mock
    without cross-test contamination. Closes the HTTP client if remote.
    """
    global _reranker
    if isinstance(_reranker, RemoteReranker):
        _reranker.close()
    _reranker = None
