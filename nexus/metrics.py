# Version: v1.0
"""
nexus.metrics — Lightweight performance metrics for ingestion and query tracking.

Collects timing data for:
- File-level ingestion (total, graph, vector breakdown)
- Per-chunk ingestion (graph LLM extraction, vector embedding)
- Query latency (retrieval, synthesis, total)

Metrics are:
1. Logged as structured INFO lines for immediate visibility
2. Appended to a JSONL file for historical analysis
3. Held in memory for summary stats via MCP tools
"""

import json
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from nexus.config import logger

# ---------------------------------------------------------------------------
# JSONL log file — persists across restarts
# ---------------------------------------------------------------------------
_METRICS_DIR = Path(__file__).parent.parent / "metrics"
_METRICS_FILE = _METRICS_DIR / "performance.jsonl"

# ---------------------------------------------------------------------------
# In-memory rolling stats (last N entries per category)
# ---------------------------------------------------------------------------
_MAX_HISTORY = 200
_history: dict[str, list[dict]] = defaultdict(list)


def _ensure_dir() -> None:
    _METRICS_DIR.mkdir(parents=True, exist_ok=True)


def _append_jsonl(entry: dict) -> None:
    """Append a metrics entry to the JSONL file."""
    try:
        _ensure_dir()
        with open(_METRICS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.debug(f"metrics: failed to write JSONL: {e}")


def _store(category: str, entry: dict) -> None:
    """Store in memory + JSONL."""
    buf = _history[category]
    buf.append(entry)
    if len(buf) > _MAX_HISTORY:
        buf[:] = buf[-_MAX_HISTORY:]
    _append_jsonl(entry)


# ---------------------------------------------------------------------------
# Timer context manager
# ---------------------------------------------------------------------------


@contextmanager
def timer():
    """Yield an object with `.elapsed_ms` populated on exit."""

    class _Timer:
        elapsed_ms: float = 0.0

    t = _Timer()
    start = time.monotonic()
    try:
        yield t
    finally:
        t.elapsed_ms = (time.monotonic() - start) * 1000


# ---------------------------------------------------------------------------
# Ingestion metrics
# ---------------------------------------------------------------------------


def record_file_ingestion(
    *,
    file_path: str,
    project_id: str,
    scope: str,
    total_ms: float,
    graph_ms: float,
    vector_ms: float,
    chunks: int,
    graph_chunks_ingested: int,
    vector_chunks_ingested: int,
) -> None:
    """Record a complete file ingestion event."""
    entry = {
        "type": "file_ingestion",
        "ts": time.time(),
        "file_path": file_path,
        "project_id": project_id,
        "scope": scope,
        "total_ms": round(total_ms, 1),
        "graph_ms": round(graph_ms, 1),
        "vector_ms": round(vector_ms, 1),
        "chunks": chunks,
        "graph_chunks_ingested": graph_chunks_ingested,
        "vector_chunks_ingested": vector_chunks_ingested,
        "avg_chunk_ms": round(total_ms / max(chunks, 1), 1),
    }
    _store("file_ingestion", entry)
    logger.info(
        f"METRICS file_ingestion: {file_path} | "
        f"{total_ms:.0f}ms total ({graph_ms:.0f}ms graph + {vector_ms:.0f}ms vector) | "
        f"{chunks} chunks ({graph_chunks_ingested}g+{vector_chunks_ingested}v ingested) | "
        f"{entry['avg_chunk_ms']:.0f}ms/chunk"
    )


def record_chunk_ingestion(
    *,
    store: str,  # "graph" or "vector"
    project_id: str,
    chunk_index: int,
    total_chunks: int,
    elapsed_ms: float,
    skipped: bool = False,
) -> None:
    """Record a single chunk ingestion event."""
    entry = {
        "type": "chunk_ingestion",
        "ts": time.time(),
        "store": store,
        "project_id": project_id,
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
        "elapsed_ms": round(elapsed_ms, 1),
        "skipped": skipped,
    }
    _store("chunk_ingestion", entry)


def record_query(
    *,
    query: str,
    project_id: str,
    scope: str,
    total_ms: float,
    retrieval_ms: float,
    synthesis_ms: float,
    vector_passages: int,
    graph_passages: int,
    cached: bool = False,
    source: str = "answer_query",
) -> None:
    """Record a query event."""
    entry = {
        "type": "query",
        "ts": time.time(),
        "source": source,
        "query": query[:100],
        "project_id": project_id,
        "scope": scope,
        "total_ms": round(total_ms, 1),
        "retrieval_ms": round(retrieval_ms, 1),
        "synthesis_ms": round(synthesis_ms, 1),
        "vector_passages": vector_passages,
        "graph_passages": graph_passages,
        "cached": cached,
    }
    _store("query", entry)
    logger.info(
        f"METRICS query: {query[:60]!r} | "
        f"{total_ms:.0f}ms total ({retrieval_ms:.0f}ms retrieval + {synthesis_ms:.0f}ms synthesis) | "
        f"{vector_passages}v+{graph_passages}g passages | cached={cached}"
    )


def record_http_query(
    *,
    query: str,
    project_id: str,
    elapsed_ms: int,
    vector_count: int,
    graph_count: int,
    has_synthesis: bool,
) -> None:
    """Record an HTTP API query event."""
    entry = {
        "type": "http_query",
        "ts": time.time(),
        "query": query[:100],
        "project_id": project_id,
        "elapsed_ms": elapsed_ms,
        "vector_count": vector_count,
        "graph_count": graph_count,
        "has_synthesis": has_synthesis,
    }
    _store("http_query", entry)


# ---------------------------------------------------------------------------
# Summary / stats
# ---------------------------------------------------------------------------


def get_summary() -> dict:
    """Return summary statistics from in-memory history."""
    result: dict = {}

    # File ingestion stats
    fi = _history.get("file_ingestion", [])
    if fi:
        total_times = [e["total_ms"] for e in fi]
        graph_times = [e["graph_ms"] for e in fi]
        vector_times = [e["vector_ms"] for e in fi]
        result["file_ingestion"] = {
            "count": len(fi),
            "avg_total_ms": round(sum(total_times) / len(total_times), 1),
            "avg_graph_ms": round(sum(graph_times) / len(graph_times), 1),
            "avg_vector_ms": round(sum(vector_times) / len(vector_times), 1),
            "min_total_ms": round(min(total_times), 1),
            "max_total_ms": round(max(total_times), 1),
            "total_chunks": sum(e["chunks"] for e in fi),
            "last_5": [
                {
                    "file": e["file_path"],
                    "total_ms": e["total_ms"],
                    "chunks": e["chunks"],
                }
                for e in fi[-5:]
            ],
        }

    # Query stats
    qs = _history.get("query", [])
    if qs:
        total_times = [e["total_ms"] for e in qs]
        cached_count = sum(1 for e in qs if e.get("cached"))
        result["query"] = {
            "count": len(qs),
            "cached": cached_count,
            "avg_total_ms": round(sum(total_times) / len(total_times), 1),
            "min_total_ms": round(min(total_times), 1),
            "max_total_ms": round(max(total_times), 1),
            "last_5": [
                {
                    "query": e["query"],
                    "total_ms": e["total_ms"],
                    "cached": e.get("cached", False),
                }
                for e in qs[-5:]
            ],
        }

    # HTTP query stats
    hq = _history.get("http_query", [])
    if hq:
        times = [e["elapsed_ms"] for e in hq]
        result["http_query"] = {
            "count": len(hq),
            "avg_ms": round(sum(times) / len(times), 1),
            "min_ms": min(times),
            "max_ms": max(times),
        }

    return result


def get_jsonl_path() -> Optional[str]:
    """Return the path to the JSONL metrics file, or None if it doesn't exist."""
    if _METRICS_FILE.exists():
        return str(_METRICS_FILE)
    return None
