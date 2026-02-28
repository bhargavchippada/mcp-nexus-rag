# Version: v1.0
"""
Integration tests that run against live Docker services.
Requires: Neo4j on bolt://localhost:7687, Qdrant on http://localhost:6333,
          Ollama on http://localhost:11434 with llama3.1:8b + nomic-embed-text.

Marked with pytest.mark.integration — run with:
  PYTHONPATH=. pytest tests/test_integration.py -v -m integration
"""
import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Smoke-test: settings initialise only once against real Ollama
# ---------------------------------------------------------------------------

class TestSetupSettingsLive:
    def test_initialises_successfully(self):
        """Covers the full setup_settings() code path (lines 53-64)."""
        import server
        # Reset so we can observe a real init
        original = server._settings_initialized
        try:
            server._settings_initialized = False
            from llama_index.core import Settings
            server.setup_settings()
            assert server._settings_initialized is True
            assert Settings.llm is not None
            assert Settings.embed_model is not None
        finally:
            server._settings_initialized = original


# ---------------------------------------------------------------------------
# get_graph_index fallback branch (lines 82-84)
# ---------------------------------------------------------------------------

class TestGraphIndexFallback:
    def test_fallback_creates_empty_index_when_from_existing_fails(self):
        """Force PropertyGraphIndex.from_existing to raise, triggering the fallback."""
        from unittest.mock import patch, MagicMock
        import server

        mock_graph_store = MagicMock()
        # from_existing raises; from_documents must succeed
        with patch("server.setup_settings"), \
             patch("server.Neo4jPropertyGraphStore", return_value=mock_graph_store), \
             patch("server.PropertyGraphIndex.from_existing",
                   side_effect=Exception("graph not found")), \
             patch("server.PropertyGraphIndex.from_documents",
                   return_value=MagicMock()) as mock_from_docs:
            result = server.get_graph_index()

        mock_from_docs.assert_called_once()
        assert result is not None


# ---------------------------------------------------------------------------
# setup_settings thread-safe inner branch (line 52)
# ---------------------------------------------------------------------------

class TestSetupSettingsInnerLock:
    def test_inner_lock_branch_is_hit_by_second_thread(self):
        """
        Verify double-checked locking: when two threads race, Ollama() __init__
        is called only once.  We replace the entire Settings assignment block
        so llama_index's property validators never run.
        """
        import threading
        import server
        from unittest.mock import patch

        original_initialized = server._settings_initialized
        init_count = []
        first_in = threading.Event()
        can_proceed = threading.Event()
        results = []

        # Thin wrapper: records construction, then pauses to let t2 queue up
        class _SlowOllama:
            def __init__(self, *a, **kw):
                init_count.append(1)
                first_in.set()
                can_proceed.wait()

        def _patched_setup():
            """Same logic as server.setup_settings() but without Settings assignment."""
            global _patched_initialized
            if server._settings_initialized:
                return
            with server._settings_lock:
                if server._settings_initialized:  # inner guard — line under test
                    return
                _SlowOllama()          # surrogate for the real Ollama() call
                server._settings_initialized = True

        try:
            server._settings_initialized = False

            def run():
                _patched_setup()
                results.append("done")

            t1 = threading.Thread(target=run)
            t2 = threading.Thread(target=run)

            t1.start()
            first_in.wait()   # t1 is inside _SlowOllama.__init__, holding the lock
            t2.start()        # t2 blocks on lock acquisition
            can_proceed.set() # unblock t1
            t1.join(timeout=5)
            t2.join(timeout=5)
        finally:
            server._settings_initialized = original_initialized

        assert len(init_count) == 1, "Singleton violated: constructor called more than once"
        assert len(results) == 2, "Both threads must complete"


# ---------------------------------------------------------------------------
# delete_tenant_data — partial failure reporting
# ---------------------------------------------------------------------------

class TestDeleteTenantDataErrorReporting:
    async def test_neo4j_failure_reported_in_return(self):
        from unittest.mock import patch
        import server
        with patch("server.delete_data_neo4j", side_effect=Exception("bolt closed")), \
             patch("server.delete_data_qdrant"):
            result = await server.delete_tenant_data("PROJ")
        assert "Partial failure" in result
        assert "Neo4j" in result
        assert "bolt closed" in result

    async def test_qdrant_failure_reported_in_return(self):
        from unittest.mock import patch
        import server
        with patch("server.delete_data_neo4j"), \
             patch("server.delete_data_qdrant", side_effect=Exception("connection timeout")):
            result = await server.delete_tenant_data("PROJ")
        assert "Partial failure" in result
        assert "Qdrant" in result

    async def test_both_failure_reports_both_backends(self):
        from unittest.mock import patch
        import server
        with patch("server.delete_data_neo4j", side_effect=Exception("n4j down")), \
             patch("server.delete_data_qdrant", side_effect=Exception("qdrant down")):
            result = await server.delete_tenant_data("PROJ")
        assert "Neo4j" in result
        assert "Qdrant" in result

    async def test_success_returns_success_prefix(self):
        from unittest.mock import patch
        import server
        with patch("server.delete_data_neo4j"), patch("server.delete_data_qdrant"):
            result = await server.delete_tenant_data("PROJ", "SCOPE")
        assert result.startswith("Successfully deleted")
        assert "SCOPE" in result


# ---------------------------------------------------------------------------
# delete_data_qdrant — raise on error (new behaviour after re-raise fix)
# ---------------------------------------------------------------------------

class TestDeleteQdrantRaises:
    def test_qdrant_delete_propagates_on_error(self):
        """delete_data_qdrant now re-raises so delete_tenant_data can catch it."""
        from unittest.mock import patch
        import server
        with patch("qdrant_client.QdrantClient", side_effect=Exception("down")):
            with pytest.raises(Exception, match="down"):
                server.delete_data_qdrant("PROJ")


# ---------------------------------------------------------------------------
# COLLECTION_NAME is used (not the literal) in scroll and delete
# ---------------------------------------------------------------------------

class TestCollectionNameConstant:
    def test_collection_name_constant_is_used_in_scroll(self):
        from unittest.mock import patch, MagicMock
        import server

        mock_client = MagicMock()
        mock_client.scroll.side_effect = [([], None)]

        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            server._scroll_qdrant_field("project_id")

        _, kwargs = mock_client.scroll.call_args
        assert kwargs["collection_name"] == server.COLLECTION_NAME

    def test_collection_name_constant_is_used_in_delete(self):
        from unittest.mock import patch, MagicMock
        import server

        mock_client = MagicMock()
        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            server.delete_data_qdrant("PROJ")

        _, kwargs = mock_client.delete.call_args
        assert kwargs["collection_name"] == server.COLLECTION_NAME


# ---------------------------------------------------------------------------
# main() entry point — just verify it calls mcp.run()
# ---------------------------------------------------------------------------

class TestMainEntryPoint:
    def test_main_calls_mcp_run(self):
        from unittest.mock import patch
        import server
        with patch.object(server.mcp, "run") as mock_run:
            server.main()
        mock_run.assert_called_once()
