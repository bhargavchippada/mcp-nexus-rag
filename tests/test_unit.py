# Version: v1.1
"""
Unit tests for server.py helper functions.
All database calls are mocked — no live Qdrant or Neo4j required.
asyncio_mode=auto (set in pyproject.toml) removes the need for @pytest.mark.asyncio.
"""
import pytest
from unittest.mock import MagicMock, patch, call

import server


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _make_neo4j_driver(session_records=None):
    """Build a MagicMock Neo4j driver whose session returns *session_records*."""
    mock_session = MagicMock()
    mock_session.run.return_value = session_records or []
    mock_driver = MagicMock()
    mock_driver.__enter__ = lambda s: mock_driver
    mock_driver.__exit__ = MagicMock(return_value=False)
    mock_driver.session.return_value.__enter__ = lambda s: mock_session
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return mock_driver, mock_session


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

    def test_empty_string_key_is_rejected(self):
        with pytest.raises(ValueError, match="Disallowed metadata key"):
            server.get_distinct_metadata_qdrant("")

    def test_get_distinct_qdrant_allows_project_id(self):
        with patch("server._scroll_qdrant_field", return_value={"P1", "P2"}):
            result = server.get_distinct_metadata_qdrant("project_id")
        assert set(result) == {"P1", "P2"}

    def test_get_distinct_neo4j_allows_tenant_scope(self):
        mock_driver, _ = _make_neo4j_driver([{"value": "SCOPE_A"}, {"value": "SCOPE_B"}])
        with patch("server._neo4j_driver", return_value=mock_driver):
            result = server.get_distinct_metadata_neo4j("tenant_scope")
        assert set(result) == {"SCOPE_A", "SCOPE_B"}

    def test_get_distinct_neo4j_returns_empty_on_error(self):
        with patch("server._neo4j_driver", side_effect=Exception("down")):
            result = server.get_distinct_metadata_neo4j("project_id")
        assert result == []

    def test_get_distinct_qdrant_returns_empty_on_error(self):
        with patch("server._scroll_qdrant_field", side_effect=Exception("timeout")):
            result = server.get_distinct_metadata_qdrant("project_id")
        assert result == []


# ---------------------------------------------------------------------------
# _scroll_qdrant_field — pagination loop
# ---------------------------------------------------------------------------

class TestScrollQdrantField:
    def test_collects_across_multiple_pages(self):
        """Simulates two pages returned by client.scroll."""
        mock_client = MagicMock()

        page1_records = [MagicMock(payload={"project_id": "A"}),
                         MagicMock(payload={"project_id": "B"})]
        page2_records = [MagicMock(payload={"project_id": "C"})]

        # First call returns offset="cursor", second returns offset=None (last page)
        mock_client.scroll.side_effect = [
            (page1_records, "cursor"),
            (page2_records, None),
        ]

        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            result = server._scroll_qdrant_field("project_id")

        assert result == {"A", "B", "C"}
        assert mock_client.scroll.call_count == 2

    def test_skips_records_without_the_key(self):
        mock_client = MagicMock()
        records = [
            MagicMock(payload={"project_id": "FOUND"}),
            MagicMock(payload={"other_key": "ignored"}),
            MagicMock(payload=None),
        ]
        mock_client.scroll.side_effect = [(records, None)]

        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            result = server._scroll_qdrant_field("project_id")

        assert result == {"FOUND"}

    def test_passes_qdrant_filter_to_scroll(self):
        mock_client = MagicMock()
        mock_client.scroll.side_effect = [([], None)]
        mock_filter = MagicMock()

        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            server._scroll_qdrant_field("project_id", qdrant_filter=mock_filter)

        _, kwargs = mock_client.scroll.call_args
        assert kwargs["scroll_filter"] is mock_filter


# ---------------------------------------------------------------------------
# delete_data_neo4j — Cypher branching
# ---------------------------------------------------------------------------

