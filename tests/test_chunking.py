# Version: v1.0
"""
Tests for nexus.chunking — Document chunking utilities.
"""

import pytest
from unittest.mock import patch, MagicMock

from nexus import chunking
from nexus.chunking import needs_chunking, chunk_document


class TestNeedsChunking:
    """Tests for needs_chunking()."""

    def test_small_document_does_not_need_chunking(self):
        """Documents under MAX_DOCUMENT_SIZE should not need chunking."""
        small_text = "Hello, world!"
        assert needs_chunking(small_text) is False

    def test_large_document_needs_chunking(self):
        """Documents over MAX_DOCUMENT_SIZE should need chunking."""
        # Create a document larger than 512KB
        large_text = "x" * (512 * 1024 + 1)
        assert needs_chunking(large_text) is True

    def test_exact_limit_does_not_need_chunking(self):
        """Document exactly at MAX_DOCUMENT_SIZE should not need chunking."""
        exact_text = "x" * (512 * 1024)
        assert needs_chunking(exact_text) is False

    def test_empty_document_does_not_need_chunking(self):
        """Empty documents should not need chunking."""
        assert needs_chunking("") is False

    def test_unicode_document_size_in_bytes(self):
        """Unicode characters should be measured in bytes, not characters."""
        # Each emoji is 4 bytes in UTF-8
        emoji_text = "😀" * (128 * 1024 + 1)  # 128K emojis = 512KB + 4 bytes
        assert needs_chunking(emoji_text) is True

    @patch.object(chunking, "MAX_DOCUMENT_SIZE", 100)
    def test_respects_config_max_document_size(self):
        """Should use MAX_DOCUMENT_SIZE from config."""
        text = "x" * 101
        assert needs_chunking(text) is True

        text = "x" * 100
        assert needs_chunking(text) is False


class TestChunkDocument:
    """Tests for chunk_document()."""

    def test_small_document_returns_single_chunk(self):
        """Documents under MAX_DOCUMENT_SIZE should return as single chunk."""
        small_text = "Hello, world!"
        chunks = chunk_document(small_text)
        assert len(chunks) == 1
        assert chunks[0] == small_text

    @patch.object(chunking, "MAX_DOCUMENT_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_OVERLAP", 10)
    def test_large_document_returns_multiple_chunks(self):
        """Documents over MAX_DOCUMENT_SIZE should be split into chunks."""
        large_text = "Hello world. " * 50  # ~650 bytes > 100 byte limit
        chunks = chunk_document(large_text)
        assert len(chunks) > 1

    @patch.object(chunking, "MAX_DOCUMENT_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_OVERLAP", 10)
    def test_chunks_are_non_empty(self):
        """All returned chunks should be non-empty."""
        large_text = "Hello world. " * 50
        chunks = chunk_document(large_text)
        for chunk in chunks:
            assert len(chunk) > 0

    @patch.object(chunking, "MAX_DOCUMENT_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_SIZE", 50)
    @patch.object(chunking, "INGEST_CHUNK_OVERLAP", 10)
    def test_respects_chunk_size_config(self):
        """Should use INGEST_CHUNK_SIZE and INGEST_CHUNK_OVERLAP from config."""
        large_text = "Hello world. " * 50
        chunks = chunk_document(large_text)
        # With smaller chunk size, we should get more chunks
        assert len(chunks) >= 2

    def test_empty_document_returns_single_empty_chunk(self):
        """Empty documents should return single empty chunk."""
        chunks = chunk_document("")
        assert len(chunks) == 1
        assert chunks[0] == ""


