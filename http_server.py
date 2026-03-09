# Version: v2.1
"""
HTTP API server for Nexus RAG.

Exposes the MCP tools via HTTP endpoints for use by web applications
like mission-control that cannot use the stdio-based MCP protocol directly.

Run with: uvicorn http_server:app --host 0.0.0.0 --port 8765
"""

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Import the actual tool implementations
# Note: nest_asyncio removed - conflicts with uvloop used by uvicorn
from nexus.tools import (
    answer_query,
    get_all_project_ids,
    get_all_tenant_scopes,
    get_graph_context,
    get_vector_context,
    health_check,
)

# Per-task timeout (seconds) for individual retrieval calls.
# Graph context is the bottleneck: PropertyGraphIndex → Ollama LLM (Cypher gen)
# → Neo4j → Ollama LLM (synthesis).  Two sequential LLM calls can take 30-60s
# on cold model.  Synthesis (answer_query) adds another LLM call on top.
_RETRIEVAL_TIMEOUT = 60  # seconds per vector/graph scope query
_SYNTHESIS_TIMEOUT = 90  # seconds for answer_query (includes its own retrieval)

# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    """Request body for /query endpoint."""

    query: str = Field(..., description="The user's query")
    project_id: Optional[str] = Field(
        None, description="Project ID filter (optional, defaults to all)"
    )
    scope: Optional[str] = Field(
        None, description="Scope filter (optional, defaults to all)"
    )
    synthesize: bool = Field(True, description="Whether to generate LLM synthesis")
    rerank: bool = Field(True, description="Whether to apply reranking")


class VectorResult(BaseModel):
    """A single vector search result."""

    text: str
    score: float = 0.0
    project_id: str
    scope: str
    source: str


class GraphResult(BaseModel):
    """A single graph search result."""

    text: str
    score: float = 0.0
    project_id: str
    scope: str
    source: str


class QueryResponse(BaseModel):
    """Response body for /query endpoint."""

    query: str
    project_id: Optional[str]
    vector_results: list[VectorResult]
    graph_results: list[GraphResult]
    synthesis: Optional[str]
    timestamp: str


class HealthResponse(BaseModel):
    """Response body for /health endpoint."""

    neo4j: str
    qdrant: str
    ollama: str
    status: str


class ProjectsResponse(BaseModel):
    """Response body for /projects endpoint."""

    project_ids: list[str]


class ScopesResponse(BaseModel):
    """Response body for /scopes endpoint."""

    scopes: list[str]


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    yield


