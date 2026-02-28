# Version: v2.0
"""
Unit tests for the nexus/ package.
All database calls are mocked — no live Qdrant or Neo4j required.
asyncio_mode=auto (set in pyproject.toml) removes the need for @pytest.mark.asyncio decorators.
"""
import threading
import pytest
from unittest.mock import MagicMock, patch

from nexus import config as nexus_config
from nexus import dedup as nexus_dedup
from nexus.backends import neo4j as neo4j_backend
from nexus.backends import qdrant as qdrant_backend
from nexus import indexes as nexus_indexes
from nexus import tools as nexus_tools


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------

def _make_neo4j_driver(session_records=None):
    """Build a MagicMock Neo4j driver whose session.run() returns *session_records*."""
    mock_session = MagicMock()
    mock_session.run.return_value = session_records or []
    mock_driver = MagicMock()
    mock_driver.__enter__ = lambda s: mock_driver
    mock_driver.__exit__ = MagicMock(return_value=False)
    mock_driver.session.return_value.__enter__ = lambda s: mock_session
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return mock_driver, mock_session


def _make_neo4j_driver_with_single(single_return):
    """Build a MagicMock Neo4j driver whose session.run().single() returns *single_return*."""
    mock_session = MagicMock()
    mock_session.run.return_value.single.return_value = single_return
    mock_driver = MagicMock()
    mock_driver.__enter__ = lambda s: mock_driver
    mock_driver.__exit__ = MagicMock(return_value=False)
    mock_driver.session.return_value.__enter__ = lambda s: mock_session
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return mock_driver


# ---------------------------------------------------------------------------
# nexus.config — ALLOWED_META_KEYS allowlist guard
# ---------------------------------------------------------------------------

class TestAllowedMetaKeys:
    def test_get_distinct_qdrant_rejects_unknown_key(self):
        with pytest.raises(ValueError, match="Disallowed metadata key"):
            qdrant_backend.get_distinct_metadata("arbitrary_field; DROP TABLE")

    def test_get_distinct_neo4j_rejects_unknown_key(self):
        with pytest.raises(ValueError, match="Disallowed metadata key"):
            neo4j_backend.get_distinct_metadata("'; MATCH (n) DELETE n //")

    def test_empty_string_key_is_rejected(self):
        with pytest.raises(ValueError, match="Disallowed metadata key"):
            qdrant_backend.get_distinct_metadata("")

    def test_content_hash_is_allowed(self):
        assert "content_hash" in nexus_config.ALLOWED_META_KEYS

    def test_get_distinct_qdrant_allows_project_id(self):
        with patch.object(qdrant_backend, "scroll_field", return_value={"P1", "P2"}):
            result = qdrant_backend.get_distinct_metadata("project_id")
        assert set(result) == {"P1", "P2"}

    def test_get_distinct_neo4j_allows_tenant_scope(self):
        mock_driver, _ = _make_neo4j_driver([{"value": "SCOPE_A"}, {"value": "SCOPE_B"}])
        with patch.object(neo4j_backend, "neo4j_driver", return_value=mock_driver):
            result = neo4j_backend.get_distinct_metadata("tenant_scope")
        assert set(result) == {"SCOPE_A", "SCOPE_B"}

    def test_get_distinct_neo4j_returns_empty_on_error(self):
        with patch.object(neo4j_backend, "neo4j_driver", side_effect=Exception("down")):
            result = neo4j_backend.get_distinct_metadata("project_id")
        assert result == []

    def test_get_distinct_qdrant_returns_empty_on_error(self):
        with patch.object(qdrant_backend, "scroll_field", side_effect=Exception("timeout")):
            result = qdrant_backend.get_distinct_metadata("project_id")
        assert result == []


# ---------------------------------------------------------------------------
# nexus.backends.qdrant — scroll_field pagination loop
# ---------------------------------------------------------------------------