class TestIngestWithChunking:
    """Tests for chunking integration with ingest tools."""

    @pytest.fixture
    def mock_graph_index(self):
        """Mock the graph index."""
        mock_index = MagicMock()
        mock_index.insert = MagicMock()
        return mock_index

    @pytest.fixture
    def mock_vector_index(self):
        """Mock the vector index."""
        mock_index = MagicMock()
        mock_index.insert = MagicMock()
        return mock_index

    @patch.object(chunking, "MAX_DOCUMENT_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_OVERLAP", 10)
    @patch("nexus.tools.get_graph_index")
    @patch("nexus.tools.neo4j_backend")
    async def test_graph_ingest_chunks_large_document(
        self, mock_neo4j, mock_get_index
    ):
        """Large documents should be automatically chunked for GraphRAG."""
        from nexus.tools import ingest_graph_document

        mock_index = MagicMock()
        mock_index.insert = MagicMock()
        mock_get_index.return_value = mock_index
        mock_neo4j.is_duplicate.return_value = False

        large_text = "Hello world. " * 50  # ~650 bytes > 100 byte limit
        result = await ingest_graph_document(
            text=large_text,
            project_id="TEST",
            scope="CODE",
        )

        assert "chunks" in result.lower()
        assert mock_index.insert.call_count > 1

    @patch.object(chunking, "MAX_DOCUMENT_SIZE", 100)
    @patch("nexus.tools.get_graph_index")
    @patch("nexus.tools.neo4j_backend")
    async def test_graph_ingest_rejects_large_when_auto_chunk_false(
        self, mock_neo4j, mock_get_index
    ):
        """Large documents should be rejected when auto_chunk=False."""
        from nexus.tools import ingest_graph_document

        mock_index = MagicMock()
        mock_get_index.return_value = mock_index
        mock_neo4j.is_duplicate.return_value = False

        large_text = "Hello world. " * 50  # > 100 bytes
        result = await ingest_graph_document(
            text=large_text,
            project_id="TEST",
            scope="CODE",
            auto_chunk=False,
        )

        assert "error" in result.lower()
        assert "limit" in result.lower()
        mock_index.insert.assert_not_called()

    @patch.object(chunking, "MAX_DOCUMENT_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_OVERLAP", 10)
    @patch("nexus.tools.get_vector_index")
    @patch("nexus.tools.qdrant_backend")
    async def test_vector_ingest_chunks_large_document(
        self, mock_qdrant, mock_get_index
    ):
        """Large documents should be automatically chunked for VectorRAG."""
        from nexus.tools import ingest_vector_document

        mock_index = MagicMock()
        mock_index.insert = MagicMock()
        mock_get_index.return_value = mock_index
        mock_qdrant.is_duplicate.return_value = False

        large_text = "Hello world. " * 50  # ~650 bytes > 100 byte limit
        result = await ingest_vector_document(
            text=large_text,
            project_id="TEST",
            scope="CODE",
        )

        assert "chunks" in result.lower()
        assert mock_index.insert.call_count > 1

    @patch.object(chunking, "MAX_DOCUMENT_SIZE", 100)
    @patch("nexus.tools.get_vector_index")
    @patch("nexus.tools.qdrant_backend")
    async def test_vector_ingest_rejects_large_when_auto_chunk_false(
        self, mock_qdrant, mock_get_index
    ):
        """Large documents should be rejected when auto_chunk=False."""
        from nexus.tools import ingest_vector_document

        mock_index = MagicMock()
        mock_get_index.return_value = mock_index
        mock_qdrant.is_duplicate.return_value = False

        large_text = "Hello world. " * 50  # > 100 bytes
        result = await ingest_vector_document(
            text=large_text,
            project_id="TEST",
            scope="CODE",
            auto_chunk=False,
        )

        assert "error" in result.lower()
        assert "limit" in result.lower()
        mock_index.insert.assert_not_called()

    @patch("nexus.tools.get_graph_index")
    @patch("nexus.tools.neo4j_backend")
    async def test_small_document_not_chunked(self, mock_neo4j, mock_get_index, mock_graph_index):
        """Small documents should be ingested as single document."""
        from nexus.tools import ingest_graph_document

        mock_get_index.return_value = mock_graph_index
        mock_neo4j.is_duplicate.return_value = False

        small_text = "Hello world."
        result = await ingest_graph_document(
            text=small_text,
            project_id="TEST",
            scope="CODE",
        )

        assert "successfully ingested graph document" in result.lower()
        mock_graph_index.insert.assert_called_once()

    @patch.object(chunking, "MAX_DOCUMENT_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_OVERLAP", 10)
    @patch("nexus.tools.get_graph_index")
    @patch("nexus.tools.neo4j_backend")
    async def test_chunk_source_identifier_format(
        self, mock_neo4j, mock_get_index
    ):
        """Chunk source identifiers should include chunk number."""
        from nexus.tools import ingest_graph_document

        mock_index = MagicMock()
        mock_index.insert = MagicMock()
        mock_get_index.return_value = mock_index
        mock_neo4j.is_duplicate.return_value = False

        large_text = "Hello world. " * 50
        await ingest_graph_document(
            text=large_text,
            project_id="TEST",
            scope="CODE",
            source_identifier="test_doc",
        )

        # Check that source identifiers include chunk info
        calls = mock_index.insert.call_args_list
        for i, call in enumerate(calls):
            doc = call[0][0]
            assert f"chunk_{i+1}_of_" in doc.metadata["source"]