class TestDeleteNeo4j:
    def test_without_scope_uses_project_only_cypher(self):
        mock_driver, mock_session = _make_neo4j_driver()
        with patch("server._neo4j_driver", return_value=mock_driver):
            server.delete_data_neo4j("MY_PROJECT")
        cypher, kwargs = mock_session.run.call_args
        assert "tenant_scope" not in cypher[0]
        assert kwargs == {"project_id": "MY_PROJECT"}

    def test_with_scope_includes_tenant_scope_in_cypher(self):
        mock_driver, mock_session = _make_neo4j_driver()
        with patch("server._neo4j_driver", return_value=mock_driver):
            server.delete_data_neo4j("MY_PROJECT", "MY_SCOPE")
        cypher, kwargs = mock_session.run.call_args
        assert "tenant_scope" in cypher[0]
        assert kwargs == {"project_id": "MY_PROJECT", "scope": "MY_SCOPE"}

    def test_neo4j_error_is_swallowed(self):
        with patch("server._neo4j_driver", side_effect=Exception("connection refused")):
            server.delete_data_neo4j("PROJ")  # must not raise


# ---------------------------------------------------------------------------
# delete_data_qdrant — filter construction
# ---------------------------------------------------------------------------

class TestDeleteQdrant:
    def test_without_scope_single_must_condition(self):
        mock_client = MagicMock()
        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            server.delete_data_qdrant("MY_PROJECT")
        must = mock_client.delete.call_args[1]["points_selector"].filter.must
        assert len(must) == 1
        assert must[0].key == "project_id"

    def test_with_scope_two_must_conditions(self):
        mock_client = MagicMock()
        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            server.delete_data_qdrant("MY_PROJECT", "MY_SCOPE")
        must = mock_client.delete.call_args[1]["points_selector"].filter.must
        assert len(must) == 2
        assert {c.key for c in must} == {"project_id", "tenant_scope"}

    def test_qdrant_error_is_propagated(self):
        with patch("qdrant_client.QdrantClient", side_effect=Exception("timeout")):
            with pytest.raises(Exception, match="timeout"):
                server.delete_data_qdrant("PROJ")


# ---------------------------------------------------------------------------
# delete_tenant_data — calls both backends, return value
# ---------------------------------------------------------------------------

class TestDeleteTenantData:
    async def test_calls_both_backends(self):
        with patch("server.delete_data_neo4j") as mock_neo4j, \
             patch("server.delete_data_qdrant") as mock_qdrant:
            result = await server.delete_tenant_data("PROJ", "SCOPE")
        mock_neo4j.assert_called_once_with("PROJ", "SCOPE")
        mock_qdrant.assert_called_once_with("PROJ", "SCOPE")
        assert "PROJ" in result
        assert "SCOPE" in result

    async def test_without_scope_omits_scope_from_message(self):
        with patch("server.delete_data_neo4j"), patch("server.delete_data_qdrant"):
            result = await server.delete_tenant_data("PROJ")
        assert "PROJ" in result
        assert "scope" not in result.lower()

    async def test_empty_project_id_still_calls_backends(self):
        """Guard: empty string is falsy but must still propagate to backends."""
        with patch("server.delete_data_neo4j") as mock_neo4j, \
             patch("server.delete_data_qdrant") as mock_qdrant:
            await server.delete_tenant_data("", "")
        mock_neo4j.assert_called_once_with("", "")
        mock_qdrant.assert_called_once_with("", "")


# ---------------------------------------------------------------------------
# ingest_* tools — error path returns string, not raises
# ---------------------------------------------------------------------------

class TestIngestErrorPaths:
    async def test_ingest_vector_document_error_returns_string(self):
        with patch("server.get_vector_index", side_effect=Exception("DB down")):
            result = await server.ingest_vector_document("text", "PROJ", "SCOPE")
        assert "Error" in result
        assert "DB down" in result

    async def test_ingest_graph_document_error_returns_string(self):
        with patch("server.get_graph_index", side_effect=Exception("Neo4j offline")):
            result = await server.ingest_graph_document("text", "PROJ", "SCOPE")
        assert "Error" in result
        assert "Neo4j offline" in result


# ---------------------------------------------------------------------------
# get_vector_context / get_graph_context — no-results and error paths
# ---------------------------------------------------------------------------