class TestScrollQdrantField:
    def test_collects_across_multiple_pages(self):
        page1 = [MagicMock(payload={"project_id": "A"}),
                 MagicMock(payload={"project_id": "B"})]
        page2 = [MagicMock(payload={"project_id": "C"})]
        mock_client = MagicMock()
        mock_client.scroll.side_effect = [(page1, "cursor"), (page2, None)]

        with patch.object(qdrant_backend, "_get_client", return_value=mock_client):
            result = qdrant_backend.scroll_field("project_id")

        assert result == {"A", "B", "C"}
        assert mock_client.scroll.call_count == 2

    def test_skips_records_without_the_key(self):
        records = [
            MagicMock(payload={"project_id": "FOUND"}),
            MagicMock(payload={"other_key": "ignored"}),
            MagicMock(payload=None),
        ]
        mock_client = MagicMock()
        mock_client.scroll.side_effect = [(records, None)]

        with patch.object(qdrant_backend, "_get_client", return_value=mock_client):
            result = qdrant_backend.scroll_field("project_id")

        assert result == {"FOUND"}

    def test_passes_qdrant_filter_to_scroll(self):
        mock_client = MagicMock()
        mock_client.scroll.side_effect = [([], None)]
        mock_filter = MagicMock()

        with patch.object(qdrant_backend, "_get_client", return_value=mock_client):
            qdrant_backend.scroll_field("project_id", qdrant_filter=mock_filter)

        _, kwargs = mock_client.scroll.call_args
        assert kwargs["scroll_filter"] is mock_filter


# ---------------------------------------------------------------------------
# nexus.backends.qdrant — _get_client caching (Bug fix #2)
# ---------------------------------------------------------------------------

class TestQdrantClientCache:
    def test_same_url_returns_same_instance(self):
        # Clear cache so we test fresh creation
        qdrant_backend._client_cache.clear()
        mock_client = MagicMock()
        with patch("qdrant_client.QdrantClient", return_value=mock_client) as mock_cls:
            c1 = qdrant_backend._get_client("http://fake-qdrant:9999")
            c2 = qdrant_backend._get_client("http://fake-qdrant:9999")
        # QdrantClient constructor should only be called once
        assert mock_cls.call_count == 1
        assert c1 is c2
        qdrant_backend._client_cache.clear()

    def test_different_url_creates_separate_instance(self):
        qdrant_backend._client_cache.clear()
        mock_a = MagicMock()
        mock_b = MagicMock()
        with patch("qdrant_client.QdrantClient", side_effect=[mock_a, mock_b]):
            ca = qdrant_backend._get_client("http://url-a")
            cb = qdrant_backend._get_client("http://url-b")
        assert ca is mock_a
        assert cb is mock_b
        qdrant_backend._client_cache.clear()


# ---------------------------------------------------------------------------
# nexus.backends.neo4j — delete_data Cypher branching (Bug fix #1: re-raises)
# ---------------------------------------------------------------------------

class TestDeleteNeo4j:
    def test_without_scope_uses_project_only_cypher(self):
        mock_driver, mock_session = _make_neo4j_driver()
        with patch.object(neo4j_backend, "neo4j_driver", return_value=mock_driver):
            neo4j_backend.delete_data("MY_PROJECT")
        cypher, kwargs = mock_session.run.call_args
        assert "tenant_scope" not in cypher[0]
        assert kwargs == {"project_id": "MY_PROJECT"}

    def test_with_scope_includes_tenant_scope_in_cypher(self):
        mock_driver, mock_session = _make_neo4j_driver()
        with patch.object(neo4j_backend, "neo4j_driver", return_value=mock_driver):
            neo4j_backend.delete_data("MY_PROJECT", "MY_SCOPE")
        cypher, kwargs = mock_session.run.call_args
        assert "tenant_scope" in cypher[0]
        assert kwargs == {"project_id": "MY_PROJECT", "scope": "MY_SCOPE"}

    def test_neo4j_error_is_re_raised(self):
        """Bug fix #1: delete_data_neo4j now re-raises instead of swallowing."""
        with patch.object(neo4j_backend, "neo4j_driver", side_effect=Exception("connection refused")):
            with pytest.raises(Exception, match="connection refused"):
                neo4j_backend.delete_data("PROJ")


# ---------------------------------------------------------------------------
# nexus.backends.qdrant — delete_data filter construction
# ---------------------------------------------------------------------------

class TestDeleteQdrant:
    def test_without_scope_single_must_condition(self):
        mock_client = MagicMock()
        with patch.object(qdrant_backend, "_get_client", return_value=mock_client):
            qdrant_backend.delete_data("MY_PROJECT")
        must = mock_client.delete.call_args[1]["points_selector"].filter.must
        assert len(must) == 1
        assert must[0].key == "project_id"

    def test_with_scope_two_must_conditions(self):
        mock_client = MagicMock()
        with patch.object(qdrant_backend, "_get_client", return_value=mock_client):
            qdrant_backend.delete_data("MY_PROJECT", "MY_SCOPE")
        must = mock_client.delete.call_args[1]["points_selector"].filter.must
        assert len(must) == 2
        assert {c.key for c in must} == {"project_id", "tenant_scope"}

    def test_qdrant_error_is_propagated(self):
        with patch.object(qdrant_backend, "_get_client", side_effect=Exception("timeout")):
            with pytest.raises(Exception, match="timeout"):
                qdrant_backend.delete_data("PROJ")


