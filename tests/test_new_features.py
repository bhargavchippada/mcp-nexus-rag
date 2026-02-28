# Version: v1.9
"""
Unit tests for new v1.9 features: batch ingestion and tenant statistics.
All database calls are mocked — no live Qdrant or Neo4j required.
"""

from unittest.mock import MagicMock, patch, AsyncMock

from nexus import tools as nexus_tools
from nexus.backends import neo4j as neo4j_backend
from nexus.backends import qdrant as qdrant_backend


# ---------------------------------------------------------------------------
# Tenant Statistics Tests
# ---------------------------------------------------------------------------


class TestGetTenantStats:
    """Tests for the get_tenant_stats MCP tool."""

    async def test_returns_counts_from_both_backends(self):
        """Verify stats are collected from both Neo4j and Qdrant."""
        with patch.object(neo4j_backend, "get_document_count", return_value=5):
            with patch.object(qdrant_backend, "get_document_count", return_value=3):
                result = await nexus_tools.get_tenant_stats("TEST_PROJECT", "TEST_SCOPE")
                assert result["graph_docs"] == 5
                assert result["vector_docs"] == 3
                assert result["total_docs"] == 8

    async def test_handles_empty_scope(self):
        """Verify stats work without scope (all scopes)."""
        with patch.object(neo4j_backend, "get_document_count", return_value=10):
            with patch.object(qdrant_backend, "get_document_count", return_value=15):
                result = await nexus_tools.get_tenant_stats("TEST_PROJECT")
                assert result["graph_docs"] == 10
                assert result["vector_docs"] == 15
                assert result["total_docs"] == 25

    async def test_rejects_empty_project_id(self):
        """Verify empty project_id is rejected."""
        result = await nexus_tools.get_tenant_stats("")
        assert "error" in result
        assert "project_id" in result["error"].lower()

    async def test_handles_backend_errors_gracefully(self):
        """Verify errors from backends return 0 counts."""
        with patch.object(neo4j_backend, "get_document_count", return_value=0):
            with patch.object(qdrant_backend, "get_document_count", return_value=0):
                result = await nexus_tools.get_tenant_stats("TEST_PROJECT", "TEST_SCOPE")
                assert result["graph_docs"] == 0
                assert result["vector_docs"] == 0
                assert result["total_docs"] == 0


# ---------------------------------------------------------------------------
# Neo4j Document Count Tests
# ---------------------------------------------------------------------------


class TestNeo4jGetDocumentCount:
    """Tests for Neo4j get_document_count backend function."""

    def test_counts_with_scope(self):
        """Verify count query includes scope filter."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.single.return_value = {"count": 42}
        mock_session.run.return_value = mock_result

        mock_driver.__enter__ = lambda s: mock_driver
        mock_driver.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(neo4j_backend, "neo4j_driver", return_value=mock_driver):
            count = neo4j_backend.get_document_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 42
            assert mock_session.run.called

    def test_counts_without_scope(self):
        """Verify count query works without scope filter."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.single.return_value = {"count": 100}
        mock_session.run.return_value = mock_result

        mock_driver.__enter__ = lambda s: mock_driver
        mock_driver.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(neo4j_backend, "neo4j_driver", return_value=mock_driver):
            count = neo4j_backend.get_document_count("TEST_PROJECT")
            assert count == 100

    def test_returns_zero_on_error(self):
        """Verify errors return 0 instead of raising."""
        with patch.object(
            neo4j_backend, "neo4j_driver", side_effect=Exception("Connection failed")
        ):
            count = neo4j_backend.get_document_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 0


# ---------------------------------------------------------------------------
# Qdrant Document Count Tests
# ---------------------------------------------------------------------------


class TestQdrantGetDocumentCount:
    """Tests for Qdrant get_document_count backend function."""

    def test_counts_with_scope(self):
        """Verify count uses both project_id and scope filters."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.count = 25
        mock_client.count.return_value = mock_result

        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            count = qdrant_backend.get_document_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 25
            assert mock_client.count.called

    def test_counts_without_scope(self):
        """Verify count uses only project_id filter when no scope."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.count = 50
        mock_client.count.return_value = mock_result

        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            count = qdrant_backend.get_document_count("TEST_PROJECT")
            assert count == 50

    def test_returns_zero_on_error(self):
        """Verify errors return 0 instead of raising."""
        mock_client = MagicMock()
        mock_client.count.side_effect = Exception("Qdrant unreachable")

        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            count = qdrant_backend.get_document_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 0


# ---------------------------------------------------------------------------
# Batch Graph Ingestion Tests
# ---------------------------------------------------------------------------


