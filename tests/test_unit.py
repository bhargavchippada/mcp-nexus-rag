# Version: v1.0
"""
Unit tests for server.py helper functions.
All database calls are mocked — no live Qdrant or Neo4j required.
"""
import pytest
from unittest.mock import MagicMock, patch, call

import server


# ---------------------------------------------------------------------------
# _ALLOWED_META_KEYS allowlist guard
# ---------------------------------------------------------------------------

class TestAllowedMetaKeys:
    def test_get_distinct_qdrant_rejects_unknown_key(self):
        with pytest.raises(ValueError, match="Disallowed metadata key"):
            server.get_distinct_metadata_qdrant("arbitrary_field; DROP TABLE")

    def test_get_distinct_neo4j_rejects_unknown_key(self):
        with pytest.raises(ValueError, match="Disallowed metadata key"):
            server.get_distinct_metadata_neo4j("'; MATCH (n) DELETE n //")

    def test_get_distinct_qdrant_allows_project_id(self):
        """Allowlisted key must not raise — we mock the client to avoid real I/O."""
        with patch("server._scroll_qdrant_field", return_value={"P1", "P2"}):
            result = server.get_distinct_metadata_qdrant("project_id")
        assert set(result) == {"P1", "P2"}

    def test_get_distinct_neo4j_allows_tenant_scope(self):
        mock_session = MagicMock()
        mock_session.run.return_value = [{"value": "SCOPE_A"}, {"value": "SCOPE_B"}]
        mock_driver = MagicMock()
        mock_driver.__enter__ = lambda s: mock_driver
        mock_driver.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

        with patch("server._neo4j_driver", return_value=mock_driver):
            result = server.get_distinct_metadata_neo4j("tenant_scope")
        assert set(result) == {"SCOPE_A", "SCOPE_B"}


# ---------------------------------------------------------------------------
# delete_data_neo4j — correct Cypher branching
# ---------------------------------------------------------------------------

class TestDeleteNeo4j:
    def _make_driver(self):
        mock_session = MagicMock()
        mock_driver = MagicMock()
        mock_driver.__enter__ = lambda s: mock_driver
        mock_driver.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        return mock_driver, mock_session

    def test_without_scope_uses_project_only_cypher(self):
        mock_driver, mock_session = self._make_driver()
        with patch("server._neo4j_driver", return_value=mock_driver):
            server.delete_data_neo4j("MY_PROJECT")
        cypher, kwargs = mock_session.run.call_args
        assert "tenant_scope" not in cypher[0]
        assert kwargs == {"project_id": "MY_PROJECT"}

    def test_with_scope_includes_tenant_scope_in_cypher(self):
        mock_driver, mock_session = self._make_driver()
        with patch("server._neo4j_driver", return_value=mock_driver):
            server.delete_data_neo4j("MY_PROJECT", "MY_SCOPE")
        cypher, kwargs = mock_session.run.call_args
        assert "tenant_scope" in cypher[0]
        assert kwargs == {"project_id": "MY_PROJECT", "scope": "MY_SCOPE"}

    def test_neo4j_error_is_logged_not_raised(self):
        with patch("server._neo4j_driver", side_effect=Exception("connection refused")):
            # Must not propagate
            server.delete_data_neo4j("PROJ")


# ---------------------------------------------------------------------------
# delete_data_qdrant — correct filter construction
# ---------------------------------------------------------------------------

class TestDeleteQdrant:
    def _make_client(self):
        mock_client = MagicMock()
        return mock_client

    def test_without_scope_single_must_condition(self):
        mock_client = self._make_client()
        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            server.delete_data_qdrant("MY_PROJECT")
        mock_client.delete.assert_called_once()
        call_kwargs = mock_client.delete.call_args[1]
        must = call_kwargs["points_selector"].filter.must
        assert len(must) == 1
        assert must[0].key == "project_id"

    def test_with_scope_two_must_conditions(self):
        mock_client = self._make_client()
        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            server.delete_data_qdrant("MY_PROJECT", "MY_SCOPE")
        mock_client.delete.assert_called_once()
        call_kwargs = mock_client.delete.call_args[1]
        must = call_kwargs["points_selector"].filter.must
        assert len(must) == 2
        keys = {c.key for c in must}
        assert keys == {"project_id", "tenant_scope"}

    def test_qdrant_error_is_logged_not_raised(self):
        with patch("qdrant_client.QdrantClient", side_effect=Exception("timeout")):
            server.delete_data_qdrant("PROJ")


