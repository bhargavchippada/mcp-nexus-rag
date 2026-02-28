# Version: v2.0
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
        """Verify stats are collected from all Neo4j helpers and Qdrant."""
        with patch.object(neo4j_backend, "get_document_count", return_value=5):
            with patch.object(neo4j_backend, "get_chunk_node_count", return_value=3):
                with patch.object(neo4j_backend, "get_entity_node_count", return_value=2):
                    with patch.object(qdrant_backend, "get_document_count", return_value=7):
                        result = await nexus_tools.get_tenant_stats("TEST_PROJECT", "TEST_SCOPE")
                        assert result["graph_nodes_total"] == 5
                        assert result["graph_chunk_nodes"] == 3
                        assert result["graph_entity_nodes"] == 2
                        assert result["vector_docs"] == 7
                        assert result["total_docs"] == 12  # 5 + 7

    async def test_handles_empty_scope(self):
        """Verify stats work without scope (all scopes)."""
        with patch.object(neo4j_backend, "get_document_count", return_value=10):
            with patch.object(neo4j_backend, "get_chunk_node_count", return_value=6):
                with patch.object(neo4j_backend, "get_entity_node_count", return_value=4):
                    with patch.object(qdrant_backend, "get_document_count", return_value=15):
                        result = await nexus_tools.get_tenant_stats("TEST_PROJECT")
                        assert result["graph_nodes_total"] == 10
                        assert result["vector_docs"] == 15
                        assert result["total_docs"] == 25

    async def test_rejects_empty_project_id(self):
        """Verify empty project_id raises ValueError."""
        import pytest
        with pytest.raises(ValueError, match="project_id must not be empty"):
            await nexus_tools.get_tenant_stats("")

    async def test_handles_backend_zeros(self):
        """Verify zero counts are returned correctly."""
        with patch.object(neo4j_backend, "get_document_count", return_value=0):
            with patch.object(neo4j_backend, "get_chunk_node_count", return_value=0):
                with patch.object(neo4j_backend, "get_entity_node_count", return_value=0):
                    with patch.object(qdrant_backend, "get_document_count", return_value=0):
                        result = await nexus_tools.get_tenant_stats("TEST_PROJECT", "TEST_SCOPE")
                        assert result["graph_nodes_total"] == 0
                        assert result["graph_chunk_nodes"] == 0
                        assert result["graph_entity_nodes"] == 0
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
# Neo4j Chunk Node Count Tests
# ---------------------------------------------------------------------------


class TestNeo4jGetChunkNodeCount:
    """Tests for Neo4j get_chunk_node_count backend function."""

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
        with patch.object(neo4j_backend, "neo4j_driver", return_value=mock_driver):
            count = neo4j_backend.get_chunk_node_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 7

    def test_counts_chunk_nodes_without_scope(self):
        """Verify chunk count works across all scopes."""
        mock_driver = self._mock_driver(20)
        with patch.object(neo4j_backend, "neo4j_driver", return_value=mock_driver):
            count = neo4j_backend.get_chunk_node_count("TEST_PROJECT")
            assert count == 20

    def test_returns_zero_on_error(self):
        """Verify errors return 0 instead of raising."""
        with patch.object(
            neo4j_backend, "neo4j_driver", side_effect=Exception("Connection failed")
        ):
            count = neo4j_backend.get_chunk_node_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 0


# ---------------------------------------------------------------------------
# Neo4j Entity Node Count Tests
# ---------------------------------------------------------------------------


class TestNeo4jGetEntityNodeCount:
    """Tests for Neo4j get_entity_node_count backend function."""

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
        with patch.object(neo4j_backend, "neo4j_driver", return_value=mock_driver):
            count = neo4j_backend.get_entity_node_count("TEST_PROJECT", "TEST_SCOPE")
            assert count == 150

    def test_counts_entity_nodes_without_scope(self):
        """Verify entity count works across all scopes."""
        mock_driver = self._mock_driver(300)
        with patch.object(neo4j_backend, "neo4j_driver", return_value=mock_driver):
            count = neo4j_backend.get_entity_node_count("TEST_PROJECT")
            assert count == 300

    def test_returns_zero_on_error(self):
        """Verify errors return 0 instead of raising."""
        with patch.object(
            neo4j_backend, "neo4j_driver", side_effect=Exception("Connection failed")
        ):
            count = neo4j_backend.get_entity_node_count("TEST_PROJECT", "TEST_SCOPE")
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