class TestContextRetrieval:
    def _mock_index(self, nodes=None):
        mock_node = MagicMock()
        mock_node.node.get_content.return_value = "relevant content"
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes if nodes is not None else []
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever
        return mock_index

    async def test_get_vector_context_no_results(self):
        with patch("server.get_vector_index", return_value=self._mock_index([])):
            result = await server.get_vector_context("query", "PROJ", "SCOPE")
        assert "No Vector context found" in result

    async def test_get_vector_context_with_results(self):
        nodes = [MagicMock()]
        nodes[0].node.get_content.return_value = "match!"
        with patch("server.get_vector_index", return_value=self._mock_index(nodes)):
            result = await server.get_vector_context("query", "PROJ", "SCOPE")
        assert "match!" in result

    async def test_get_vector_context_error_returns_string(self):
        with patch("server.get_vector_index", side_effect=Exception("Qdrant exploded")):
            result = await server.get_vector_context("query", "PROJ", "SCOPE")
        assert "Error" in result

    async def test_get_graph_context_no_results(self):
        with patch("server.get_graph_index", return_value=self._mock_index([])):
            result = await server.get_graph_context("query", "PROJ", "SCOPE")
        assert "No Graph context found" in result

    async def test_get_graph_context_error_returns_string(self):
        with patch("server.get_graph_index", side_effect=Exception("Neo4j exploded")):
            result = await server.get_graph_context("query", "PROJ", "SCOPE")
        assert "Error" in result


# ---------------------------------------------------------------------------
# get_all_project_ids — merges, deduplicates, sorts
# ---------------------------------------------------------------------------

class TestGetAllProjectIds:
    async def test_merges_and_deduplicates(self):
        with patch("server.get_distinct_metadata_neo4j", return_value=["A", "B"]), \
             patch("server.get_distinct_metadata_qdrant", return_value=["B", "C"]):
            result = await server.get_all_project_ids()
        assert result == ["A", "B", "C"]

    async def test_returns_sorted(self):
        with patch("server.get_distinct_metadata_neo4j", return_value=["Z", "M"]), \
             patch("server.get_distinct_metadata_qdrant", return_value=["A"]):
            result = await server.get_all_project_ids()
        assert result == sorted(result)

    async def test_one_backend_down_returns_partial(self):
        with patch("server.get_distinct_metadata_neo4j", return_value=[]), \
             patch("server.get_distinct_metadata_qdrant", return_value=["QDRANT_ONLY"]):
            result = await server.get_all_project_ids()
        assert result == ["QDRANT_ONLY"]


# ---------------------------------------------------------------------------
# get_all_tenant_scopes — global vs project-filtered, partial-backend resilience
# ---------------------------------------------------------------------------

class TestGetAllTenantScopes:
    async def test_global_path_merges_both_backends(self):
        with patch("server.get_distinct_metadata_neo4j", return_value=["SCOPE_A"]), \
             patch("server.get_distinct_metadata_qdrant", return_value=["SCOPE_B"]):
            result = await server.get_all_tenant_scopes()
        assert "SCOPE_A" in result
        assert "SCOPE_B" in result

    async def test_project_filter_path(self):
        mock_driver, _ = _make_neo4j_driver([{"value": "GRAPH_SCOPE"}])
        with patch("server._neo4j_driver", return_value=mock_driver), \
             patch("server._scroll_qdrant_field", return_value={"QDRANT_SCOPE"}):
            result = await server.get_all_tenant_scopes(project_id="PROJ")
        assert "GRAPH_SCOPE" in result
        assert "QDRANT_SCOPE" in result

    async def test_project_filter_neo4j_down_returns_qdrant_only(self):
        with patch("server._neo4j_driver", side_effect=Exception("down")), \
             patch("server._scroll_qdrant_field", return_value={"QDRANT_SCOPE"}):
            result = await server.get_all_tenant_scopes(project_id="PROJ")
        assert "QDRANT_SCOPE" in result

    async def test_project_filter_qdrant_down_returns_neo4j_only(self):
        mock_driver, _ = _make_neo4j_driver([{"value": "GRAPH_SCOPE"}])
        with patch("server._neo4j_driver", return_value=mock_driver), \
             patch("server._scroll_qdrant_field", side_effect=Exception("down")):
            result = await server.get_all_tenant_scopes(project_id="PROJ")
        assert "GRAPH_SCOPE" in result


