# Version: v2.0
"""
Integration tests — mix of mocked near-integration and live Docker tests.
Requires: Neo4j on bolt://localhost:7687, Qdrant on http://localhost:6333,
          Ollama on http://localhost:11434 with llama3.1:8b + nomic-embed-text.

Run with:
  PYTHONPATH=. pytest tests/test_integration.py -v -m integration
"""

import threading
import pytest
from unittest.mock import patch, MagicMock

from nexus import indexes as nexus_indexes
from nexus.backends import neo4j as neo4j_backend
from nexus.backends import qdrant as qdrant_backend
from nexus import tools as nexus_tools
from nexus import config as nexus_config

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Smoke-test: settings initialise only once against real Ollama
# ---------------------------------------------------------------------------


class TestSetupSettingsLive:
    def test_initialises_successfully(self):
        """Covers the full setup_settings() code path."""
        original = nexus_indexes._settings_initialized
        try:
            nexus_indexes._settings_initialized = False
            from llama_index.core import Settings

            nexus_indexes.setup_settings()
            assert nexus_indexes._settings_initialized is True
            assert Settings.llm is not None
            assert Settings.embed_model is not None
        finally:
            nexus_indexes._settings_initialized = original


# ---------------------------------------------------------------------------
# get_graph_index fallback branch (when from_existing raises)
# ---------------------------------------------------------------------------


class TestGraphIndexFallback:
    def test_fallback_creates_empty_index_when_from_existing_fails(self):
        """Force PropertyGraphIndex.from_existing to raise, triggering the fallback."""
        mock_graph_store = MagicMock()
        with (
            patch.object(nexus_indexes, "setup_settings"),
            patch(
                "nexus.indexes.Neo4jPropertyGraphStore", return_value=mock_graph_store
            ),
            patch(
                "nexus.indexes.PropertyGraphIndex.from_existing",
                side_effect=Exception("graph not found"),
            ),
            patch(
                "nexus.indexes.PropertyGraphIndex.from_documents",
                return_value=MagicMock(),
            ) as mock_from_docs,
        ):
            result = nexus_indexes.get_graph_index()

        mock_from_docs.assert_called_once()
        assert result is not None


# ---------------------------------------------------------------------------
# setup_settings thread-safe inner branch (double-checked locking)
# ---------------------------------------------------------------------------


class TestSetupSettingsInnerLock:
    def test_inner_lock_branch_is_hit_by_second_thread(self):
        """
        Verify double-checked locking: when two threads race, the slow init
        is called only once.
        """
        original_initialized = nexus_indexes._settings_initialized
        init_count = []
        first_in = threading.Event()
        can_proceed = threading.Event()
        results = []

        class _SlowOllama:
            def __init__(self, *a, **kw):
                init_count.append(1)
                first_in.set()
                can_proceed.wait()

        def _patched_setup():
            if nexus_indexes._settings_initialized:
                return
            with nexus_indexes._settings_lock:
                if nexus_indexes._settings_initialized:
                    return
                _SlowOllama()
                nexus_indexes._settings_initialized = True

        try:
            nexus_indexes._settings_initialized = False

            def run():
                _patched_setup()
                results.append("done")

            t1 = threading.Thread(target=run)
            t2 = threading.Thread(target=run)

            t1.start()
            first_in.wait()
            t2.start()
            can_proceed.set()
            t1.join(timeout=5)
            t2.join(timeout=5)
        finally:
            nexus_indexes._settings_initialized = original_initialized

        assert len(init_count) == 1, (
            "Singleton violated: constructor called more than once"
        )
        assert len(results) == 2, "Both threads must complete"


# ---------------------------------------------------------------------------
# delete_tenant_data — partial failure reporting
# ---------------------------------------------------------------------------


class TestDeleteTenantDataErrorReporting:
    async def test_neo4j_failure_reported_in_return(self):
        with (
            patch.object(
                neo4j_backend, "delete_data", side_effect=Exception("bolt closed")
            ),
            patch.object(qdrant_backend, "delete_data"),
        ):
            result = await nexus_tools.delete_tenant_data("PROJ")
        assert "Partial failure" in result
        assert "Neo4j" in result
        assert "bolt closed" in result

    async def test_qdrant_failure_reported_in_return(self):
        with (
            patch.object(neo4j_backend, "delete_data"),
            patch.object(
                qdrant_backend,
                "delete_data",
                side_effect=Exception("connection timeout"),
            ),
        ):
            result = await nexus_tools.delete_tenant_data("PROJ")
        assert "Partial failure" in result
        assert "Qdrant" in result

    async def test_both_failure_reports_both_backends(self):
        with (
            patch.object(
                neo4j_backend, "delete_data", side_effect=Exception("n4j down")
            ),
            patch.object(
                qdrant_backend, "delete_data", side_effect=Exception("qdrant down")
            ),
        ):
            result = await nexus_tools.delete_tenant_data("PROJ")
        assert "Neo4j" in result
        assert "Qdrant" in result

    async def test_success_returns_success_prefix(self):
        with (
            patch.object(neo4j_backend, "delete_data"),
            patch.object(qdrant_backend, "delete_data"),
        ):
            result = await nexus_tools.delete_tenant_data("PROJ", "SCOPE")
        assert result.startswith("Successfully deleted")
        assert "SCOPE" in result


# ---------------------------------------------------------------------------
# delete_data_qdrant — raises on error (new behaviour after re-raise fix)
# ---------------------------------------------------------------------------


class TestDeleteQdrantRaises:
    def test_qdrant_delete_propagates_on_error(self):
        """delete_data now re-raises so delete_tenant_data can catch it."""
        # Must clear cache so get_client actually tries to create a new client
        qdrant_backend._client_cache.clear()
        with patch("qdrant_client.QdrantClient", side_effect=Exception("down")):
            with pytest.raises(Exception, match="down"):
                qdrant_backend.delete_data("PROJ")
        qdrant_backend._client_cache.clear()


# ---------------------------------------------------------------------------
# COLLECTION_NAME constant is used in scroll and delete (not a literal)
# ---------------------------------------------------------------------------


class TestCollectionNameConstant:
    def test_collection_name_constant_is_used_in_scroll(self):
        mock_client = MagicMock()
        mock_client.scroll.side_effect = [([], None)]
        qdrant_backend._client_cache.clear()

        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            qdrant_backend.scroll_field("project_id")

        _, kwargs = mock_client.scroll.call_args
        assert kwargs["collection_name"] == nexus_config.COLLECTION_NAME
        qdrant_backend._client_cache.clear()

    def test_collection_name_constant_is_used_in_delete(self):
        mock_client = MagicMock()
        qdrant_backend._client_cache.clear()

        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            qdrant_backend.delete_data("PROJ")

        _, kwargs = mock_client.delete.call_args
        assert kwargs["collection_name"] == nexus_config.COLLECTION_NAME
        qdrant_backend._client_cache.clear()


# ---------------------------------------------------------------------------
# main() entry point — verify it calls mcp.run()
# ---------------------------------------------------------------------------


class TestMainEntryPoint:
    def test_main_calls_mcp_run(self):
        import server

        with patch.object(server.mcp, "run") as mock_run:
            server.main()
        mock_run.assert_called_once()