# ---------------------------------------------------------------------------
# Print All Stats Tests
# ---------------------------------------------------------------------------


class TestPrintAllStats:
    """Tests for the print_all_stats MCP tool."""

    async def test_returns_empty_message_when_no_data(self):
        """Verify empty databases return appropriate message."""
        with patch.object(neo4j_backend, "get_distinct_metadata", return_value=[]):
            with patch.object(qdrant_backend, "get_distinct_metadata", return_value=[]):
                result = await nexus_tools.print_all_stats()
                assert "No data found" in result
                assert "empty" in result.lower()

    async def test_returns_table_with_single_project(self):
        """Verify table is generated for single project."""
        with patch.object(neo4j_backend, "get_distinct_metadata", return_value=["PROJ1"]):
            with patch.object(qdrant_backend, "get_distinct_metadata", return_value=[]):
                with patch.object(
                    neo4j_backend, "get_scopes_for_project", return_value=["SCOPE1"]
                ):
                    with patch.object(
                        qdrant_backend, "scroll_field", return_value=set()
                    ):
                        with patch.object(
                            neo4j_backend, "get_document_count", return_value=10
                        ):
                            with patch.object(
                                qdrant_backend, "get_document_count", return_value=5
                            ):
                                result = await nexus_tools.print_all_stats()

                                assert "PROJ1" in result
                                assert "SCOPE1" in result
                                assert "PROJECT_ID" in result
                                assert "GRAPH" in result
                                assert "VECTOR" in result
                                assert "TOTAL" in result
                                assert "10" in result
                                assert "5" in result
                                assert "15" in result  # 10 + 5

    async def test_returns_table_with_multiple_projects(self):
        """Verify table includes multiple projects and scopes."""
        with patch.object(
            neo4j_backend, "get_distinct_metadata", return_value=["PROJ1", "PROJ2"]
        ):
            with patch.object(qdrant_backend, "get_distinct_metadata", return_value=[]):
                with patch.object(
                    neo4j_backend,
                    "get_scopes_for_project",
                    side_effect=[["SCOPE1", "SCOPE2"], ["SCOPE3"]],
                ):
                    with patch.object(qdrant_backend, "scroll_field", return_value=set()):
                        with patch.object(
                            neo4j_backend, "get_document_count", return_value=5
                        ):
                            with patch.object(
                                qdrant_backend, "get_document_count", return_value=3
                            ):
                                result = await nexus_tools.print_all_stats()

                                assert "PROJ1" in result
                                assert "PROJ2" in result
                                assert "SCOPE1" in result
                                assert "SCOPE2" in result
                                assert "SCOPE3" in result
                                assert "Projects: 2" in result
                                assert "Rows: 3" in result

    async def test_includes_summary_totals(self):
        """Verify summary row shows correct totals."""
        with patch.object(neo4j_backend, "get_distinct_metadata", return_value=["PROJ1"]):
            with patch.object(qdrant_backend, "get_distinct_metadata", return_value=[]):
                with patch.object(
                    neo4j_backend, "get_scopes_for_project", return_value=["SCOPE1"]
                ):
                    with patch.object(qdrant_backend, "scroll_field", return_value=set()):
                        with patch.object(
                            neo4j_backend, "get_document_count", return_value=20
                        ):
                            with patch.object(
                                qdrant_backend, "get_document_count", return_value=30
                            ):
                                result = await nexus_tools.print_all_stats()

                                # Check summary line
                                assert "Graph nodes: 20" in result
                                assert "Vector docs: 30" in result
                                assert "Total: 50" in result


    async def test_handles_projects_in_only_vector_store(self):
        """Verify projects only in Qdrant are included."""
        with patch.object(neo4j_backend, "get_distinct_metadata", return_value=[]):
            with patch.object(
                qdrant_backend, "get_distinct_metadata", return_value=["VECTOR_ONLY"]
            ):
                with patch.object(
                    neo4j_backend, "get_scopes_for_project", return_value=[]
                ):
                    with patch.object(
                        qdrant_backend, "scroll_field", return_value={"VSCOPE"}
                    ):
                        with patch.object(
                            neo4j_backend, "get_document_count", return_value=0
                        ):
                            with patch.object(
                                qdrant_backend, "get_document_count", return_value=15
                            ):
                                result = await nexus_tools.print_all_stats()

                                assert "VECTOR_ONLY" in result
                                assert "VSCOPE" in result
                                assert "15" in result

    async def test_handles_project_with_no_scopes(self):
        """Verify project with no scopes shows '(all)' placeholder."""
        with patch.object(neo4j_backend, "get_distinct_metadata", return_value=["PROJ1"]):
            with patch.object(qdrant_backend, "get_distinct_metadata", return_value=[]):
                with patch.object(
                    neo4j_backend, "get_scopes_for_project", return_value=[]
                ):
                    with patch.object(qdrant_backend, "scroll_field", return_value=set()):
                        with patch.object(
                            neo4j_backend, "get_document_count", return_value=5
                        ):
                            with patch.object(
                                qdrant_backend, "get_document_count", return_value=5
                            ):
                                result = await nexus_tools.print_all_stats()

                                assert "PROJ1" in result
                                assert "(all)" in result

    async def test_table_has_proper_ascii_formatting(self):
        """Verify table has proper ASCII border formatting."""
        with patch.object(neo4j_backend, "get_distinct_metadata", return_value=["P1"]):
            with patch.object(qdrant_backend, "get_distinct_metadata", return_value=[]):
                with patch.object(
                    neo4j_backend, "get_scopes_for_project", return_value=["S1"]
                ):
                    with patch.object(qdrant_backend, "scroll_field", return_value=set()):
                        with patch.object(
                            neo4j_backend, "get_document_count", return_value=1
                        ):
                            with patch.object(
                                qdrant_backend, "get_document_count", return_value=1
                            ):
                                result = await nexus_tools.print_all_stats()

                                # Check for table borders
                                assert "+" in result
                                assert "-" in result
                                assert "|" in result