# ---------------------------------------------------------------------------
# delete_tenant_data — calls both backends
# ---------------------------------------------------------------------------

class TestDeleteTenantData:
    @pytest.mark.asyncio
    async def test_calls_both_backends(self):
        with patch("server.delete_data_neo4j") as mock_neo4j, \
             patch("server.delete_data_qdrant") as mock_qdrant:
            result = await server.delete_tenant_data("PROJ", "SCOPE")
        mock_neo4j.assert_called_once_with("PROJ", "SCOPE")
        mock_qdrant.assert_called_once_with("PROJ", "SCOPE")
        assert "PROJ" in result
        assert "SCOPE" in result

    @pytest.mark.asyncio
    async def test_without_scope_omits_scope_from_message(self):
        with patch("server.delete_data_neo4j"), patch("server.delete_data_qdrant"):
            result = await server.delete_tenant_data("PROJ")
        assert "PROJ" in result
        assert "scope" not in result.lower()


# ---------------------------------------------------------------------------
# get_all_project_ids — merges and deduplicates both backends
# ---------------------------------------------------------------------------

class TestGetAllProjectIds:
    @pytest.mark.asyncio
    async def test_merges_and_deduplicates(self):
        with patch("server.get_distinct_metadata_neo4j", return_value=["A", "B"]), \
             patch("server.get_distinct_metadata_qdrant", return_value=["B", "C"]):
            result = await server.get_all_project_ids()
        assert result == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_returns_sorted(self):
        with patch("server.get_distinct_metadata_neo4j", return_value=["Z", "M"]), \
             patch("server.get_distinct_metadata_qdrant", return_value=["A"]):
            result = await server.get_all_project_ids()
        assert result == sorted(result)


# ---------------------------------------------------------------------------
# get_all_tenant_scopes — global vs project-filtered paths
# ---------------------------------------------------------------------------

class TestGetAllTenantScopes:
    @pytest.mark.asyncio
    async def test_global_path_merges_both_backends(self):
        with patch("server.get_distinct_metadata_neo4j", return_value=["SCOPE_A"]), \
             patch("server.get_distinct_metadata_qdrant", return_value=["SCOPE_B"]):
            result = await server.get_all_tenant_scopes()
        assert "SCOPE_A" in result
        assert "SCOPE_B" in result

    @pytest.mark.asyncio
    async def test_project_filter_path(self):
        mock_session = MagicMock()
        mock_session.run.return_value = [{"value": "GRAPH_SCOPE"}]
        mock_driver = MagicMock()
        mock_driver.__enter__ = lambda s: mock_driver
        mock_driver.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

        with patch("server._neo4j_driver", return_value=mock_driver), \
             patch("server._scroll_qdrant_field", return_value={"QDRANT_SCOPE"}):
            result = await server.get_all_tenant_scopes(project_id="PROJ")
        assert "GRAPH_SCOPE" in result
        assert "QDRANT_SCOPE" in result


# ---------------------------------------------------------------------------
# setup_settings — singleton guard
# ---------------------------------------------------------------------------

class TestSetupSettings:
    def test_is_idempotent(self):
        """Calling setup_settings twice must not re-instantiate models."""
        # Reset for this test
        original = server._settings_initialized
        try:
            server._settings_initialized = True  # pretend already set
            with patch("server.Ollama") as mock_llm, \
                 patch("server.OllamaEmbedding") as mock_embed:
                server.setup_settings()
            mock_llm.assert_not_called()
            mock_embed.assert_not_called()
        finally:
            server._settings_initialized = original
