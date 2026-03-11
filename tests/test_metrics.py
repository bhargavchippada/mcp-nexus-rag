# Version: v1.0
"""Tests for nexus.metrics — performance tracking."""

import json
from unittest.mock import patch

import pytest

from nexus.metrics import (
    _history,
    get_jsonl_path,
    get_summary,
    record_chunk_ingestion,
    record_file_ingestion,
    record_http_query,
    record_query,
    timer,
)


@pytest.fixture(autouse=True)
def _clear_history():
    """Clear in-memory history before each test."""
    _history.clear()
    yield
    _history.clear()


class TestTimer:
    def test_timer_records_elapsed(self):
        import time

        with timer() as t:
            time.sleep(0.01)
        assert t.elapsed_ms >= 5  # At least 5ms

    def test_timer_zero_for_instant(self):
        with timer() as t:
            pass
        assert t.elapsed_ms >= 0
        assert t.elapsed_ms < 100


class TestRecordFileIngestion:
    def test_records_to_history(self):
        with patch("nexus.metrics._append_jsonl"):
            record_file_ingestion(
                file_path="CLAUDE.md",
                project_id="AGENT",
                scope="PERSONA",
                total_ms=5000.0,
                graph_ms=4000.0,
                vector_ms=1000.0,
                chunks=10,
                graph_chunks_ingested=10,
                vector_chunks_ingested=10,
            )
        assert len(_history["file_ingestion"]) == 1
        entry = _history["file_ingestion"][0]
        assert entry["file_path"] == "CLAUDE.md"
        assert entry["total_ms"] == 5000.0
        assert entry["avg_chunk_ms"] == 500.0

    def test_avg_chunk_ms_zero_chunks(self):
        with patch("nexus.metrics._append_jsonl"):
            record_file_ingestion(
                file_path="empty.md",
                project_id="TEST",
                scope="S",
                total_ms=100.0,
                graph_ms=50.0,
                vector_ms=50.0,
                chunks=0,
                graph_chunks_ingested=0,
                vector_chunks_ingested=0,
            )
        entry = _history["file_ingestion"][0]
        assert entry["avg_chunk_ms"] == 100.0  # total_ms / max(0, 1) = 100


class TestRecordQuery:
    def test_records_query(self):
        with patch("nexus.metrics._append_jsonl"):
            record_query(
                query="test query",
                project_id="AGENT",
                scope="all",
                total_ms=500.0,
                retrieval_ms=200.0,
                synthesis_ms=300.0,
                vector_passages=5,
                graph_passages=3,
            )
        assert len(_history["query"]) == 1
        assert _history["query"][0]["total_ms"] == 500.0

    def test_records_cached_query(self):
        with patch("nexus.metrics._append_jsonl"):
            record_query(
                query="cached query",
                project_id="AGENT",
                scope="all",
                total_ms=0,
                retrieval_ms=0,
                synthesis_ms=0,
                vector_passages=0,
                graph_passages=0,
                cached=True,
            )
        assert _history["query"][0]["cached"] is True


class TestRecordChunkIngestion:
    def test_records_chunk(self):
        with patch("nexus.metrics._append_jsonl"):
            record_chunk_ingestion(
                store="graph",
                project_id="AGENT",
                chunk_index=0,
                total_chunks=10,
                elapsed_ms=700.0,
            )
        assert len(_history["chunk_ingestion"]) == 1
        assert _history["chunk_ingestion"][0]["store"] == "graph"

    def test_records_skipped_chunk(self):
        with patch("nexus.metrics._append_jsonl"):
            record_chunk_ingestion(
                store="vector",
                project_id="TEST",
                chunk_index=2,
                total_chunks=5,
                elapsed_ms=0.0,
                skipped=True,
            )
        assert _history["chunk_ingestion"][0]["skipped"] is True


class TestRecordHttpQuery:
    def test_records_http_query(self):
        with patch("nexus.metrics._append_jsonl"):
            record_http_query(
                query="http test",
                project_id="AGENT",
                elapsed_ms=1200,
                vector_count=5,
                graph_count=3,
                has_synthesis=True,
            )
        assert len(_history["http_query"]) == 1
        assert _history["http_query"][0]["elapsed_ms"] == 1200


class TestGetSummary:
    def test_empty_summary(self):
        result = get_summary()
        assert result == {}

    def test_file_ingestion_summary(self):
        with patch("nexus.metrics._append_jsonl"):
            for i in range(3):
                record_file_ingestion(
                    file_path=f"file_{i}.md",
                    project_id="TEST",
                    scope="S",
                    total_ms=1000.0 * (i + 1),
                    graph_ms=600.0 * (i + 1),
                    vector_ms=400.0 * (i + 1),
                    chunks=5,
                    graph_chunks_ingested=5,
                    vector_chunks_ingested=5,
                )
        summary = get_summary()
        fi = summary["file_ingestion"]
        assert fi["count"] == 3
        assert fi["avg_total_ms"] == 2000.0
        assert fi["min_total_ms"] == 1000.0
        assert fi["max_total_ms"] == 3000.0
        assert fi["total_chunks"] == 15
        assert len(fi["last_5"]) == 3

    def test_query_summary(self):
        with patch("nexus.metrics._append_jsonl"):
            record_query(
                query="q1",
                project_id="A",
                scope="s",
                total_ms=100.0,
                retrieval_ms=50.0,
                synthesis_ms=50.0,
                vector_passages=3,
                graph_passages=2,
            )
            record_query(
                query="q2",
                project_id="A",
                scope="s",
                total_ms=0,
                retrieval_ms=0,
                synthesis_ms=0,
                vector_passages=0,
                graph_passages=0,
                cached=True,
            )
        summary = get_summary()
        qs = summary["query"]
        assert qs["count"] == 2
        assert qs["cached"] == 1


class TestHistoryLimit:
    def test_history_capped_at_max(self):
        with patch("nexus.metrics._append_jsonl"):
            for i in range(250):
                record_chunk_ingestion(
                    store="graph",
                    project_id="TEST",
                    chunk_index=i,
                    total_chunks=250,
                    elapsed_ms=10.0,
                )
        assert len(_history["chunk_ingestion"]) == 200


class TestJsonlPath:
    def test_returns_none_when_no_file(self, tmp_path):
        with patch("nexus.metrics._METRICS_FILE", tmp_path / "nonexistent.jsonl"):
            assert get_jsonl_path() is None

    def test_returns_path_when_exists(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("{}")
        with patch("nexus.metrics._METRICS_FILE", f):
            assert get_jsonl_path() == str(f)


class TestAppendJsonl:
    def test_writes_valid_jsonl(self, tmp_path):
        metrics_file = tmp_path / "metrics" / "test.jsonl"
        with (
            patch("nexus.metrics._METRICS_FILE", metrics_file),
            patch("nexus.metrics._METRICS_DIR", tmp_path / "metrics"),
        ):
            record_file_ingestion(
                file_path="test.md",
                project_id="TEST",
                scope="S",
                total_ms=100.0,
                graph_ms=60.0,
                vector_ms=40.0,
                chunks=2,
                graph_chunks_ingested=2,
                vector_chunks_ingested=2,
            )
        lines = metrics_file.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["type"] == "file_ingestion"
        assert data["total_ms"] == 100.0
