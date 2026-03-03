# Version: v1.7
"""
HTTP API server for Nexus RAG.

Exposes the MCP tools via HTTP endpoints for use by web applications
like mission-control that cannot use the stdio-based MCP protocol directly.

Run with: uvicorn http_server:app --host 0.0.0.0 --port 8765
"""

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Import the actual tool implementations
# Note: nest_asyncio removed - conflicts with uvloop used by uvicorn
from nexus.tools import (
    get_vector_context,
    get_graph_context,
    answer_query,
    health_check,
    get_all_project_ids,
    get_all_tenant_scopes,
)


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
    if "No " in context_str and "context found" in context_str:
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
                current_result["text"] = current_result["text"][:500]
                results.append(current_result)

            # Start new result
            try:
                score = float(match.group(1))
            except ValueError:
                score = 0.0
            current_result = {
                "text": match.group(2),
                "score": score,
                "project_id": default_project,
                "scope": default_scope,
                "source": "nexus-rag",
            }
        elif current_result:
            # Append to current result's text (multiline content)
            current_result["text"] += " " + line

    # Don't forget the last result
    if current_result:
        current_result["text"] = current_result["text"][:500]
        results.append(current_result)

    return results


@app.post("/query", response_model=QueryResponse)
async def http_query(request: QueryRequest):
    """Query both vector and graph stores with optional synthesis."""
    from nexus.config import logger

    project_id = request.project_id or "AGENT"
    scope = request.scope or ""

    # Get available scopes if none specified
    scopes_to_query = [scope] if scope else []
    if not scopes_to_query:
        all_scopes = await get_all_tenant_scopes(project_id=project_id)
        scopes_to_query = list(all_scopes) if all_scopes else ["CORE_CODE"]

    # Query all scopes concurrently
    # max_chars=0 disables truncation since we parse into structured results
    vector_tasks = [
        get_vector_context(
            query=request.query,
            project_id=project_id,
            scope=s,
            rerank=request.rerank,
            max_chars=0,
        )
        for s in scopes_to_query
    ]
    graph_tasks = [
        get_graph_context(
            query=request.query,
            project_id=project_id,
            scope=s,
            rerank=request.rerank,
            max_chars=0,
        )
        for s in scopes_to_query
    ]

    all_results = await asyncio.gather(
        *vector_tasks, *graph_tasks, return_exceptions=True
    )

    # Split results
    num_scopes = len(scopes_to_query)
    vector_results_raw = all_results[:num_scopes]
    graph_results_raw = all_results[num_scopes:]

    # Parse vector results from all scopes
    vector_results = []
    for s, result in zip(scopes_to_query, vector_results_raw):
        if isinstance(result, Exception):
            logger.warning(f"Vector context task failed for scope {s!r}: {result}")
            continue
        # Skip tool-level error strings (start with "Error"), pass all other content
        if isinstance(result, str) and not result.startswith("Error"):
            parsed = _parse_context_results(result, project_id, s)
            vector_results.extend([VectorResult(**r) for r in parsed])

    # Parse graph results from all scopes
    graph_results = []
    for s, result in zip(scopes_to_query, graph_results_raw):
        if isinstance(result, Exception):
            logger.warning(f"Graph context task failed for scope {s!r}: {result}")
            continue
        if isinstance(result, str) and not result.startswith("Error"):
            parsed = _parse_context_results(result, project_id, s)
            graph_results.extend([GraphResult(**r) for r in parsed])

    # Sort by score descending (highest relevance first)
    vector_results.sort(key=lambda x: x.score, reverse=True)
    graph_results.sort(key=lambda x: x.score, reverse=True)

    # Optional synthesis (answer_query handles empty scope by searching all)
    synthesis = None
    if request.synthesize:
        try:
            synthesis = await answer_query(
                query=request.query,
                project_id=project_id,
                scope=request.scope or "",  # Empty = search all scopes
                rerank=request.rerank,
            )
            # Clean up error messages
            if synthesis and synthesis.startswith("Error"):
                synthesis = None
        except Exception:
            synthesis = None

    return QueryResponse(
        query=request.query,
        project_id=request.project_id,
        vector_results=vector_results,
        graph_results=graph_results,
        synthesis=synthesis,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8765)