# ---------------------------------------------------------------------------
# nexus.tools.delete_tenant_data — calls both backends, guards empty project_id
# ---------------------------------------------------------------------------

class TestDeleteTenantData:
    async def test_calls_both_backends(self):
        with patch.object(neo4j_backend, "delete_data") as mock_neo4j, \
             patch.object(qdrant_backend, "delete_data") as mock_qdrant:
            result = await nexus_tools.delete_tenant_data("PROJ", "SCOPE")
        mock_neo4j.assert_called_once_with("PROJ", "SCOPE")
        mock_qdrant.assert_called_once_with("PROJ", "SCOPE")
        assert "Successfully" in result
        assert "PROJ" in result

    async def test_without_scope_omits_scope_from_message(self):
        with patch.object(neo4j_backend, "delete_data"), \
             patch.object(qdrant_backend, "delete_data"):
            result = await nexus_tools.delete_tenant_data("PROJ")
        assert "PROJ" in result
        assert "scope" not in result.lower()

    async def test_empty_project_id_is_rejected(self):
        """Bug fix #4: empty project_id must return an error, not delete everything."""
        result = await nexus_tools.delete_tenant_data("")
        assert "Error" in result

    async def test_whitespace_project_id_is_rejected(self):
        result = await nexus_tools.delete_tenant_data("   ")
        assert "Error" in result

    async def test_partial_failure_reported(self):
        with patch.object(neo4j_backend, "delete_data", side_effect=Exception("neo4j down")), \
             patch.object(qdrant_backend, "delete_data"):
            result = await nexus_tools.delete_tenant_data("PROJ")
        assert "Partial failure" in result
        assert "Neo4j" in result


# ---------------------------------------------------------------------------
# nexus.tools — input validation (Bug fix #3: empty inputs rejected)
# ---------------------------------------------------------------------------

class TestIngestInputValidation:
    async def test_empty_text_rejected_vector(self):
        result = await nexus_tools.ingest_vector_document("", "PROJ", "SCOPE")
        assert "Error" in result

    async def test_whitespace_text_rejected_vector(self):
        result = await nexus_tools.ingest_vector_document("   ", "PROJ", "SCOPE")
        assert "Error" in result

    async def test_empty_project_id_rejected_vector(self):
        result = await nexus_tools.ingest_vector_document("text", "", "SCOPE")
        assert "Error" in result

    async def test_empty_scope_rejected_vector(self):
        result = await nexus_tools.ingest_vector_document("text", "PROJ", "")
        assert "Error" in result

    async def test_empty_text_rejected_graph(self):
        result = await nexus_tools.ingest_graph_document("", "PROJ", "SCOPE")
        assert "Error" in result

    async def test_empty_project_id_rejected_graph(self):
        result = await nexus_tools.ingest_graph_document("text", "", "SCOPE")
        assert "Error" in result

    async def test_empty_scope_rejected_graph(self):
        result = await nexus_tools.ingest_graph_document("text", "PROJ", "")
        assert "Error" in result


# ---------------------------------------------------------------------------
# nexus.tools — ingest error paths
# ---------------------------------------------------------------------------

class TestIngestErrorPaths:
    async def test_ingest_vector_document_error_returns_string(self):
        with patch("nexus.tools.get_vector_index", side_effect=Exception("DB down")), \
             patch.object(qdrant_backend, "is_duplicate", return_value=False):
            result = await nexus_tools.ingest_vector_document("text", "PROJ", "SCOPE")
        assert "Error" in result
        assert "DB down" in result

    async def test_ingest_graph_document_error_returns_string(self):
        with patch("nexus.tools.get_graph_index", side_effect=Exception("Neo4j offline")), \
             patch.object(neo4j_backend, "is_duplicate", return_value=False):
            result = await nexus_tools.ingest_graph_document("text", "PROJ", "SCOPE")
        assert "Error" in result
        assert "Neo4j offline" in result