class TestBatchGraphIngestion:
    """Tests for ingest_graph_documents_batch."""

    async def test_ingests_all_valid_documents(self):
        """Verify all valid documents are ingested."""
        docs = [
            {
                "text": "Doc 1",
                "project_id": "TEST",
                "scope": "SCOPE1",
                "source_identifier": "batch1",
            },
            {"text": "Doc 2", "project_id": "TEST", "scope": "SCOPE1"},
        ]

        with patch.object(neo4j_backend, "is_duplicate", return_value=False):
            with patch("nexus.tools.get_graph_index") as mock_index:
                mock_idx = MagicMock()
                mock_index.return_value = mock_idx

                result = await nexus_tools.ingest_graph_documents_batch(docs)

                assert result["ingested"] == 2
                assert result["skipped"] == 0
                assert result["errors"] == 0
                assert mock_idx.insert.call_count == 2

    async def test_skips_duplicates_when_enabled(self):
        """Verify duplicate documents are skipped when skip_duplicates=True."""
        docs = [
            {"text": "Doc 1", "project_id": "TEST", "scope": "SCOPE1"},
            {"text": "Doc 2", "project_id": "TEST", "scope": "SCOPE1"},
        ]

        def is_dup(hash, pid, scope):
            return hash.startswith("a")  # Pretend first doc is duplicate

        with patch.object(neo4j_backend, "is_duplicate", side_effect=is_dup):
            with patch("nexus.tools.content_hash") as mock_hash:
                mock_hash.side_effect = ["aaaa", "bbbb"]  # First is duplicate
                with patch("nexus.tools.get_graph_index") as mock_index:
                    mock_idx = MagicMock()
                    mock_index.return_value = mock_idx

                    result = await nexus_tools.ingest_graph_documents_batch(
                        docs, skip_duplicates=True
                    )

                    assert result["ingested"] == 1
                    assert result["skipped"] == 1
                    assert result["errors"] == 0

    async def test_counts_validation_errors(self):
        """Verify invalid documents are counted as errors."""
        docs = [
            {"text": "", "project_id": "TEST", "scope": "SCOPE1"},  # Empty text
            {"text": "Valid", "project_id": "", "scope": "SCOPE1"},  # Empty project_id
            {"text": "Valid", "project_id": "TEST", "scope": "SCOPE1"},  # Valid
        ]

        with patch.object(neo4j_backend, "is_duplicate", return_value=False):
            with patch("nexus.tools.get_graph_index") as mock_index:
                mock_idx = MagicMock()
                mock_index.return_value = mock_idx

                result = await nexus_tools.ingest_graph_documents_batch(docs)

                assert result["ingested"] == 1
                assert result["errors"] == 2

    async def test_handles_insert_errors_gracefully(self):
        """Verify insert errors are caught and counted."""
        docs = [
            {"text": "Doc 1", "project_id": "TEST", "scope": "SCOPE1"},
            {"text": "Doc 2", "project_id": "TEST", "scope": "SCOPE1"},
        ]

        with patch.object(neo4j_backend, "is_duplicate", return_value=False):
            with patch("nexus.tools.get_graph_index") as mock_index:
                mock_idx = MagicMock()
                mock_idx.insert.side_effect = [None, Exception("Insert failed")]
                mock_index.return_value = mock_idx

                result = await nexus_tools.ingest_graph_documents_batch(docs)

                assert result["ingested"] == 1
                assert result["errors"] == 1


# ---------------------------------------------------------------------------
# Batch Vector Ingestion Tests
# ---------------------------------------------------------------------------


