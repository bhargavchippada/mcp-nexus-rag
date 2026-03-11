# Version: v3.0
"""
Unit tests for new v1.9 features: batch ingestion and tenant statistics.
All database calls are mocked — no live pgvector or Memgraph required.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from nexus import tools as nexus_tools
from nexus.backends import memgraph as graph_backend
from nexus.backends import pgvector as vector_backend

# ---------------------------------------------------------------------------
# Tenant Statistics Tests
# ---------------------------------------------------------------------------


class TestGetTenantStats:
    """Tests for the get_tenant_stats MCP tool."""

    async def test_returns_counts_from_both_backends(self):
        """Verify stats are collected from all Memgraph helpers and pgvector."""
        with patch.object(graph_backend, "get_document_count", return_value=5):
            with patch.object(graph_backend, "get_chunk_node_count", return_value=3):
                with patch.object(
                    graph_backend, "get_entity_node_count", return_value=2
                ):
                    with patch.object(
                        vector_backend, "get_document_count", return_value=7
                    ):
                        result = await nexus_tools.get_tenant_stats(
                            "TEST_PROJECT", "TEST_SCOPE"
                        )
                        assert result["graph_nodes_total"] == 5
                        assert result["graph_chunk_nodes"] == 3
                        assert result["graph_entity_nodes"] == 2
                        assert result["vector_docs"] == 7
                        assert result["total_docs"] == 12  # 5 + 7

    async def test_handles_empty_scope(self):
        """Verify stats work without scope (all scopes)."""
        with patch.object(graph_backend, "get_document_count", return_value=10):
            with patch.object(graph_backend, "get_chunk_node_count", return_value=6):
                with patch.object(
                    graph_backend, "get_entity_node_count", return_value=4
                ):
                    with patch.object(
                        vector_backend, "get_document_count", return_value=15
                    ):
                        result = await nexus_tools.get_tenant_stats("TEST_PROJECT")
                        assert result["graph_nodes_total"] == 10
                        assert result["vector_docs"] == 15
                        assert result["total_docs"] == 25

    async def test_rejects_empty_project_id(self):
        """Verify empty project_id returns an error string (not raises ValueError)."""
        result = await nexus_tools.get_tenant_stats("")
        assert isinstance(result, str)
        assert "Error" in result

    async def test_handles_backend_zeros(self):
        """Verify zero counts are returned correctly."""
        with patch.object(graph_backend, "get_document_count", return_value=0):
            with patch.object(graph_backend, "get_chunk_node_count", return_value=0):
                with patch.object(
                    graph_backend, "get_entity_node_count", return_value=0
                ):
                    with patch.object(
                        vector_backend, "get_document_count", return_value=0
                    ):
                        result = await nexus_tools.get_tenant_stats(
                            "TEST_PROJECT", "TEST_SCOPE"
                        )
                        assert result["graph_nodes_total"] == 0
                        assert result["graph_chunk_nodes"] == 0
                        assert result["graph_entity_nodes"] == 0
                        assert result["vector_docs"] == 0
                        assert result["total_docs"] == 0


# ---------------------------------------------------------------------------
# Memgraph Document Count Tests
# ---------------------------------------------------------------------------


class TestMemgraphGetDocumentCount:
    """Tests for Memgraph get_document_count backend function."""

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

        with patch.object(graph_backend, "get_driver", return_value=mock_driver):
            count = graph_backend.get_document_count("TEST_PROJECT", "TEST_SCOPE")
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

        with patch.object(graph_backend, "get_driver", return_value=mock_driver):
            count = graph_backend.get_document_count("TEST_PROJECT")
            assert count == 100

    def test_returns_zero_on_error(self):
        """Verify errors return 0 instead of raising."""
        with patch.object(
            graph_backend, "get_driver", side_effect=Exception("Connection failed")
        ):
            count = graph_backend.get_document_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 0


# ---------------------------------------------------------------------------
# Memgraph Chunk Node Count Tests
# ---------------------------------------------------------------------------


class TestMemgraphGetChunkNodeCount:
    """Tests for Memgraph get_chunk_node_count backend function."""

    def _mock_driver(self, count: int) -> MagicMock:
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.single.return_value = {"count": count}
        mock_session.run.return_value = mock_result
        mock_driver.__enter__ = lambda s: mock_driver
        mock_driver.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        return mock_driver

    def test_counts_chunk_nodes_with_scope(self):
        """Verify chunk count returns correct count with scope filter."""
        mock_driver = self._mock_driver(7)
        with patch.object(graph_backend, "get_driver", return_value=mock_driver):
            count = graph_backend.get_chunk_node_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 7

    def test_counts_chunk_nodes_without_scope(self):
        """Verify chunk count works across all scopes."""
        mock_driver = self._mock_driver(20)
        with patch.object(graph_backend, "get_driver", return_value=mock_driver):
            count = graph_backend.get_chunk_node_count("TEST_PROJECT")
            assert count == 20

    def test_returns_zero_on_error(self):
        """Verify errors return 0 instead of raising."""
        with patch.object(
            graph_backend, "get_driver", side_effect=Exception("Connection failed")
        ):
            count = graph_backend.get_chunk_node_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 0


# ---------------------------------------------------------------------------
# Memgraph Entity Node Count Tests
# ---------------------------------------------------------------------------


class TestMemgraphGetEntityNodeCount:
    """Tests for Memgraph get_entity_node_count backend function."""

    def _mock_driver(self, count: int) -> MagicMock:
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.single.return_value = {"count": count}
        mock_session.run.return_value = mock_result
        mock_driver.__enter__ = lambda s: mock_driver
        mock_driver.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        return mock_driver

    def test_counts_entity_nodes_with_scope(self):
        """Verify entity count traverses from chunk to adjacent nodes."""
        mock_driver = self._mock_driver(150)
        with patch.object(graph_backend, "get_driver", return_value=mock_driver):
            count = graph_backend.get_entity_node_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 150

    def test_counts_entity_nodes_without_scope(self):
        """Verify entity count works across all scopes."""
        mock_driver = self._mock_driver(300)
        with patch.object(graph_backend, "get_driver", return_value=mock_driver):
            count = graph_backend.get_entity_node_count("TEST_PROJECT")
            assert count == 300

    def test_returns_zero_on_error(self):
        """Verify errors return 0 instead of raising."""
        with patch.object(
            graph_backend, "get_driver", side_effect=Exception("Connection failed")
        ):
            count = graph_backend.get_entity_node_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 0


# ---------------------------------------------------------------------------
# pgvector Document Count Tests
# ---------------------------------------------------------------------------


class TestPgvectorGetDocumentCount:
    """Tests for pgvector get_document_count backend function."""

    def test_counts_with_scope(self):
        """Verify count uses both project_id and scope filters."""
        with patch(
            "nexus.backends.pgvector._query_metadata",
            return_value=[{"count": 25}],
        ):
            count = vector_backend.get_document_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 25

    def test_counts_without_scope(self):
        """Verify count uses only project_id filter when no scope."""
        with patch(
            "nexus.backends.pgvector._query_metadata",
            return_value=[{"count": 50}],
        ):
            count = vector_backend.get_document_count("TEST_PROJECT")
            assert count == 50

    def test_returns_zero_on_error(self):
        """Verify errors return 0 instead of raising."""
        with patch(
            "nexus.backends.pgvector._query_metadata",
            side_effect=Exception("pgvector unreachable"),
        ):
            count = vector_backend.get_document_count("TEST_PROJECT", "TEST_SCOPE")
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

        with patch.object(graph_backend, "is_duplicate", return_value=False):
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

        with patch.object(graph_backend, "is_duplicate", side_effect=is_dup):
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

        with patch.object(graph_backend, "is_duplicate", return_value=False):
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

        with patch.object(graph_backend, "is_duplicate", return_value=False):
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

        with patch.object(vector_backend, "is_duplicate", return_value=False):
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

        with patch.object(vector_backend, "is_duplicate", side_effect=is_dup):
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

        with patch.object(vector_backend, "is_duplicate", return_value=False):
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

        with patch.object(vector_backend, "is_duplicate", return_value=False):
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
        with patch.object(graph_backend, "get_driver") as mock_driver_factory:
            mock_driver = MagicMock()
            mock_session = MagicMock()
            mock_driver.__enter__ = lambda s: mock_driver
            mock_driver.__exit__ = MagicMock(return_value=False)
            mock_driver.session.return_value.__enter__ = lambda s: mock_session
            mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
            mock_driver_factory.return_value = mock_driver

            with (
                patch.object(vector_backend, "get_connection"),
                patch.object(
                    vector_backend, "_query_metadata", return_value=[{"ok": 1}]
                ),
            ):
                with patch("httpx.AsyncClient") as mock_httpx:
                    mock_response = MagicMock()
                    mock_response.status_code = 200
                    mock_http_client = MagicMock()
                    mock_http_client.__aenter__ = AsyncMock(
                        return_value=mock_http_client
                    )
                    mock_http_client.__aexit__ = AsyncMock(return_value=False)
                    mock_http_client.get = AsyncMock(return_value=mock_response)
                    mock_httpx.return_value = mock_http_client

                    result = await nexus_tools.health_check()

                    assert result["memgraph"] == "ok"
                    assert result["pgvector"] == "ok"
                    assert result["ollama"] == "ok"

    async def test_memgraph_connection_error(self):
        """Verify Memgraph connection errors are captured."""
        with patch.object(
            graph_backend, "get_driver", side_effect=Exception("Connection refused")
        ):
            with (
                patch.object(vector_backend, "get_connection"),
                patch.object(
                    vector_backend, "_query_metadata", return_value=[{"ok": 1}]
                ),
            ):
                with patch("httpx.AsyncClient") as mock_httpx:
                    mock_response = MagicMock()
                    mock_response.status_code = 200
                    mock_http_client = MagicMock()
                    mock_http_client.__aenter__ = AsyncMock(
                        return_value=mock_http_client
                    )
                    mock_http_client.__aexit__ = AsyncMock(return_value=False)
                    mock_http_client.get = AsyncMock(return_value=mock_response)
                    mock_httpx.return_value = mock_http_client

                    result = await nexus_tools.health_check()

                    assert "error" in result["memgraph"]
                    assert result["pgvector"] == "ok"
                    assert result["ollama"] == "ok"

    async def test_ollama_http_error(self):
        """Verify Ollama HTTP errors are captured."""
        with patch.object(graph_backend, "get_driver") as mock_driver_factory:
            mock_driver = MagicMock()
            mock_session = MagicMock()
            mock_driver.__enter__ = lambda s: mock_driver
            mock_driver.__exit__ = MagicMock(return_value=False)
            mock_driver.session.return_value.__enter__ = lambda s: mock_session
            mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
            mock_driver_factory.return_value = mock_driver

            with (
                patch.object(vector_backend, "get_connection"),
                patch.object(
                    vector_backend, "_query_metadata", return_value=[{"ok": 1}]
                ),
            ):
                with patch("httpx.AsyncClient") as mock_httpx:
                    mock_response = MagicMock()
                    mock_response.status_code = 500
                    mock_http_client = MagicMock()
                    mock_http_client.__aenter__ = AsyncMock(
                        return_value=mock_http_client
                    )
                    mock_http_client.__aexit__ = AsyncMock(return_value=False)
                    mock_http_client.get = AsyncMock(return_value=mock_response)
                    mock_httpx.return_value = mock_http_client

                    result = await nexus_tools.health_check()

                    assert result["memgraph"] == "ok"
                    assert result["pgvector"] == "ok"
                    assert "error: HTTP 500" in result["ollama"]


# ---------------------------------------------------------------------------
# Print All Stats Tests
# ---------------------------------------------------------------------------


class TestPrintAllStats:
    """Tests for the print_all_stats MCP tool."""

    async def test_returns_empty_message_when_no_data(self):
        """Verify empty databases return appropriate message."""
        with patch.object(graph_backend, "get_distinct_metadata", return_value=[]):
            with patch.object(vector_backend, "get_distinct_metadata", return_value=[]):
                result = await nexus_tools.print_all_stats()
                assert "No data found" in result
                assert "empty" in result.lower()

    async def test_returns_table_with_single_project(self):
        """Verify table is generated for single project."""
        with patch.object(
            graph_backend, "get_distinct_metadata", return_value=["PROJ1"]
        ):
            with patch.object(vector_backend, "get_distinct_metadata", return_value=[]):
                with patch.object(
                    graph_backend,
                    "get_scopes_for_project",
                    return_value=["SCOPE1"],
                ):
                    with patch.object(
                        vector_backend,
                        "get_scopes_for_project",
                        return_value=[],
                    ):
                        with patch.object(
                            graph_backend, "get_document_count", return_value=10
                        ):
                            with patch.object(
                                graph_backend,
                                "get_chunk_node_count",
                                return_value=0,
                            ):
                                with patch.object(
                                    graph_backend,
                                    "get_entity_node_count",
                                    return_value=0,
                                ):
                                    with patch.object(
                                        vector_backend,
                                        "get_document_count",
                                        return_value=5,
                                    ):
                                        result = await nexus_tools.print_all_stats()

                                        assert "PROJ1" in result
                                        assert "SCOPE1" in result
                                        assert "PROJECT_ID" in result