# ---------------------------------------------------------------------------
# nexus.tools — context retrieval
# ---------------------------------------------------------------------------

class TestContextRetrieval:
    def _mock_index(self, nodes=None):
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes if nodes is not None else []
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever
        return mock_index

    async def test_get_vector_context_no_results(self):
        with patch("nexus.tools.get_vector_index", return_value=self._mock_index([])):
            result = await nexus_tools.get_vector_context("query", "PROJ", "SCOPE")
        assert "No Vector context found" in result

    async def test_get_vector_context_with_results(self):
        node = MagicMock()
        node.node.get_content.return_value = "match!"
        with patch("nexus.tools.get_vector_index", return_value=self._mock_index([node])):
            result = await nexus_tools.get_vector_context("query", "PROJ", "SCOPE")
        assert "match!" in result

    async def test_get_vector_context_error_returns_string(self):
        with patch("nexus.tools.get_vector_index", side_effect=Exception("Qdrant exploded")):
            result = await nexus_tools.get_vector_context("query", "PROJ", "SCOPE")
        assert "Error" in result

    async def test_get_graph_context_no_results(self):
        with patch("nexus.tools.get_graph_index", return_value=self._mock_index([])):
            result = await nexus_tools.get_graph_context("query", "PROJ", "SCOPE")
        assert "No Graph context found" in result

    async def test_get_graph_context_error_returns_string(self):
        with patch("nexus.tools.get_graph_index", side_effect=Exception("Neo4j exploded")):
            result = await nexus_tools.get_graph_context("query", "PROJ", "SCOPE")
        assert "Error" in result


# ---------------------------------------------------------------------------
# nexus.tools — get_all_project_ids
# ---------------------------------------------------------------------------

class TestGetAllProjectIds:
    async def test_merges_and_deduplicates(self):
        with patch.object(neo4j_backend, "get_distinct_metadata", return_value=["A", "B"]), \
             patch.object(qdrant_backend, "get_distinct_metadata", return_value=["B", "C"]):
            result = await nexus_tools.get_all_project_ids()
        assert result == ["A", "B", "C"]

    async def test_returns_sorted(self):
        with patch.object(neo4j_backend, "get_distinct_metadata", return_value=["Z", "M"]), \
             patch.object(qdrant_backend, "get_distinct_metadata", return_value=["A"]):
            result = await nexus_tools.get_all_project_ids()
        assert result == sorted(result)

    async def test_one_backend_down_returns_partial(self):
        with patch.object(neo4j_backend, "get_distinct_metadata", return_value=[]), \
             patch.object(qdrant_backend, "get_distinct_metadata", return_value=["QDRANT_ONLY"]):
            result = await nexus_tools.get_all_project_ids()
        assert result == ["QDRANT_ONLY"]


# ---------------------------------------------------------------------------
# nexus.tools — get_all_tenant_scopes
# ---------------------------------------------------------------------------

class TestGetAllTenantScopes:
    async def test_global_path_merges_both_backends(self):
        with patch.object(neo4j_backend, "get_distinct_metadata", return_value=["SCOPE_A"]), \
             patch.object(qdrant_backend, "get_distinct_metadata", return_value=["SCOPE_B"]):
            result = await nexus_tools.get_all_tenant_scopes()
        assert "SCOPE_A" in result
        assert "SCOPE_B" in result

    async def test_project_filter_path(self):
        with patch.object(neo4j_backend, "get_scopes_for_project", return_value=["GRAPH_SCOPE"]), \
             patch.object(qdrant_backend, "scroll_field", return_value={"QDRANT_SCOPE"}):
            result = await nexus_tools.get_all_tenant_scopes(project_id="PROJ")
        assert "GRAPH_SCOPE" in result
        assert "QDRANT_SCOPE" in result

    async def test_project_filter_neo4j_down_returns_qdrant_only(self):
        with patch.object(neo4j_backend, "get_scopes_for_project", return_value=[]), \
             patch.object(qdrant_backend, "scroll_field", return_value={"QDRANT_SCOPE"}):
            result = await nexus_tools.get_all_tenant_scopes(project_id="PROJ")
        assert "QDRANT_SCOPE" in result

    async def test_project_filter_qdrant_down_returns_neo4j_only(self):
        with patch.object(neo4j_backend, "get_scopes_for_project", return_value=["GRAPH_SCOPE"]), \
             patch.object(qdrant_backend, "scroll_field", side_effect=Exception("down")):
            result = await nexus_tools.get_all_tenant_scopes(project_id="PROJ")
        assert "GRAPH_SCOPE" in result