app = FastAPI(
    title="Nexus RAG HTTP API",
    description="HTTP interface to Nexus RAG MCP tools",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for mission-control
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to specific origins
    allow_credentials=False,  # Must be False when allow_origins=["*"] (CORS spec)
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def http_health_check():
    """Check connectivity to all backends."""
    result = await health_check()
    # health_check() returns a dict with keys: neo4j, qdrant, ollama
    # Values are "ok" or "error: <message>"
    health = {
        "neo4j": "OK" if result.get("neo4j") == "ok" else "ERROR",
        "qdrant": "OK" if result.get("qdrant") == "ok" else "ERROR",
        "ollama": "OK" if result.get("ollama") == "ok" else "ERROR",
    }

    all_ok = all(v == "OK" for v in health.values())
    return HealthResponse(
        neo4j=health["neo4j"],
        qdrant=health["qdrant"],
        ollama=health["ollama"],
        status="healthy" if all_ok else "degraded",
    )


@app.get("/projects", response_model=ProjectsResponse)
async def http_get_projects():
    """Get all available project IDs."""
    result = await get_all_project_ids()
    return ProjectsResponse(project_ids=list(result))


@app.get("/scopes", response_model=ScopesResponse)
async def http_get_scopes(project_id: Optional[str] = None):
    """Get all available tenant scopes, optionally filtered by project."""
    result = await get_all_tenant_scopes(project_id=project_id)
    return ScopesResponse(scopes=list(result))


def _parse_context_results(context_str: str, default_project: str, default_scope: str):
    """Parse context string into structured results.

    Supports format: - [score: X.XXXX] content here
    Only lines with [score:] prefix are treated as separate results.
    Content without score prefix is appended to the previous result.
    """
    results = []
    if not context_str or not context_str.strip():
        return results
    # Guard against "No Vector/Graph context found for ..." response strings.
    # Use startswith to avoid false-positive matches when retrieved document
    # content itself contains the phrase "No ... context found".
    if context_str.startswith("No ") and "context found" in context_str:
        return results

    # Pattern to match: - [score: X.XXXX] content (including negative scores)
    score_pattern = re.compile(r"^-\s*\[score:\s*([-\d.]+)\]\s*(.*)$")

    lines = context_str.split("\n")
    current_result = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Check if this line starts a new result (has score prefix)
        match = score_pattern.match(line)
        if match:
            # Save previous result if exists
            if current_result:
                results.append(current_result)

            # Start new result
            try:
                score = float(match.group(1))
            except ValueError:
                score = 0.0
            current_result = {
                "text": match.group(2)[:500],
                "score": score,
                "project_id": default_project,
                "scope": default_scope,
                "source": "nexus-rag",
            }
        elif current_result:
            # Append to current result's text (multiline content, cap total)
            remaining = 500 - len(current_result["text"])
            if remaining > 0:
                current_result["text"] += " " + line[:remaining]

    # Don't forget the last result
    if current_result:
        results.append(current_result)

    return results


async def _resolve_scopes(project_id: str, scope: str) -> list[str]:
    """Resolve which scopes to query.

    If a specific scope is provided, return it as a single-element list.
    Otherwise, discover all scopes for the project, filtering out empty strings.
    """
    if scope:
        return [scope]
    all_scopes = await get_all_tenant_scopes(project_id=project_id)
    # Filter out empty strings from scope list
    return [s for s in all_scopes if s] or [""]


def _collect_results(scopes, raw_results, model_cls, project_id, logger):
    """Parse raw context strings into typed result models."""
    results = []
    label = model_cls.__name__
    for s, result in zip(scopes, raw_results):
        if isinstance(result, Exception):
            if isinstance(result, asyncio.TimeoutError):
                logger.warning(
                    f"{label} timed out after {_RETRIEVAL_TIMEOUT}s for scope {s!r}"
                )
            else:
                logger.warning(f"{label} task failed for scope {s!r}: {result}")
            continue
        if isinstance(result, str) and not result.startswith("Error"):
            parsed = _parse_context_results(result, project_id, s)
            results.extend([model_cls(**r) for r in parsed])
    results.sort(key=lambda x: x.score, reverse=True)
    return results


async def _synthesize(query: str, project_id: str, scope: str, rerank: bool):
    """Run answer_query synthesis, returning None on any error or timeout."""
    try:
        result = await asyncio.wait_for(
            answer_query(
                query=query,
                project_id=project_id,
                scope=scope,
                rerank=rerank,
            ),
            timeout=_SYNTHESIS_TIMEOUT,
        )
        if result and result.startswith("Error"):
            return None
        return result
    except asyncio.TimeoutError:
        from nexus.config import logger

        logger.warning(
            f"Synthesis timed out after {_SYNTHESIS_TIMEOUT}s for query={query!r}"
        )
        return None
    except Exception:
        return None


@app.post("/query", response_model=QueryResponse)
async def http_query(request: QueryRequest):
    """Query both vector and graph stores with optional synthesis."""
    from nexus.config import logger

    project_id = request.project_id or "AGENT"
    scope = request.scope or ""

    scopes_to_query = await _resolve_scopes(project_id, scope)

    # Query all scopes concurrently with per-task timeout.
    # max_chars=0 disables truncation since we parse into structured results.
    vector_tasks = [
        asyncio.wait_for(
            get_vector_context(
                query=request.query,
                project_id=project_id,
                scope=s,
                rerank=request.rerank,
                max_chars=0,
            ),
            timeout=_RETRIEVAL_TIMEOUT,
        )
        for s in scopes_to_query
    ]
    graph_tasks = [
        asyncio.wait_for(
            get_graph_context(
                query=request.query,
                project_id=project_id,
                scope=s,
                rerank=request.rerank,
                max_chars=0,
            ),
            timeout=_RETRIEVAL_TIMEOUT,
        )
        for s in scopes_to_query
    ]

    all_results = await asyncio.gather(
        *vector_tasks, *graph_tasks, return_exceptions=True
    )

    num_scopes = len(scopes_to_query)
    vector_results = _collect_results(
        scopes_to_query, all_results[:num_scopes], VectorResult, project_id, logger
    )
    graph_results = _collect_results(
        scopes_to_query, all_results[num_scopes:], GraphResult, project_id, logger
    )

    # Optional synthesis
    synthesis = None
    if request.synthesize:
        synthesis = await _synthesize(request.query, project_id, scope, request.rerank)

    return QueryResponse(
        query=request.query,
        project_id=project_id,
        vector_results=vector_results,
        graph_results=graph_results,
        synthesis=synthesis,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8765)