# ---------------------------------------------------------------------------
# setup_settings — singleton and thread-safe lock
# ---------------------------------------------------------------------------

class TestSetupSettings:
    def test_is_idempotent(self):
        original = server._settings_initialized
        try:
            server._settings_initialized = True
            with patch("server.Ollama") as mock_llm, \
                 patch("server.OllamaEmbedding") as mock_embed:
                server.setup_settings()
            mock_llm.assert_not_called()
            mock_embed.assert_not_called()
        finally:
            server._settings_initialized = original

    def test_lock_exists(self):
        import threading
        assert isinstance(server._settings_lock, type(threading.Lock()))


# ---------------------------------------------------------------------------
# Deduplication — _content_hash
# ---------------------------------------------------------------------------

class TestContentHash:
    def test_same_text_same_project_scope_is_identical(self):
        h1 = server._content_hash("Hello", "PROJ", "SCOPE")
        h2 = server._content_hash("Hello", "PROJ", "SCOPE")
        assert h1 == h2

    def test_different_project_gives_different_hash(self):
        h1 = server._content_hash("Hello", "PROJ_A", "SCOPE")
        h2 = server._content_hash("Hello", "PROJ_B", "SCOPE")
        assert h1 != h2

    def test_different_scope_gives_different_hash(self):
        h1 = server._content_hash("Hello", "PROJ", "SCOPE_A")
        h2 = server._content_hash("Hello", "PROJ", "SCOPE_B")
        assert h1 != h2

    def test_different_text_gives_different_hash(self):
        h1 = server._content_hash("Hello", "PROJ", "SCOPE")
        h2 = server._content_hash("World", "PROJ", "SCOPE")
        assert h1 != h2

    def test_returns_hex_string(self):
        h = server._content_hash("x", "P", "S")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Deduplication — _is_duplicate_qdrant
# ---------------------------------------------------------------------------

class TestIsDuplicateQdrant:
    def test_returns_true_when_record_found(self):
        mock_client = MagicMock()
        mock_client.scroll.return_value = (["fake_record"], None)
        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            assert server._is_duplicate_qdrant("abc", "PROJ", "SCOPE") is True

    def test_returns_false_when_no_record(self):
        mock_client = MagicMock()
        mock_client.scroll.return_value = ([], None)
        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            assert server._is_duplicate_qdrant("abc", "PROJ", "SCOPE") is False

    def test_fail_open_on_exception(self):
        with patch("qdrant_client.QdrantClient", side_effect=Exception("timeout")):
            # Must return False (fail-open), not raise
            assert server._is_duplicate_qdrant("abc", "PROJ", "SCOPE") is False

    def test_scroll_uses_all_three_filters(self):
        mock_client = MagicMock()
        mock_client.scroll.return_value = ([], None)
        with patch("qdrant_client.QdrantClient", return_value=mock_client):
            server._is_duplicate_qdrant("HASH123", "MY_PROJ", "MY_SCOPE")
        _, kwargs = mock_client.scroll.call_args
        keys = {c.key for c in kwargs["scroll_filter"].must}
        assert keys == {"project_id", "tenant_scope", "content_hash"}


# ---------------------------------------------------------------------------
# Deduplication — _is_duplicate_neo4j
# ---------------------------------------------------------------------------

