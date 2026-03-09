# Version: v1.0
"""
reranker_service.py — Shared reranker HTTP microservice (port 8767).

Holds a single FlagEmbeddingReranker (bge-reranker-v2-m3, ~2 GB FP16) in GPU
memory and serves reranking requests over HTTP. Both server.py (MCP stdio) and
http_server.py (:8766) can use this via RERANKER_MODE=remote, saving ~2 GB VRAM
compared to each loading the model independently.

Run with: uvicorn reranker_service:app --host 0.0.0.0 --port 8767
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
DEFAULT_TOP_N = int(os.environ.get("RERANKER_TOP_N", "5"))

logger = logging.getLogger("reranker-service")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Model holder
# ---------------------------------------------------------------------------

_reranker = None


def _load_model():
    """Eagerly load the cross-encoder model into GPU."""
    global _reranker
    from llama_index.postprocessor.flag_embedding_reranker import (
        FlagEmbeddingReranker,
    )

    logger.info(f"Loading reranker model: {RERANKER_MODEL} (fp16=True)")
    _reranker = FlagEmbeddingReranker(
        model=RERANKER_MODEL,
        top_n=DEFAULT_TOP_N,
        use_fp16=True,
    )
    logger.info("Reranker model loaded and ready.")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the model on startup, cleanup on shutdown."""
    _load_model()
    yield
    logger.info("Reranker service shutting down.")


app = FastAPI(
    title="Nexus Reranker Service",
    version="1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class RerankRequest(BaseModel):
    """POST /rerank request body."""

    query: str = Field(..., description="The query to rerank documents against")
    documents: list[str] = Field(..., description="Documents to rerank")
    top_n: int = Field(DEFAULT_TOP_N, description="Number of top results to return")


class RerankResultItem(BaseModel):
    """A single reranked result."""

    index: int
    score: float
    text: str


class RerankResponse(BaseModel):
    """POST /rerank response body."""

    results: list[RerankResultItem]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/rerank", response_model=RerankResponse)
async def rerank(request: RerankRequest):
    """Rerank documents against a query using the cross-encoder model."""
    if _reranker is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    if not request.documents:
        return RerankResponse(results=[])

    from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode

    # Build nodes with original index tracking via metadata
    nodes = []
    for i, doc in enumerate(request.documents):
        text_node = TextNode(text=doc, metadata={"_original_index": i})
        nodes.append(NodeWithScore(node=text_node, score=0.0))

    query_bundle = QueryBundle(query_str=request.query)

    # Override top_n for this request if different from default
    old_top_n = _reranker.top_n
    _reranker.top_n = request.top_n

    try:
        reranked = _reranker.postprocess_nodes(nodes, query_bundle)
    finally:
        _reranker.top_n = old_top_n

    results = []
    for node_with_score in reranked:
        idx = node_with_score.node.metadata.get("_original_index", -1)
        results.append(
            RerankResultItem(
                index=idx,
                score=node_with_score.score,
                text=node_with_score.node.get_content(),
            )
        )

    return RerankResponse(results=results)


@app.get("/health")
async def health():
    """Health check — reports model status."""
    gpu_loaded = _reranker is not None
    return {
        "status": "ok" if gpu_loaded else "loading",
        "model": RERANKER_MODEL,
        "gpu_loaded": gpu_loaded,
    }
