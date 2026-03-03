# Version: v1.0
"""
HTTP API server for Nexus RAG.

Exposes the MCP tools via HTTP endpoints for use by web applications
like mission-control that cannot use the stdio-based MCP protocol directly.

Run with: uvicorn http_server:app --host 0.0.0.0 --port 8765
"""

import asyncio
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
    allow_credentials=True,
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
    # Parse the health check result string
    lines = result.split("\n")
    health = {"neo4j": "unknown", "qdrant": "unknown", "ollama": "unknown"}
    for line in lines:
        if "neo4j" in line.lower():
            health["neo4j"] = "OK" if "OK" in line else "ERROR"
        elif "qdrant" in line.lower():
            health["qdrant"] = "OK" if "OK" in line else "ERROR"
        elif "ollama" in line.lower():
            health["ollama"] = "OK" if "OK" in line else "ERROR"

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
    # The result is a string like "Available project IDs: ['X', 'Y']"
    # or a list directly depending on implementation
    if isinstance(result, str):
        # Parse from string format
        import re

        match = re.search(r"\[([^\]]*)\]", result)
        if match:
            ids_str = match.group(1)
            project_ids = [
                p.strip().strip("'\"") for p in ids_str.split(",") if p.strip()
            ]
        else:
            project_ids = []
    else:
        project_ids = list(result)

    return ProjectsResponse(project_ids=project_ids)


def _parse_context_results(context_str: str, default_project: str, default_scope: str):
    """Parse context string into structured results."""
    results = []
    if "No " in context_str and "context found" in context_str:
        return results

    lines = context_str.split("\n")
    for line in lines:
        line = line.strip()
        if line.startswith("- "):
            text = line[2:]  # Remove "- " prefix
            results.append(
                {
                    "text": text[:500],  # Limit text length
                    "project_id": default_project,
                    "scope": default_scope,
                    "source": "nexus-rag",
                }
            )
    return results


@app.post("/query", response_model=QueryResponse)
async def http_query(request: QueryRequest):
    """Query both vector and graph stores with optional synthesis."""
    from datetime import datetime, timezone

    project_id = request.project_id or ""
    scope = request.scope or ""

    # Query both backends concurrently
    vector_task = get_vector_context(
        query=request.query,
        project_id=project_id if project_id else "AGENT",  # Default to AGENT
        scope=scope if scope else "CORE_CODE",  # Default scope
        rerank=request.rerank,
    )
    graph_task = get_graph_context(
        query=request.query,
        project_id=project_id if project_id else "AGENT",
        scope=scope if scope else "CORE_CODE",
        rerank=request.rerank,
    )

    vector_result, graph_result = await asyncio.gather(
        vector_task, graph_task, return_exceptions=True
    )

    # Parse results
    vector_results = []
    if isinstance(vector_result, str) and "Error" not in vector_result:
        parsed = _parse_context_results(
            vector_result, project_id or "AGENT", scope or "CORE_CODE"
        )
        vector_results = [VectorResult(**r, score=0.0) for r in parsed]

    graph_results = []
    if isinstance(graph_result, str) and "Error" not in graph_result:
        parsed = _parse_context_results(
            graph_result, project_id or "AGENT", scope or "CORE_CODE"
        )
        graph_results = [GraphResult(**r) for r in parsed]

    # Optional synthesis
    synthesis = None
    if request.synthesize and (vector_results or graph_results):
        try:
            synthesis = await answer_query(
                query=request.query,
                project_id=project_id if project_id else "AGENT",
                scope=scope,
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