class TestIsDuplicateNeo4j:
    def _make_driver_with_single(self, single_return):
        mock_session = MagicMock()
        mock_session.run.return_value.single.return_value = single_return
        mock_driver = MagicMock()
        mock_driver.__enter__ = lambda s: mock_driver
        mock_driver.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        return mock_driver

    def test_returns_true_when_exists(self):
        driver = self._make_driver_with_single({"exists": True})
        with patch("server._neo4j_driver", return_value=driver):
            assert server._is_duplicate_neo4j("abc", "PROJ", "SCOPE") is True

    def test_returns_false_when_not_exists(self):
        driver = self._make_driver_with_single({"exists": False})
        with patch("server._neo4j_driver", return_value=driver):
            assert server._is_duplicate_neo4j("abc", "PROJ", "SCOPE") is False

    def test_returns_false_when_no_record(self):
        driver = self._make_driver_with_single(None)
        with patch("server._neo4j_driver", return_value=driver):
            assert server._is_duplicate_neo4j("abc", "PROJ", "SCOPE") is False

    def test_fail_open_on_exception(self):
        with patch("server._neo4j_driver", side_effect=Exception("bolt down")):
            assert server._is_duplicate_neo4j("abc", "PROJ", "SCOPE") is False


# ---------------------------------------------------------------------------
# Deduplication — ingest tools with dedup gate
# ---------------------------------------------------------------------------

class TestIngestVectorDedup:
    async def test_skips_on_duplicate(self):
        with patch("server._content_hash", return_value="HASH"), \
             patch("server._is_duplicate_qdrant", return_value=True) as mock_check, \
             patch("server.get_vector_index") as mock_index:
            result = await server.ingest_vector_document("text", "PROJ", "SCOPE")
        assert "Skipped" in result
        mock_index.assert_not_called()
        mock_check.assert_called_once_with("HASH", "PROJ", "SCOPE")

    async def test_ingests_on_first_time(self):
        mock_index = MagicMock()
        with patch("server._content_hash", return_value="HASH"), \
             patch("server._is_duplicate_qdrant", return_value=False), \
             patch("server.get_vector_index", return_value=mock_index):
            result = await server.ingest_vector_document("text", "PROJ", "SCOPE")
        assert "Successfully" in result
        mock_index.insert.assert_called_once()

    async def test_doc_id_set_to_hash(self):
        mock_index = MagicMock()
        with patch("server._content_hash", return_value="DEADBEEF"), \
             patch("server._is_duplicate_qdrant", return_value=False), \
             patch("server.get_vector_index", return_value=mock_index):
            await server.ingest_vector_document("text", "PROJ", "SCOPE")
        doc = mock_index.insert.call_args[0][0]
        assert doc.doc_id == "DEADBEEF"

    async def test_content_hash_in_metadata(self):
        mock_index = MagicMock()
        with patch("server._content_hash", return_value="CAFEF00D"), \
             patch("server._is_duplicate_qdrant", return_value=False), \
             patch("server.get_vector_index", return_value=mock_index):
            await server.ingest_vector_document("text", "PROJ", "SCOPE")
        doc = mock_index.insert.call_args[0][0]
        assert doc.metadata["content_hash"] == "CAFEF00D"


class TestIngestGraphDedup:
    async def test_skips_on_duplicate(self):
        with patch("server._content_hash", return_value="HASH"), \
             patch("server._is_duplicate_neo4j", return_value=True) as mock_check, \
             patch("server.get_graph_index") as mock_index:
            result = await server.ingest_graph_document("text", "PROJ", "SCOPE")
        assert "Skipped" in result
        mock_index.assert_not_called()
        mock_check.assert_called_once_with("HASH", "PROJ", "SCOPE")

    async def test_ingests_on_first_time(self):
        mock_index = MagicMock()
        with patch("server._content_hash", return_value="HASH"), \
             patch("server._is_duplicate_neo4j", return_value=False), \
             patch("server.get_graph_index", return_value=mock_index):
            result = await server.ingest_graph_document("text", "PROJ", "SCOPE")
        assert "Successfully" in result
        mock_index.insert.assert_called_once()

    async def test_doc_id_set_to_hash(self):
        mock_index = MagicMock()
        with patch("server._content_hash", return_value="GRAPHHASH"), \
             patch("server._is_duplicate_neo4j", return_value=False), \
             patch("server.get_graph_index", return_value=mock_index):
            await server.ingest_graph_document("text", "PROJ", "SCOPE")
        doc = mock_index.insert.call_args[0][0]
        assert doc.doc_id == "GRAPHHASH"