# ---------------------------------------------------------------------------
# Answer Query Tests
# ---------------------------------------------------------------------------


def _make_node(text: str) -> MagicMock:
    """Build a minimal retriever node mock with .node.get_content()."""
    node = MagicMock()
    node.node.get_content.return_value = text
    return node


def _ollama_mock(answer: str = "The answer is 42.") -> MagicMock:
    """Return a mock httpx.AsyncClient that yields a valid Ollama response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"message": {"content": answer}}
    mock_response.raise_for_status = MagicMock()

    mock_http_client = MagicMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)
    mock_http_client.post = AsyncMock(return_value=mock_response)
    return mock_http_client


class TestAnswerQuery:
    """Tests for the answer_query MCP tool (combined RAG + GraphRAG)."""

    # ── Input validation ──────────────────────────────────────────────────────

    async def test_rejects_empty_query(self):
        result = await nexus_tools.answer_query("", "PROJ", "SCOPE")
        assert "Error" in result
        assert "query" in result

    async def test_rejects_empty_project_id(self):
        result = await nexus_tools.answer_query("What is X?", "", "SCOPE")
        assert "Error" in result
        assert "project_id" in result


    # ── Happy path ────────────────────────────────────────────────────────────

    async def test_returns_answer_from_combined_context(self):
        """Verify answer is generated when both backends return passages."""
        graph_node = _make_node("Graph: The sky is blue.")
        vector_node = _make_node("Vector: Water is H2O.")

        mock_graph_retriever = MagicMock()
        mock_graph_retriever.aretrieve = AsyncMock(return_value=[graph_node])
        mock_vector_retriever = MagicMock()
        mock_vector_retriever.aretrieve = AsyncMock(return_value=[vector_node])

        mock_graph_index = MagicMock()
        mock_graph_index.as_retriever.return_value = mock_graph_retriever
        mock_vector_index = MagicMock()
        mock_vector_index.as_retriever.return_value = mock_vector_retriever

        with patch("nexus.tools.get_graph_index", return_value=mock_graph_index):
            with patch("nexus.tools.get_vector_index", return_value=mock_vector_index):
                with patch("nexus.tools.RERANKER_ENABLED", False):
                    with patch("httpx.AsyncClient", return_value=_ollama_mock("42.")):
                        result = await nexus_tools.answer_query(
                            "What is the answer?", "PROJ", "SCOPE"
                        )
                        assert result == "42."

    async def test_prompt_includes_both_sources(self):
        """Verify the Ollama prompt contains [graph] and [vector] prefixes."""
        graph_node = _make_node("GraphPassage")
        vector_node = _make_node("VectorPassage")

        mock_graph_retriever = MagicMock()
        mock_graph_retriever.aretrieve = AsyncMock(return_value=[graph_node])
        mock_vector_retriever = MagicMock()
        mock_vector_retriever.aretrieve = AsyncMock(return_value=[vector_node])

        mock_graph_index = MagicMock()
        mock_graph_index.as_retriever.return_value = mock_graph_retriever
        mock_vector_index = MagicMock()
        mock_vector_index.as_retriever.return_value = mock_vector_retriever

        captured_payload: list[dict] = []

        async def capture_post(url: str, json: dict) -> MagicMock:  # type: ignore[override]
            captured_payload.append(json)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"message": {"content": "ok"}}
            return resp

        mock_http_client = MagicMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = capture_post

        with patch("nexus.tools.get_graph_index", return_value=mock_graph_index):
            with patch("nexus.tools.get_vector_index", return_value=mock_vector_index):
                with patch("nexus.tools.RERANKER_ENABLED", False):
                    with patch("httpx.AsyncClient", return_value=mock_http_client):
                        await nexus_tools.answer_query("Q?", "PROJ", "SCOPE")

        assert captured_payload, "No POST was captured"
        user_content = captured_payload[0]["messages"][1]["content"]
        assert "[graph] GraphPassage" in user_content
        assert "[vector] VectorPassage" in user_content

    # ── Deduplication ─────────────────────────────────────────────────────────

    async def test_deduplicates_identical_passages_across_sources(self):
        """Same text from both backends should appear only once in the prompt."""
        shared_text = "Shared knowledge."
        graph_node = _make_node(shared_text)
        vector_node = _make_node(shared_text)

        mock_graph_retriever = MagicMock()
        mock_graph_retriever.aretrieve = AsyncMock(return_value=[graph_node])
        mock_vector_retriever = MagicMock()
        mock_vector_retriever.aretrieve = AsyncMock(return_value=[vector_node])

        mock_graph_index = MagicMock()
        mock_graph_index.as_retriever.return_value = mock_graph_retriever
        mock_vector_index = MagicMock()
        mock_vector_index.as_retriever.return_value = mock_vector_retriever

        captured_payload: list[dict] = []

        async def capture_post(url: str, json: dict) -> MagicMock:  # type: ignore[override]
            captured_payload.append(json)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"message": {"content": "deduped"}}
            return resp

        mock_http_client = MagicMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = capture_post

        with patch("nexus.tools.get_graph_index", return_value=mock_graph_index):
            with patch("nexus.tools.get_vector_index", return_value=mock_vector_index):
                with patch("nexus.tools.RERANKER_ENABLED", False):
                    with patch("httpx.AsyncClient", return_value=mock_http_client):
                        await nexus_tools.answer_query("Q?", "PROJ", "SCOPE")

        user_content = captured_payload[0]["messages"][1]["content"]
        assert user_content.count(shared_text) == 1

    # ── No context ────────────────────────────────────────────────────────────

    async def test_returns_no_context_message_when_both_backends_empty(self):
        """Verify graceful message when neither backend returns passages."""
        mock_retriever = MagicMock()
        mock_retriever.aretrieve = AsyncMock(return_value=[])
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        with patch("nexus.tools.get_graph_index", return_value=mock_index):
            with patch("nexus.tools.get_vector_index", return_value=mock_index):
                with patch("nexus.tools.RERANKER_ENABLED", False):
                    result = await nexus_tools.answer_query("Q?", "PROJ", "SCOPE")
                    assert "No context found" in result
                    assert "PROJ" in result
                    assert "SCOPE" in result

    # ── Partial results ───────────────────────────────────────────────────────

    async def test_generates_answer_when_only_graph_returns_results(self):
        """Answer is still generated if vector backend returns nothing."""
        graph_node = _make_node("Graph-only passage.")
        mock_graph_retriever = MagicMock()
        mock_graph_retriever.aretrieve = AsyncMock(return_value=[graph_node])
        mock_vector_retriever = MagicMock()
        mock_vector_retriever.aretrieve = AsyncMock(return_value=[])

        mock_graph_index = MagicMock()
        mock_graph_index.as_retriever.return_value = mock_graph_retriever
        mock_vector_index = MagicMock()
        mock_vector_index.as_retriever.return_value = mock_vector_retriever

        with patch("nexus.tools.get_graph_index", return_value=mock_graph_index):
            with patch("nexus.tools.get_vector_index", return_value=mock_vector_index):
                with patch("nexus.tools.RERANKER_ENABLED", False):
                    with patch("httpx.AsyncClient", return_value=_ollama_mock("Graph answer.")):
                        result = await nexus_tools.answer_query("Q?", "PROJ", "SCOPE")
                        assert result == "Graph answer."

    async def test_generates_answer_when_only_vector_returns_results(self):
        """Answer is still generated if graph backend returns nothing."""
        vector_node = _make_node("Vector-only passage.")
        mock_graph_retriever = MagicMock()
        mock_graph_retriever.aretrieve = AsyncMock(return_value=[])
        mock_vector_retriever = MagicMock()
        mock_vector_retriever.aretrieve = AsyncMock(return_value=[vector_node])

        mock_graph_index = MagicMock()
        mock_graph_index.as_retriever.return_value = mock_graph_retriever
        mock_vector_index = MagicMock()
        mock_vector_index.as_retriever.return_value = mock_vector_retriever

        with patch("nexus.tools.get_graph_index", return_value=mock_graph_index):
            with patch("nexus.tools.get_vector_index", return_value=mock_vector_index):
                with patch("nexus.tools.RERANKER_ENABLED", False):
                    with patch("httpx.AsyncClient", return_value=_ollama_mock("Vector answer.")):
                        result = await nexus_tools.answer_query("Q?", "PROJ", "SCOPE")
                        assert result == "Vector answer."

    # ── Context truncation ────────────────────────────────────────────────────

    async def test_truncates_context_when_exceeds_max_chars(self):
        """Verify context is cut to max_context_chars and truncation marker added."""
        long_text = "x" * 5000
        graph_node = _make_node(long_text)

        mock_graph_retriever = MagicMock()
        mock_graph_retriever.aretrieve = AsyncMock(return_value=[graph_node])
        mock_vector_retriever = MagicMock()
        mock_vector_retriever.aretrieve = AsyncMock(return_value=[])

        mock_graph_index = MagicMock()
        mock_graph_index.as_retriever.return_value = mock_graph_retriever
        mock_vector_index = MagicMock()
        mock_vector_index.as_retriever.return_value = mock_vector_retriever

        captured_payload: list[dict] = []

        async def capture_post(url: str, json: dict) -> MagicMock:  # type: ignore[override]
            captured_payload.append(json)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"message": {"content": "truncated"}}
            return resp

        mock_http_client = MagicMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = capture_post

        with patch("nexus.tools.get_graph_index", return_value=mock_graph_index):
            with patch("nexus.tools.get_vector_index", return_value=mock_vector_index):
                with patch("nexus.tools.RERANKER_ENABLED", False):
                    with patch("httpx.AsyncClient", return_value=mock_http_client):
                        await nexus_tools.answer_query(
                            "Q?", "PROJ", "SCOPE", max_context_chars=100
                        )

        user_content = captured_payload[0]["messages"][1]["content"]
        assert "[context truncated]" in user_content

    # ── Error handling ────────────────────────────────────────────────────────

    async def test_handles_ollama_http_error(self):
        """Verify Ollama HTTP errors are returned as error strings."""
        import httpx

        graph_node = _make_node("Some passage.")
        mock_retriever = MagicMock()
        mock_retriever.aretrieve = AsyncMock(return_value=[graph_node])
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.text = "Service Unavailable"

        async def raise_http_error(url: str, json: dict) -> MagicMock:  # type: ignore[override]
            raise httpx.HTTPStatusError(
                "503", request=MagicMock(), response=mock_response
            )

        mock_http_client = MagicMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = raise_http_error

        with patch("nexus.tools.get_graph_index", return_value=mock_index):
            with patch("nexus.tools.get_vector_index", return_value=mock_index):
                with patch("nexus.tools.RERANKER_ENABLED", False):
                    with patch("httpx.AsyncClient", return_value=mock_http_client):
                        result = await nexus_tools.answer_query("Q?", "PROJ", "SCOPE")
                        assert "Ollama HTTP error 503" in result

    async def test_handles_ollama_connection_error(self):
        """Verify connection errors are returned as error strings."""
        graph_node = _make_node("Some passage.")
        mock_retriever = MagicMock()
        mock_retriever.aretrieve = AsyncMock(return_value=[graph_node])
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        async def raise_connection_error(url: str, json: dict) -> MagicMock:  # type: ignore[override]
            raise ConnectionError("refused")

        mock_http_client = MagicMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = raise_connection_error

        with patch("nexus.tools.get_graph_index", return_value=mock_index):
            with patch("nexus.tools.get_vector_index", return_value=mock_index):
                with patch("nexus.tools.RERANKER_ENABLED", False):
                    with patch("httpx.AsyncClient", return_value=mock_http_client):
                        result = await nexus_tools.answer_query("Q?", "PROJ", "SCOPE")
                        assert "Error generating answer" in result

    # ── Model override ────────────────────────────────────────────────────────

    async def test_uses_custom_model_when_provided(self):
        """Verify model parameter overrides DEFAULT_LLM_MODEL in the payload."""
        graph_node = _make_node("Some passage.")
        mock_retriever = MagicMock()
        mock_retriever.aretrieve = AsyncMock(return_value=[graph_node])
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        captured_payload: list[dict] = []

        async def capture_post(url: str, json: dict) -> MagicMock:  # type: ignore[override]
            captured_payload.append(json)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"message": {"content": "custom model"}}
            return resp

        mock_http_client = MagicMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = capture_post

        with patch("nexus.tools.get_graph_index", return_value=mock_index):
            with patch("nexus.tools.get_vector_index", return_value=mock_index):
                with patch("nexus.tools.RERANKER_ENABLED", False):
                    with patch("httpx.AsyncClient", return_value=mock_http_client):
                        await nexus_tools.answer_query(
                            "Q?", "PROJ", "SCOPE", model="mistral:7b"
                        )

        assert captured_payload[0]["model"] == "mistral:7b"

    # ── Backend failure isolation ─────────────────────────────────────────────

    async def test_continues_if_graph_backend_raises(self):
        """Verify that a crashing graph backend still lets vector context through."""
        vector_node = _make_node("Vector fallback.")
        mock_vector_retriever = MagicMock()
        mock_vector_retriever.aretrieve = AsyncMock(return_value=[vector_node])
        mock_vector_index = MagicMock()
        mock_vector_index.as_retriever.return_value = mock_vector_retriever

        with patch("nexus.tools.get_graph_index", side_effect=RuntimeError("neo4j down")):
            with patch("nexus.tools.get_vector_index", return_value=mock_vector_index):
                with patch("nexus.tools.RERANKER_ENABLED", False):
                    with patch("httpx.AsyncClient", return_value=_ollama_mock("fallback")):
                        result = await nexus_tools.answer_query("Q?", "PROJ", "SCOPE")
                        assert result == "fallback"

    async def test_empty_scope_omits_tenant_filter(self):
        """Verify that an empty scope omits the tenant_scope filter in both backends."""
        node = _make_node("Global context.")
        mock_retriever = MagicMock()
        mock_retriever.aretrieve = AsyncMock(return_value=[node])
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        # We will capture filters used
        captured_filters = []

        def capture_as_retriever(filters=None, **kwargs):
            captured_filters.append(filters)
            return mock_retriever

        mock_index.as_retriever.side_effect = capture_as_retriever

        with patch("nexus.tools.get_graph_index", return_value=mock_index):
            with patch("nexus.tools.get_vector_index", return_value=mock_index):
                with patch("nexus.tools.RERANKER_ENABLED", False):
                    with patch("httpx.AsyncClient", return_value=_ollama_mock("ans")):
                        await nexus_tools.answer_query("Q?", "PROJ", "")

        # Should have captured 2 sets of filters (one graph, one vector)
        assert len(captured_filters) == 2
        for filters in captured_filters:
            # filters is a MetadataFilters object. filters.filters is a list of ExactMatchFilter
            # It should only have project_id, not tenant_scope
            keys = [f.key for f in filters.filters]
            assert "project_id" in keys
            assert "tenant_scope" not in keys