# ---------------------------------------------------------------------------
# nexus.indexes — setup_settings singleton
# ---------------------------------------------------------------------------

class TestSetupSettings:
    def test_is_idempotent(self):
        original = nexus_indexes._settings_initialized
        try:
            nexus_indexes._settings_initialized = True
            with patch("nexus.indexes.Ollama") as mock_llm, \
                 patch("nexus.indexes.OllamaEmbedding") as mock_embed:
                nexus_indexes.setup_settings()
            mock_llm.assert_not_called()
            mock_embed.assert_not_called()
        finally:
            nexus_indexes._settings_initialized = original

    def test_lock_exists(self):
        assert isinstance(nexus_indexes._settings_lock, type(threading.Lock()))


# ---------------------------------------------------------------------------
# nexus.dedup — content_hash
# ---------------------------------------------------------------------------

class TestContentHash:
    def test_same_text_same_project_scope_is_identical(self):
        h1 = nexus_dedup.content_hash("Hello", "PROJ", "SCOPE")
        h2 = nexus_dedup.content_hash("Hello", "PROJ", "SCOPE")
        assert h1 == h2

    def test_different_project_gives_different_hash(self):
        h1 = nexus_dedup.content_hash("Hello", "PROJ_A", "SCOPE")
        h2 = nexus_dedup.content_hash("Hello", "PROJ_B", "SCOPE")
        assert h1 != h2

    def test_different_scope_gives_different_hash(self):
        h1 = nexus_dedup.content_hash("Hello", "PROJ", "SCOPE_A")
        h2 = nexus_dedup.content_hash("Hello", "PROJ", "SCOPE_B")
        assert h1 != h2

    def test_different_text_gives_different_hash(self):
        h1 = nexus_dedup.content_hash("Hello", "PROJ", "SCOPE")
        h2 = nexus_dedup.content_hash("World", "PROJ", "SCOPE")
        assert h1 != h2

    def test_returns_64_char_hex_string(self):
        h = nexus_dedup.content_hash("x", "P", "S")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_null_byte_separator_prevents_collisions(self):
        """'AB' + 'C' must hash differently from 'A' + 'BC'."""
        h1 = nexus_dedup.content_hash("text", "A", "BC")
        h2 = nexus_dedup.content_hash("text", "AB", "C")
        assert h1 != h2


# ---------------------------------------------------------------------------
# nexus.backends.qdrant — is_duplicate
# ---------------------------------------------------------------------------

class TestIsDuplicateQdrant:
    def test_returns_true_when_record_found(self):
        mock_client = MagicMock()
        mock_client.scroll.return_value = (["fake_record"], None)
        with patch.object(qdrant_backend, "_get_client", return_value=mock_client):
            assert qdrant_backend.is_duplicate("abc", "PROJ", "SCOPE") is True

    def test_returns_false_when_no_record(self):
        mock_client = MagicMock()
        mock_client.scroll.return_value = ([], None)
        with patch.object(qdrant_backend, "_get_client", return_value=mock_client):
            assert qdrant_backend.is_duplicate("abc", "PROJ", "SCOPE") is False

    def test_fail_open_on_exception(self):
        with patch.object(qdrant_backend, "_get_client", side_effect=Exception("timeout")):
            assert qdrant_backend.is_duplicate("abc", "PROJ", "SCOPE") is False

    def test_scroll_uses_all_three_filters(self):
        mock_client = MagicMock()
        mock_client.scroll.return_value = ([], None)
        with patch.object(qdrant_backend, "_get_client", return_value=mock_client):
            qdrant_backend.is_duplicate("HASH123", "MY_PROJ", "MY_SCOPE")
        _, kwargs = mock_client.scroll.call_args
        keys = {c.key for c in kwargs["scroll_filter"].must}
        assert keys == {"project_id", "tenant_scope", "content_hash"}


# ---------------------------------------------------------------------------
# nexus.backends.neo4j — is_duplicate
# ---------------------------------------------------------------------------