class TestBatchIngestWithChunking:
    """Tests for chunking integration with batch ingest tools."""

    @patch.object(chunking, "MAX_DOCUMENT_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_OVERLAP", 10)
    @patch("nexus.tools.get_graph_index")
    @patch("nexus.tools.neo4j_backend")
    async def test_batch_graph_ingest_chunks_large_documents(
        self, mock_neo4j, mock_get_index
    ):
        """Batch ingest should chunk large documents."""
        from nexus.tools import ingest_graph_documents_batch

        mock_index = MagicMock()
        mock_index.insert = MagicMock()
        mock_get_index.return_value = mock_index
        mock_neo4j.is_duplicate.return_value = False

        documents = [
            {"text": "Small doc.", "project_id": "TEST", "scope": "CODE"},
            {"text": "Hello world. " * 50, "project_id": "TEST", "scope": "CODE"},  # Large ~650 bytes
        ]

        result = await ingest_graph_documents_batch(documents)

        assert result["chunks"] > 0
        assert result["ingested"] > 2  # More than 2 because of chunking

    @patch.object(chunking, "MAX_DOCUMENT_SIZE", 100)
    @patch("nexus.tools.get_graph_index")
    @patch("nexus.tools.neo4j_backend")
    async def test_batch_graph_rejects_large_when_auto_chunk_false(
        self, mock_neo4j, mock_get_index
    ):
        """Batch ingest should reject large documents when auto_chunk=False."""
        from nexus.tools import ingest_graph_documents_batch

        mock_index = MagicMock()
        mock_index.insert = MagicMock()
        mock_get_index.return_value = mock_index
        mock_neo4j.is_duplicate.return_value = False

        documents = [
            {"text": "Small doc.", "project_id": "TEST", "scope": "CODE"},
            {"text": "Hello world. " * 50, "project_id": "TEST", "scope": "CODE"},  # Large
        ]

        result = await ingest_graph_documents_batch(documents, auto_chunk=False)

        assert result["errors"] == 1  # Large doc rejected
        assert result["ingested"] == 1  # Small doc ingested
        assert result["chunks"] == 0

    @patch.object(chunking, "MAX_DOCUMENT_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_SIZE", 100)
    @patch.object(chunking, "INGEST_CHUNK_OVERLAP", 10)
    @patch("nexus.tools.get_vector_index")
    @patch("nexus.tools.qdrant_backend")
    async def test_batch_vector_ingest_chunks_large_documents(
        self, mock_qdrant, mock_get_index
    ):
        """Batch vector ingest should chunk large documents."""
        from nexus.tools import ingest_vector_documents_batch

        mock_index = MagicMock()
        mock_index.insert = MagicMock()
        mock_get_index.return_value = mock_index
        mock_qdrant.is_duplicate.return_value = False

        documents = [
            {"text": "Small doc.", "project_id": "TEST", "scope": "CODE"},
            {"text": "Hello world. " * 50, "project_id": "TEST", "scope": "CODE"},  # Large ~650 bytes
        ]

        result = await ingest_vector_documents_batch(documents)

        assert result["chunks"] > 0
        assert result["ingested"] > 2
