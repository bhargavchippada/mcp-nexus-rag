# Version: v1.0
"""
nexus.reranker — Singleton FlagEmbeddingReranker wrapping bge-reranker-v2-m3.

The reranker is lazy-loaded on first use and cached for the process lifetime.
Use reset_reranker() in tests to clear the singleton between test cases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexus.config import (
    DEFAULT_RERANKER_MODEL,
    DEFAULT_RERANKER_TOP_N,
    logger,
)

if TYPE_CHECKING:
    from llama_index.postprocessor.flag_reranker import FlagEmbeddingReranker

_reranker: "FlagEmbeddingReranker | None" = None


def get_reranker() -> "FlagEmbeddingReranker":
    """Return the process-level FlagEmbeddingReranker singleton.

    Lazy-loads the model on first call. Subsequent calls return the cached
    instance with no I/O overhead.

    Returns:
        Configured FlagEmbeddingReranker instance.

    Raises:
        ImportError: If llama-index-postprocessor-flag-reranker is not installed.
        RuntimeError: If the model cannot be loaded from disk or HuggingFace Hub.
    """
    global _reranker
    if _reranker is None:
        from llama_index.postprocessor.flag_reranker import FlagEmbeddingReranker

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
    without cross-test contamination.
    """
    global _reranker
    _reranker = None