class TestIsDuplicateNeo4j:
    def test_returns_true_when_exists(self):
        driver = _make_neo4j_driver_with_single({"exists": True})
        with patch.object(neo4j_backend, "neo4j_driver", return_value=driver):
            assert neo4j_backend.is_duplicate("abc", "PROJ", "SCOPE") is True

    def test_returns_false_when_not_exists(self):
        driver = _make_neo4j_driver_with_single({"exists": False})
        with patch.object(neo4j_backend, "neo4j_driver", return_value=driver):
            assert neo4j_backend.is_duplicate("abc", "PROJ", "SCOPE") is False

    def test_returns_false_when_no_record(self):
        driver = _make_neo4j_driver_with_single(None)
        with patch.object(neo4j_backend, "neo4j_driver", return_value=driver):
            assert neo4j_backend.is_duplicate("abc", "PROJ", "SCOPE") is False

    def test_fail_open_on_exception(self):
        with patch.object(neo4j_backend, "neo4j_driver", side_effect=Exception("bolt down")):
            assert neo4j_backend.is_duplicate("abc", "PROJ", "SCOPE") is False


# ---------------------------------------------------------------------------
# nexus.tools — ingest dedup gate
# ---------------------------------------------------------------------------

class TestIngestVectorDedup:
    async def test_skips_on_duplicate(self):
        with patch("nexus.tools.content_hash", return_value="HASH"), \
             patch.object(qdrant_backend, "is_duplicate", return_value=True) as mock_check, \
             patch("nexus.tools.get_vector_index") as mock_index:
            result = await nexus_tools.ingest_vector_document("text", "PROJ", "SCOPE")
        assert "Skipped" in result
        mock_index.assert_not_called()
        mock_check.assert_called_once_with("HASH", "PROJ", "SCOPE")

    async def test_ingests_on_first_time(self):
        mock_index = MagicMock()
        with patch("nexus.tools.content_hash", return_value="HASH"), \
             patch.object(qdrant_backend, "is_duplicate", return_value=False), \
             patch("nexus.tools.get_vector_index", return_value=mock_index):
            result = await nexus_tools.ingest_vector_document("text", "PROJ", "SCOPE")
        assert "Successfully" in result
        mock_index.insert.assert_called_once()

    async def test_doc_id_set_to_hash(self):
        mock_index = MagicMock()
        with patch("nexus.tools.content_hash", return_value="DEADBEEF"), \
             patch.object(qdrant_backend, "is_duplicate", return_value=False), \
             patch("nexus.tools.get_vector_index", return_value=mock_index):
            await nexus_tools.ingest_vector_document("text", "PROJ", "SCOPE")
        doc = mock_index.insert.call_args[0][0]
        assert doc.doc_id == "DEADBEEF"

    async def test_content_hash_in_metadata(self):
        mock_index = MagicMock()
        with patch("nexus.tools.content_hash", return_value="CAFEF00D"), \
             patch.object(qdrant_backend, "is_duplicate", return_value=False), \
             patch("nexus.tools.get_vector_index", return_value=mock_index):
            await nexus_tools.ingest_vector_document("text", "PROJ", "SCOPE")
        doc = mock_index.insert.call_args[0][0]
        assert doc.metadata["content_hash"] == "CAFEF00D"


class TestIngestGraphDedup:
    async def test_skips_on_duplicate(self):
        with patch("nexus.tools.content_hash", return_value="HASH"), \
             patch.object(neo4j_backend, "is_duplicate", return_value=True) as mock_check, \
             patch("nexus.tools.get_graph_index") as mock_index:
            result = await nexus_tools.ingest_graph_document("text", "PROJ", "SCOPE")
        assert "Skipped" in result
        mock_index.assert_not_called()
        mock_check.assert_called_once_with("HASH", "PROJ", "SCOPE")

    async def test_ingests_on_first_time(self):
        mock_index = MagicMock()
        with patch("nexus.tools.content_hash", return_value="HASH"), \
             patch.object(neo4j_backend, "is_duplicate", return_value=False), \
             patch("nexus.tools.get_graph_index", return_value=mock_index):
            result = await nexus_tools.ingest_graph_document("text", "PROJ", "SCOPE")
        assert "Successfully" in result
        mock_index.insert.assert_called_once()

    async def test_doc_id_set_to_hash(self):
        mock_index = MagicMock()
        with patch("nexus.tools.content_hash", return_value="GRAPHHASH"), \
             patch.object(neo4j_backend, "is_duplicate", return_value=False), \
             patch("nexus.tools.get_graph_index", return_value=mock_index):
            await nexus_tools.ingest_graph_document("text", "PROJ", "SCOPE")
        doc = mock_index.insert.call_args[0][0]
        assert doc.doc_id == "GRAPHHASH"