class TestBatchVectorIngestion:
    """Tests for ingest_vector_documents_batch."""

    async def test_ingests_all_valid_documents(self):
        """Verify all valid documents are ingested."""
        docs = [
            {
                "text": "Doc 1",
                "project_id": "TEST",
                "scope": "SCOPE1",
                "source_identifier": "batch1",
            },
            {"text": "Doc 2", "project_id": "TEST", "scope": "SCOPE1"},
        ]

        with patch.object(qdrant_backend, "is_duplicate", return_value=False):
            with patch("nexus.tools.get_vector_index") as mock_index:
                mock_idx = MagicMock()
                mock_index.return_value = mock_idx

                result = await nexus_tools.ingest_vector_documents_batch(docs)

                assert result["ingested"] == 2
                assert result["skipped"] == 0
                assert result["errors"] == 0
                assert mock_idx.insert.call_count == 2

    async def test_skips_duplicates_when_enabled(self):
        """Verify duplicate documents are skipped when skip_duplicates=True."""
        docs = [
            {"text": "Doc 1", "project_id": "TEST", "scope": "SCOPE1"},
            {"text": "Doc 2", "project_id": "TEST", "scope": "SCOPE1"},
        ]

        def is_dup(hash, pid, scope):
            return hash.startswith("a")  # Pretend first doc is duplicate

        with patch.object(qdrant_backend, "is_duplicate", side_effect=is_dup):
            with patch("nexus.tools.content_hash") as mock_hash:
                mock_hash.side_effect = ["aaaa", "bbbb"]  # First is duplicate
                with patch("nexus.tools.get_vector_index") as mock_index:
                    mock_idx = MagicMock()
                    mock_index.return_value = mock_idx

                    result = await nexus_tools.ingest_vector_documents_batch(
                        docs, skip_duplicates=True
                    )

                    assert result["ingested"] == 1
                    assert result["skipped"] == 1
                    assert result["errors"] == 0

    async def test_counts_validation_errors(self):
        """Verify invalid documents are counted as errors."""
        docs = [
            {"text": "", "project_id": "TEST", "scope": "SCOPE1"},  # Empty text
            {"text": "Valid", "project_id": "", "scope": "SCOPE1"},  # Empty project_id
            {"text": "Valid", "project_id": "TEST", "scope": "SCOPE1"},  # Valid
        ]

        with patch.object(qdrant_backend, "is_duplicate", return_value=False):
            with patch("nexus.tools.get_vector_index") as mock_index:
                mock_idx = MagicMock()
                mock_index.return_value = mock_idx

                result = await nexus_tools.ingest_vector_documents_batch(docs)

                assert result["ingested"] == 1
                assert result["errors"] == 2

    async def test_handles_insert_errors_gracefully(self):
        """Verify insert errors are caught and counted."""
        docs = [
            {"text": "Doc 1", "project_id": "TEST", "scope": "SCOPE1"},
            {"text": "Doc 2", "project_id": "TEST", "scope": "SCOPE1"},
        ]

        with patch.object(qdrant_backend, "is_duplicate", return_value=False):
            with patch("nexus.tools.get_vector_index") as mock_index:
                mock_idx = MagicMock()
                mock_idx.insert.side_effect = [None, Exception("Insert failed")]
                mock_index.return_value = mock_idx

                result = await nexus_tools.ingest_vector_documents_batch(docs)

                assert result["ingested"] == 1
                assert result["errors"] == 1

    async def test_empty_documents_list(self):
        """Verify empty document list is handled correctly."""
        result = await nexus_tools.ingest_vector_documents_batch([])
        assert result["ingested"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == 0


# ---------------------------------------------------------------------------
# Health Check Tests
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Tests for the health_check MCP tool."""

    async def test_all_services_healthy(self):
        """Verify health check returns 'ok' when all services are healthy."""
        with patch.object(neo4j_backend, "neo4j_driver") as mock_driver_factory:
            mock_driver = MagicMock()
            mock_session = MagicMock()
            mock_driver.__enter__ = lambda s: mock_driver
            mock_driver.__exit__ = MagicMock(return_value=False)
            mock_driver.session.return_value.__enter__ = lambda s: mock_session
            mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
            mock_driver_factory.return_value = mock_driver

            with patch.object(qdrant_backend, "get_client") as mock_qdrant:
                mock_client = MagicMock()
                mock_client.get_collections.return_value = []
                mock_qdrant.return_value = mock_client

                with patch("httpx.AsyncClient") as mock_httpx:
                    mock_response = MagicMock()
                    mock_response.status_code = 200
                    mock_http_client = MagicMock()
                    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
                    mock_http_client.__aexit__ = AsyncMock(return_value=False)
                    mock_http_client.get = AsyncMock(return_value=mock_response)
                    mock_httpx.return_value = mock_http_client

                    result = await nexus_tools.health_check()

                    assert result["neo4j"] == "ok"
                    assert result["qdrant"] == "ok"
                    assert result["ollama"] == "ok"

    async def test_neo4j_connection_error(self):
        """Verify Neo4j connection errors are captured."""
        with patch.object(
            neo4j_backend, "neo4j_driver", side_effect=Exception("Connection refused")
        ):
            with patch.object(qdrant_backend, "get_client") as mock_qdrant:
                mock_client = MagicMock()
                mock_client.get_collections.return_value = []
                mock_qdrant.return_value = mock_client

                with patch("httpx.AsyncClient") as mock_httpx:
                    mock_response = MagicMock()
                    mock_response.status_code = 200
                    mock_http_client = MagicMock()
                    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
                    mock_http_client.__aexit__ = AsyncMock(return_value=False)
                    mock_http_client.get = AsyncMock(return_value=mock_response)
                    mock_httpx.return_value = mock_http_client

                    result = await nexus_tools.health_check()

                    assert "error" in result["neo4j"]
                    assert result["qdrant"] == "ok"
                    assert result["ollama"] == "ok"

    async def test_ollama_http_error(self):
        """Verify Ollama HTTP errors are captured."""
        with patch.object(neo4j_backend, "neo4j_driver") as mock_driver_factory:
            mock_driver = MagicMock()
            mock_session = MagicMock()
            mock_driver.__enter__ = lambda s: mock_driver
            mock_driver.__exit__ = MagicMock(return_value=False)
            mock_driver.session.return_value.__enter__ = lambda s: mock_session
            mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
            mock_driver_factory.return_value = mock_driver

            with patch.object(qdrant_backend, "get_client") as mock_qdrant:
                mock_client = MagicMock()
                mock_client.get_collections.return_value = []
                mock_qdrant.return_value = mock_client

                with patch("httpx.AsyncClient") as mock_httpx:
                    mock_response = MagicMock()
                    mock_response.status_code = 500
                    mock_http_client = MagicMock()
                    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
                    mock_http_client.__aexit__ = AsyncMock(return_value=False)
                    mock_http_client.get = AsyncMock(return_value=mock_response)
                    mock_httpx.return_value = mock_http_client

                    result = await nexus_tools.health_check()

                    assert result["neo4j"] == "ok"
                    assert result["qdrant"] == "ok"
                    assert "error: HTTP 500" in result["ollama"]
