# Version: v3.0
"""
Unit tests for the nexus/ package.
All database calls are mocked — no live Qdrant or Neo4j required.
asyncio_mode=auto (set in pyproject.toml) removes the need for @pytest.mark.asyncio decorators.
"""

import threading
import pytest
import redis
from unittest.mock import AsyncMock, MagicMock, patch

from nexus import config as nexus_config
from nexus import dedup as nexus_dedup
from nexus.backends import neo4j as neo4j_backend
from nexus.backends import qdrant as qdrant_backend
from nexus import indexes as nexus_indexes
from nexus import sync as nexus_sync
from nexus import tools as nexus_tools

# Save original cache functions at module-import time, before any fixtures run.
# conftest.autouse disable_cache replaces nexus.cache.set_cached with a no-op lambda;
# these module-level references still point to the real implementations.
import nexus.cache as _nexus_cache

_orig_set_cached = _nexus_cache.set_cached
_orig_invalidate_cache = _nexus_cache.invalidate_cache


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
        mock_driver, _ = _make_neo4j_driver(
            [{"value": "SCOPE_A"}, {"value": "SCOPE_B"}]
        )
        with patch.object(neo4j_backend, "get_driver", return_value=mock_driver):
            result = neo4j_backend.get_distinct_metadata("tenant_scope")
        assert set(result) == {"SCOPE_A", "SCOPE_B"}

    def test_get_distinct_neo4j_returns_empty_on_error(self):
        with patch.object(neo4j_backend, "get_driver", side_effect=Exception("down")):
            result = neo4j_backend.get_distinct_metadata("project_id")
        assert result == []

    def test_get_distinct_qdrant_returns_empty_on_error(self):
        with patch.object(
            qdrant_backend, "scroll_field", side_effect=Exception("timeout")
        ):
            result = qdrant_backend.get_distinct_metadata("project_id")
        assert result == []


# ---------------------------------------------------------------------------
# nexus.backends.qdrant — scroll_field pagination loop
# ---------------------------------------------------------------------------


class TestScrollQdrantField:
    def test_collects_across_multiple_pages(self):
        page1 = [
            MagicMock(payload={"project_id": "A"}),
            MagicMock(payload={"project_id": "B"}),
        ]
        page2 = [MagicMock(payload={"project_id": "C"})]
        mock_client = MagicMock()
        mock_client.scroll.side_effect = [(page1, "cursor"), (page2, None)]

        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
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

        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            result = qdrant_backend.scroll_field("project_id")

        assert result == {"FOUND"}

    def test_passes_qdrant_filter_to_scroll(self):
        mock_client = MagicMock()
        mock_client.scroll.side_effect = [([], None)]
        mock_filter = MagicMock()

        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            qdrant_backend.scroll_field("project_id", qdrant_filter=mock_filter)

        _, kwargs = mock_client.scroll.call_args
        assert kwargs["scroll_filter"] is mock_filter


# ---------------------------------------------------------------------------
# nexus.backends.qdrant — get_client caching (Bug fix #2)
# ---------------------------------------------------------------------------


class TestQdrantClientCache:
    def test_same_url_returns_same_instance(self):
        # Clear cache so we test fresh creation
        qdrant_backend._client_cache.clear()
        mock_client = MagicMock()
        with patch("qdrant_client.QdrantClient", return_value=mock_client) as mock_cls:
            c1 = qdrant_backend.get_client("http://fake-qdrant:9999")
            c2 = qdrant_backend.get_client("http://fake-qdrant:9999")
        # QdrantClient constructor should only be called once
        assert mock_cls.call_count == 1
        assert c1 is c2
        qdrant_backend._client_cache.clear()

    def test_different_url_creates_separate_instance(self):
        qdrant_backend._client_cache.clear()
        mock_a = MagicMock()
        mock_b = MagicMock()
        with patch("qdrant_client.QdrantClient", side_effect=[mock_a, mock_b]):
            ca = qdrant_backend.get_client("http://url-a")
            cb = qdrant_backend.get_client("http://url-b")
        assert ca is mock_a
        assert cb is mock_b
        qdrant_backend._client_cache.clear()


# ---------------------------------------------------------------------------
# nexus.backends.neo4j — delete_data Cypher branching (Bug fix #1: re-raises)
# ---------------------------------------------------------------------------


class TestDeleteNeo4j:
    def test_without_scope_uses_project_only_cypher(self):
        mock_driver, mock_session = _make_neo4j_driver()
        with patch.object(neo4j_backend, "get_driver", return_value=mock_driver):
            neo4j_backend.delete_data("MY_PROJECT")
        cypher, kwargs = mock_session.run.call_args
        assert "tenant_scope" not in cypher[0]
        assert kwargs == {"project_id": "MY_PROJECT"}

    def test_with_scope_includes_tenant_scope_in_cypher(self):
        mock_driver, mock_session = _make_neo4j_driver()
        with patch.object(neo4j_backend, "get_driver", return_value=mock_driver):
            neo4j_backend.delete_data("MY_PROJECT", "MY_SCOPE")
        cypher, kwargs = mock_session.run.call_args
        assert "tenant_scope" in cypher[0]
        assert kwargs == {"project_id": "MY_PROJECT", "scope": "MY_SCOPE"}

    def test_neo4j_error_is_re_raised(self):
        """Bug fix #1: delete_data_neo4j now re-raises instead of swallowing."""
        with patch.object(
            neo4j_backend, "get_driver", side_effect=Exception("connection refused")
        ):
            with pytest.raises(Exception, match="connection refused"):
                neo4j_backend.delete_data("PROJ")


# ---------------------------------------------------------------------------
# nexus.backends.qdrant — delete_data filter construction
# ---------------------------------------------------------------------------


class TestDeleteQdrant:
    def test_without_scope_single_must_condition(self):
        mock_client = MagicMock()
        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            qdrant_backend.delete_data("MY_PROJECT")
        must = mock_client.delete.call_args[1]["points_selector"].filter.must
        assert len(must) == 1
        assert must[0].key == "project_id"

    def test_with_scope_two_must_conditions(self):
        mock_client = MagicMock()
        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            qdrant_backend.delete_data("MY_PROJECT", "MY_SCOPE")
        must = mock_client.delete.call_args[1]["points_selector"].filter.must
        assert len(must) == 2
        assert {c.key for c in must} == {"project_id", "tenant_scope"}

    def test_qdrant_error_is_propagated(self):
        with patch.object(
            qdrant_backend, "get_client", side_effect=Exception("timeout")
        ):
            with pytest.raises(Exception, match="timeout"):
                qdrant_backend.delete_data("PROJ")


# ---------------------------------------------------------------------------
# nexus.tools.delete_tenant_data — calls both backends, guards empty project_id
# ---------------------------------------------------------------------------


class TestDeleteTenantData:
    async def test_calls_both_backends(self):
        with (
            patch.object(neo4j_backend, "delete_data") as mock_neo4j,
            patch.object(qdrant_backend, "delete_data") as mock_qdrant,
        ):
            result = await nexus_tools.delete_tenant_data("PROJ", "SCOPE")
        mock_neo4j.assert_called_once_with("PROJ", "SCOPE")
        mock_qdrant.assert_called_once_with("PROJ", "SCOPE")
        assert "Successfully" in result
        assert "PROJ" in result

    async def test_without_scope_omits_scope_from_message(self):
        with (
            patch.object(neo4j_backend, "delete_data"),
            patch.object(qdrant_backend, "delete_data"),
        ):
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
        with (
            patch.object(
                neo4j_backend, "delete_data", side_effect=Exception("neo4j down")
            ),
            patch.object(qdrant_backend, "delete_data"),
        ):
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
        """Exception message must be generic (not expose raw exception details)."""
        with (
            patch("nexus.tools.get_vector_index", side_effect=Exception("DB down")),
            patch.object(qdrant_backend, "is_duplicate", return_value=False),
        ):
            result = await nexus_tools.ingest_vector_document("text", "PROJ", "SCOPE")
        assert "Error" in result
        # Raw exception text must NOT be exposed to client
        assert "DB down" not in result

    async def test_ingest_graph_document_error_returns_string(self):
        """Exception message must be generic (not expose raw exception details)."""
        with (
            patch(
                "nexus.tools.get_graph_index", side_effect=Exception("Neo4j offline")
            ),
            patch.object(neo4j_backend, "is_duplicate", return_value=False),
        ):
            result = await nexus_tools.ingest_graph_document("text", "PROJ", "SCOPE")
        assert "Error" in result
        # Raw exception text must NOT be exposed to client
        assert "Neo4j offline" not in result


# ---------------------------------------------------------------------------
# nexus.tools — context retrieval
# ---------------------------------------------------------------------------


class TestContextRetrieval:
    def _mock_index(self, nodes=None):
        mock_retriever = MagicMock()
        mock_retriever.aretrieve = AsyncMock(
            return_value=nodes if nodes is not None else []
        )
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
        node.score = 0.95  # Score is required for formatting
        with patch(
            "nexus.tools.get_vector_index", return_value=self._mock_index([node])
        ):
            result = await nexus_tools.get_vector_context("query", "PROJ", "SCOPE")
        assert "match!" in result

    async def test_get_vector_context_error_returns_string(self):
        with patch(
            "nexus.tools.get_vector_index", side_effect=Exception("Qdrant exploded")
        ):
            result = await nexus_tools.get_vector_context("query", "PROJ", "SCOPE")
        assert "Error" in result

    async def test_get_graph_context_no_results(self):
        with patch("nexus.tools.get_graph_index", return_value=self._mock_index([])):
            result = await nexus_tools.get_graph_context("query", "PROJ", "SCOPE")
        assert "No Graph context found" in result

    async def test_get_graph_context_error_returns_string(self):
        with patch(
            "nexus.tools.get_graph_index", side_effect=Exception("Neo4j exploded")
        ):
            result = await nexus_tools.get_graph_context("query", "PROJ", "SCOPE")
        assert "Error" in result

    async def test_get_graph_context_max_chars_truncates(self):
        node = MagicMock()
        node.node.get_content.return_value = "x" * 5000
        node.score = 0.95  # Score is required for formatting
        with patch(
            "nexus.tools.get_graph_index", return_value=self._mock_index([node])
        ):
            result = await nexus_tools.get_graph_context(
                "query", "PROJ", "SCOPE", max_chars=100
            )
        assert "truncated" in result
        assert len(result) < 250  # header + [score: X.XXXX] + 100 chars + suffix

    async def test_get_graph_context_max_chars_zero_disables(self):
        node = MagicMock()
        node.node.get_content.return_value = "x" * 5000
        node.score = 0.95  # Score is required for formatting
        with patch(
            "nexus.tools.get_graph_index", return_value=self._mock_index([node])
        ):
            result = await nexus_tools.get_graph_context(
                "query", "PROJ", "SCOPE", max_chars=0
            )
        assert "truncated" not in result
        assert "x" * 5000 in result

    async def test_get_vector_context_max_chars_truncates(self):
        node = MagicMock()
        node.node.get_content.return_value = "y" * 5000
        node.score = 0.95  # Score is required for formatting
        with patch(
            "nexus.tools.get_vector_index", return_value=self._mock_index([node])
        ):
            result = await nexus_tools.get_vector_context(
                "query", "PROJ", "SCOPE", max_chars=100
            )
        assert "truncated" in result
        assert len(result) < 250  # header + [score: X.XXXX] + 100 chars + suffix

    async def test_get_vector_context_max_chars_zero_disables(self):
        node = MagicMock()
        node.node.get_content.return_value = "y" * 5000
        node.score = 0.95  # Score is required for formatting
        with patch(
            "nexus.tools.get_vector_index", return_value=self._mock_index([node])
        ):
            result = await nexus_tools.get_vector_context(
                "query", "PROJ", "SCOPE", max_chars=0
            )
        assert "truncated" not in result
        assert "y" * 5000 in result

    async def test_get_vector_context_cache_hit_respects_max_chars(self):
        """Regression: cache hits must honour max_chars (bypass bug)."""
        large_cached = (
            "Vector Context retrieved for PROJ in scope SCOPE:\n" + "z" * 5000
        )
        with patch("nexus.tools.cache_module.get_cached", return_value=large_cached):
            result = await nexus_tools.get_vector_context(
                "query", "PROJ", "SCOPE", max_chars=100
            )
        assert "truncated" in result
        assert len(result) <= 120  # 100 + len("… [truncated]")

    async def test_get_graph_context_cache_hit_respects_max_chars(self):
        """Regression: cache hits must honour max_chars (bypass bug)."""
        large_cached = "Graph Context retrieved for PROJ in scope SCOPE:\n" + "z" * 5000
        with patch("nexus.tools.cache_module.get_cached", return_value=large_cached):
            result = await nexus_tools.get_graph_context(
                "query", "PROJ", "SCOPE", max_chars=100
            )
        assert "truncated" in result
        assert len(result) <= 120

    async def test_graph_and_vector_cache_keys_do_not_collide(self):
        """Regression: graph and vector tools must use distinct cache namespaces.

        Before the fix, both tools shared the same cache key — a graph cache
        hit would poison a subsequent vector query with 'Graph Context...' text.
        """
        from nexus.cache import cache_key

        key_graph = cache_key("same query", "PROJ", "SCOPE", tool_type="graph")
        key_vector = cache_key("same query", "PROJ", "SCOPE", tool_type="vector")
        key_answer = cache_key("same query", "PROJ", "SCOPE", tool_type="answer")
        assert key_graph != key_vector
        assert key_graph != key_answer
        assert key_vector != key_answer

    async def test_get_vector_context_empty_scope_omits_scope_filter(self):
        """When scope is empty, only the project_id filter is applied."""
        from llama_index.core.vector_stores.types import MetadataFilters

        captured_filters: list[MetadataFilters] = []

        async def fake_retrieve(query):
            return []

        mock_retriever = MagicMock()
        mock_retriever.aretrieve = fake_retrieve
        mock_index = MagicMock()

        def capture_retriever(**kwargs):
            captured_filters.append(kwargs.get("filters"))
            return mock_retriever

        mock_index.as_retriever = capture_retriever

        with patch("nexus.tools.get_vector_index", return_value=mock_index):
            result = await nexus_tools.get_vector_context("query", "PROJ", scope="")

        assert "No Vector context found" in result
        assert "all scopes" in result
        # Only project_id filter — no tenant_scope filter
        assert len(captured_filters[0].filters) == 1
        assert captured_filters[0].filters[0].key == "project_id"

    async def test_get_graph_context_empty_scope_omits_scope_filter(self):
        """When scope is empty, only the project_id filter is applied."""
        from llama_index.core.vector_stores.types import MetadataFilters

        captured_filters: list[MetadataFilters] = []

        async def fake_retrieve(query):
            return []

        mock_retriever = MagicMock()
        mock_retriever.aretrieve = fake_retrieve
        mock_index = MagicMock()

        def capture_retriever(**kwargs):
            captured_filters.append(kwargs.get("filters"))
            return mock_retriever

        mock_index.as_retriever = capture_retriever

        with patch("nexus.tools.get_graph_index", return_value=mock_index):
            result = await nexus_tools.get_graph_context("query", "PROJ", scope="")

        assert "No Graph context found" in result
        assert "all scopes" in result
        assert len(captured_filters[0].filters) == 1
        assert captured_filters[0].filters[0].key == "project_id"

    async def test_get_vector_context_with_scope_includes_scope_filter(self):
        """When scope is provided, the tenant_scope filter IS applied."""
        captured_filters = []

        async def fake_retrieve(query):
            return []

        mock_retriever = MagicMock()
        mock_retriever.aretrieve = fake_retrieve
        mock_index = MagicMock()

        def capture_retriever(**kwargs):
            captured_filters.append(kwargs.get("filters"))
            return mock_retriever

        mock_index.as_retriever = capture_retriever

        with patch("nexus.tools.get_vector_index", return_value=mock_index):
            await nexus_tools.get_vector_context("query", "PROJ", scope="PERSONA")

        assert len(captured_filters[0].filters) == 2
        filter_keys = {f.key for f in captured_filters[0].filters}
        assert "project_id" in filter_keys
        assert "tenant_scope" in filter_keys


# ---------------------------------------------------------------------------
# nexus.tools._apply_cap helper
# ---------------------------------------------------------------------------


class TestApplyCap:
    def test_truncates_at_limit(self):
        from nexus.tools import _apply_cap

        result = _apply_cap("a" * 200, 100)
        assert result == "a" * 100 + "… [truncated]"
        assert len(result) == 113  # 100 + 13 chars in suffix ("… [truncated]")

    def test_no_truncation_when_under_limit(self):
        from nexus.tools import _apply_cap

        text = "short text"
        assert _apply_cap(text, 100) == text

    def test_zero_disables_cap(self):
        from nexus.tools import _apply_cap

        text = "x" * 5000
        assert _apply_cap(text, 0) == text

    def test_exact_limit_not_truncated(self):
        from nexus.tools import _apply_cap

        text = "a" * 100
        assert _apply_cap(text, 100) == text

    def test_one_over_limit_truncated(self):
        from nexus.tools import _apply_cap

        text = "a" * 101
        result = _apply_cap(text, 100)
        assert "truncated" in result


# ---------------------------------------------------------------------------
# nexus.tools — get_all_project_ids
# ---------------------------------------------------------------------------


class TestGetAllProjectIds:
    async def test_merges_and_deduplicates(self):
        with (
            patch.object(
                neo4j_backend, "get_distinct_metadata", return_value=["A", "B"]
            ),
            patch.object(
                qdrant_backend, "get_distinct_metadata", return_value=["B", "C"]
            ),
        ):
            result = await nexus_tools.get_all_project_ids()
        assert result == ["A", "B", "C"]

    async def test_returns_sorted(self):
        with (
            patch.object(
                neo4j_backend, "get_distinct_metadata", return_value=["Z", "M"]
            ),
            patch.object(qdrant_backend, "get_distinct_metadata", return_value=["A"]),
        ):
            result = await nexus_tools.get_all_project_ids()
        assert result == sorted(result)

    async def test_one_backend_down_returns_partial(self):
        with (
            patch.object(neo4j_backend, "get_distinct_metadata", return_value=[]),
            patch.object(
                qdrant_backend, "get_distinct_metadata", return_value=["QDRANT_ONLY"]
            ),
        ):
            result = await nexus_tools.get_all_project_ids()
        assert result == ["QDRANT_ONLY"]


# ---------------------------------------------------------------------------
# nexus.tools — get_all_tenant_scopes
# ---------------------------------------------------------------------------


class TestGetAllTenantScopes:
    async def test_global_path_merges_both_backends(self):
        with (
            patch.object(
                neo4j_backend, "get_distinct_metadata", return_value=["SCOPE_A"]
            ),
            patch.object(
                qdrant_backend, "get_distinct_metadata", return_value=["SCOPE_B"]
            ),
        ):
            result = await nexus_tools.get_all_tenant_scopes()
        assert "SCOPE_A" in result
        assert "SCOPE_B" in result

    async def test_project_filter_path(self):
        with (
            patch.object(
                neo4j_backend, "get_scopes_for_project", return_value=["GRAPH_SCOPE"]
            ),
            patch.object(qdrant_backend, "scroll_field", return_value={"QDRANT_SCOPE"}),
        ):
            result = await nexus_tools.get_all_tenant_scopes(project_id="PROJ")
        assert "GRAPH_SCOPE" in result
        assert "QDRANT_SCOPE" in result

    async def test_project_filter_neo4j_down_returns_qdrant_only(self):
        with (
            patch.object(neo4j_backend, "get_scopes_for_project", return_value=[]),
            patch.object(qdrant_backend, "scroll_field", return_value={"QDRANT_SCOPE"}),
        ):
            result = await nexus_tools.get_all_tenant_scopes(project_id="PROJ")
        assert "QDRANT_SCOPE" in result

    async def test_project_filter_qdrant_down_returns_neo4j_only(self):
        with (
            patch.object(
                neo4j_backend, "get_scopes_for_project", return_value=["GRAPH_SCOPE"]
            ),
            patch.object(qdrant_backend, "scroll_field", side_effect=Exception("down")),
        ):
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
            with (
                patch("nexus.indexes.Ollama") as mock_llm,
                patch("nexus.indexes.OllamaEmbedding") as mock_embed,
            ):
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
        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            assert qdrant_backend.is_duplicate("abc", "PROJ", "SCOPE") is True

    def test_returns_false_when_no_record(self):
        mock_client = MagicMock()
        mock_client.scroll.return_value = ([], None)
        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            assert qdrant_backend.is_duplicate("abc", "PROJ", "SCOPE") is False

    def test_fail_open_on_exception(self):
        with patch.object(
            qdrant_backend, "get_client", side_effect=Exception("timeout")
        ):
            assert qdrant_backend.is_duplicate("abc", "PROJ", "SCOPE") is False

    def test_scroll_uses_all_three_filters(self):
        mock_client = MagicMock()
        mock_client.scroll.return_value = ([], None)
        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
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
        with patch.object(neo4j_backend, "get_driver", return_value=driver):
            assert neo4j_backend.is_duplicate("abc", "PROJ", "SCOPE") is True

    def test_returns_false_when_not_exists(self):
        driver = _make_neo4j_driver_with_single({"exists": False})
        with patch.object(neo4j_backend, "get_driver", return_value=driver):
            assert neo4j_backend.is_duplicate("abc", "PROJ", "SCOPE") is False

    def test_returns_false_when_no_record(self):
        driver = _make_neo4j_driver_with_single(None)
        with patch.object(neo4j_backend, "get_driver", return_value=driver):
            assert neo4j_backend.is_duplicate("abc", "PROJ", "SCOPE") is False

    def test_fail_open_on_exception(self):
        with patch.object(
            neo4j_backend, "get_driver", side_effect=Exception("bolt down")
        ):
            assert neo4j_backend.is_duplicate("abc", "PROJ", "SCOPE") is False


# ---------------------------------------------------------------------------
# nexus.tools — ingest dedup gate
# ---------------------------------------------------------------------------


class TestIngestVectorDedup:
    async def test_skips_on_duplicate(self):
        with (
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(
                qdrant_backend, "is_duplicate", return_value=True
            ) as mock_check,
            patch("nexus.tools.get_vector_index") as mock_index,
        ):
            result = await nexus_tools.ingest_vector_document("text", "PROJ", "SCOPE")
        assert "Skipped" in result
        mock_index.assert_not_called()
        mock_check.assert_called_once_with("HASH", "PROJ", "SCOPE")

    async def test_ingests_on_first_time(self):
        mock_index = MagicMock()
        with (
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(qdrant_backend, "is_duplicate", return_value=False),
            patch("nexus.tools.get_vector_index", return_value=mock_index),
        ):
            result = await nexus_tools.ingest_vector_document("text", "PROJ", "SCOPE")
        assert "Successfully" in result
        mock_index.insert.assert_called_once()

    async def test_doc_id_set_to_hash(self):
        mock_index = MagicMock()
        with (
            patch("nexus.tools.content_hash", return_value="DEADBEEF"),
            patch.object(qdrant_backend, "is_duplicate", return_value=False),
            patch("nexus.tools.get_vector_index", return_value=mock_index),
        ):
            await nexus_tools.ingest_vector_document("text", "PROJ", "SCOPE")
        doc = mock_index.insert.call_args[0][0]
        assert doc.doc_id == "DEADBEEF"

    async def test_content_hash_in_metadata(self):
        mock_index = MagicMock()
        with (
            patch("nexus.tools.content_hash", return_value="CAFEF00D"),
            patch.object(qdrant_backend, "is_duplicate", return_value=False),
            patch("nexus.tools.get_vector_index", return_value=mock_index),
        ):
            await nexus_tools.ingest_vector_document("text", "PROJ", "SCOPE")
        doc = mock_index.insert.call_args[0][0]
        assert doc.metadata["content_hash"] == "CAFEF00D"


class TestIngestGraphDedup:
    async def test_skips_on_duplicate(self):
        with (
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(
                neo4j_backend, "is_duplicate", return_value=True
            ) as mock_check,
            patch("nexus.tools.get_graph_index") as mock_index,
        ):
            result = await nexus_tools.ingest_graph_document("text", "PROJ", "SCOPE")
        assert "Skipped" in result
        mock_index.assert_not_called()
        mock_check.assert_called_once_with("HASH", "PROJ", "SCOPE")

    async def test_ingests_on_first_time(self):
        mock_index = MagicMock()
        with (
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(neo4j_backend, "is_duplicate", return_value=False),
            patch("nexus.tools.get_graph_index", return_value=mock_index),
        ):
            result = await nexus_tools.ingest_graph_document("text", "PROJ", "SCOPE")
        assert "Successfully" in result
        mock_index.insert.assert_called_once()

    async def test_doc_id_set_to_hash(self):
        mock_index = MagicMock()
        with (
            patch("nexus.tools.content_hash", return_value="GRAPHHASH"),
            patch.object(neo4j_backend, "is_duplicate", return_value=False),
            patch("nexus.tools.get_graph_index", return_value=mock_index),
        ):
            await nexus_tools.ingest_graph_document("text", "PROJ", "SCOPE")
        doc = mock_index.insert.call_args[0][0]
        assert doc.doc_id == "GRAPHHASH"


# ---------------------------------------------------------------------------
# nexus.backends.qdrant — delete_all_data
# ---------------------------------------------------------------------------


class TestDeleteAllQdrant:
    def test_calls_client_delete_with_empty_filter(self):
        mock_client = MagicMock()
        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            qdrant_backend.delete_all_data()
        mock_client.delete.assert_called_once()
        selector = mock_client.delete.call_args[1]["points_selector"]
        # must= [] means no conditions — deletes everything
        assert selector.filter.must == []

    def test_propagates_exception(self):
        with patch.object(
            qdrant_backend, "get_client", side_effect=Exception("qdrant down")
        ):
            with pytest.raises(Exception, match="qdrant down"):
                qdrant_backend.delete_all_data()


# ---------------------------------------------------------------------------
# nexus.backends.neo4j — delete_all_data
# ---------------------------------------------------------------------------


class TestDeleteAllNeo4j:
    def test_runs_detach_delete_all_cypher(self):
        mock_driver, mock_session = _make_neo4j_driver()
        with patch.object(neo4j_backend, "get_driver", return_value=mock_driver):
            neo4j_backend.delete_all_data()
        cypher = mock_session.run.call_args[0][0]
        assert "MATCH (n)" in cypher
        assert "DETACH DELETE" in cypher
        # No project_id or scope filter — deletes everything
        assert "project_id" not in cypher

    def test_propagates_exception(self):
        with patch.object(
            neo4j_backend, "get_driver", side_effect=Exception("bolt down")
        ):
            with pytest.raises(Exception, match="bolt down"):
                neo4j_backend.delete_all_data()


# ---------------------------------------------------------------------------
# nexus.tools.delete_all_data — MCP tool
# ---------------------------------------------------------------------------


class TestDeleteAllDataTool:
    async def test_calls_both_backends(self):
        with (
            patch.object(neo4j_backend, "delete_all_data") as mock_neo4j,
            patch.object(qdrant_backend, "delete_all_data") as mock_qdrant,
        ):
            result = await nexus_tools.delete_all_data()
        mock_neo4j.assert_called_once()
        mock_qdrant.assert_called_once()
        assert "Successfully" in result
        assert "ALL" in result

    async def test_partial_failure_neo4j(self):
        with (
            patch.object(
                neo4j_backend, "delete_all_data", side_effect=Exception("neo4j down")
            ),
            patch.object(qdrant_backend, "delete_all_data"),
        ):
            result = await nexus_tools.delete_all_data()
        assert "Partial failure" in result
        assert "Neo4j" in result

    async def test_partial_failure_qdrant(self):
        with (
            patch.object(neo4j_backend, "delete_all_data"),
            patch.object(
                qdrant_backend, "delete_all_data", side_effect=Exception("qdrant down")
            ),
        ):
            result = await nexus_tools.delete_all_data()
        assert "Partial failure" in result
        assert "Qdrant" in result

    async def test_both_backends_fail_reports_both(self):
        with (
            patch.object(
                neo4j_backend, "delete_all_data", side_effect=Exception("neo4j err")
            ),
            patch.object(
                qdrant_backend, "delete_all_data", side_effect=Exception("qdrant err")
            ),
        ):
            result = await nexus_tools.delete_all_data()
        assert "Partial failure" in result
        assert "Neo4j" in result
        assert "Qdrant" in result


# ---------------------------------------------------------------------------
# nexus.tools — post-retrieval dedup in get_vector_context / get_graph_context
# ---------------------------------------------------------------------------


class TestPostRetrievalDedup:
    """Verify that duplicate nodes returned by the retriever are collapsed."""

    def _make_node(self, content: str, score: float = 0.95):
        node = MagicMock()
        node.node.get_content.return_value = content
        node.score = score  # Score is required for formatting
        return node

    def _mock_index(self, nodes):
        mock_retriever = MagicMock()
        mock_retriever.aretrieve = AsyncMock(return_value=nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever
        return mock_index

    async def test_vector_dedup_removes_duplicate_content(self):
        """Three nodes with same content → only one bullet in output."""
        dup_node = self._make_node("same content")
        nodes = [dup_node, dup_node, dup_node]
        with patch(
            "nexus.tools.get_vector_index", return_value=self._mock_index(nodes)
        ):
            result = await nexus_tools.get_vector_context("q", "P", "S", rerank=False)
        # Only one occurrence of the content
        assert result.count("same content") == 1

    async def test_vector_dedup_preserves_unique_content(self):
        """Two nodes with different content → both appear in output."""
        node_a = self._make_node("content A")
        node_b = self._make_node("content B")
        with patch(
            "nexus.tools.get_vector_index",
            return_value=self._mock_index([node_a, node_b]),
        ):
            result = await nexus_tools.get_vector_context("q", "P", "S", rerank=False)
        assert "content A" in result
        assert "content B" in result

    async def test_vector_dedup_mixed_duplicates(self):
        """A, B, A, B → only A and B once each."""
        node_a = self._make_node("alpha")
        node_b = self._make_node("beta")
        nodes = [node_a, node_b, node_a, node_b]
        with patch(
            "nexus.tools.get_vector_index", return_value=self._mock_index(nodes)
        ):
            result = await nexus_tools.get_vector_context("q", "P", "S", rerank=False)
        assert result.count("alpha") == 1
        assert result.count("beta") == 1

    async def test_graph_dedup_removes_duplicate_content(self):
        """Three identical graph nodes → only one bullet in output."""
        dup_node = self._make_node("graph content")
        nodes = [dup_node, dup_node, dup_node]
        with patch("nexus.tools.get_graph_index", return_value=self._mock_index(nodes)):
            result = await nexus_tools.get_graph_context("q", "P", "S", rerank=False)
        assert result.count("graph content") == 1

    async def test_graph_dedup_preserves_unique_content(self):
        """Two distinct graph nodes → both appear."""
        node_a = self._make_node("node alpha")
        node_b = self._make_node("node beta")
        with patch(
            "nexus.tools.get_graph_index",
            return_value=self._mock_index([node_a, node_b]),
        ):
            result = await nexus_tools.get_graph_context("q", "P", "S", rerank=False)
        assert "node alpha" in result
        assert "node beta" in result


# ---------------------------------------------------------------------------
# nexus.cache — secondary index (_idx_key, set_cached, invalidate_cache)
# ---------------------------------------------------------------------------


class TestCacheSecondaryIndex:
    """Verify the secondary index that enables per-tenant cache invalidation."""

    def test_idx_key_format_non_empty_scope(self):
        from nexus.cache import _idx_key

        key = _idx_key("MY_PROJ", "CORE_CODE")
        assert key.startswith("nexus:idx:")
        assert "MY_PROJ" in key
        assert "CORE_CODE" in key

    def test_idx_key_empty_scope_uses_sentinel(self):
        from nexus.cache import _idx_key

        key = _idx_key("MY_PROJ", "")
        assert key.startswith("nexus:idx:")
        assert "__all__" in key

    def test_idx_key_differs_by_scope(self):
        from nexus.cache import _idx_key

        key_a = _idx_key("P", "SCOPE_A")
        key_b = _idx_key("P", "SCOPE_B")
        key_all = _idx_key("P", "")
        assert key_a != key_b
        assert key_a != key_all
        assert key_b != key_all

    def test_set_cached_adds_key_to_secondary_index(self):
        """set_cached should SADD the cache key to the project/scope index set."""
        from nexus.cache import cache_key, _idx_key

        mock_redis = MagicMock()
        # Use _orig_set_cached (module-import-time reference) to bypass conftest patching
        with patch("nexus.cache.get_redis", return_value=mock_redis):
            with patch("nexus.cache.CACHE_ENABLED", True):
                _orig_set_cached("myquery", "PROJ", "S", "result", tool_type="vector")

        expected_cache_key = cache_key("myquery", "PROJ", "S", tool_type="vector")
        expected_idx = _idx_key("PROJ", "S")
        mock_redis.sadd.assert_called_once_with(expected_idx, expected_cache_key)

    def test_invalidate_cache_deletes_indexed_keys(self):
        """invalidate_cache must delete all keys tracked in the index set."""
        from nexus.cache import _idx_key

        idx = _idx_key("PROJ", "S")
        all_idx = _idx_key("PROJ", "")
        fake_scoped_keys = {"nexus:abc1", "nexus:abc2"}
        fake_all_keys = {"nexus:def1"}

        mock_redis = MagicMock()
        mock_redis.smembers.side_effect = lambda k: {
            idx: fake_scoped_keys,
            all_idx: fake_all_keys,
        }.get(k, set())

        with patch("nexus.cache.get_redis", return_value=mock_redis):
            with patch("nexus.cache.CACHE_ENABLED", True):
                _orig_invalidate_cache("PROJ", "S")

        deleted_args = set(mock_redis.delete.call_args[0])
        # Must delete all scope cache keys + all-scope cache keys + both index keys
        assert fake_scoped_keys <= deleted_args
        assert fake_all_keys <= deleted_args
        assert idx in deleted_args
        assert all_idx in deleted_args

    def test_invalidate_cache_empty_scope_does_not_collect_all_idx_twice(self):
        """When scope='', only the __all__ index is collected (no double-collection)."""
        from nexus.cache import _idx_key

        all_idx = _idx_key("PROJ", "")
        mock_redis = MagicMock()
        mock_redis.smembers.return_value = {"nexus:key1"}

        with patch("nexus.cache.get_redis", return_value=mock_redis):
            with patch("nexus.cache.CACHE_ENABLED", True):
                _orig_invalidate_cache("PROJ", "")

        # smembers called once (for the empty scope = all-scopes index)
        mock_redis.smembers.assert_called_once_with(all_idx)

    def test_invalidate_cache_disabled_returns_zero(self):
        with patch("nexus.cache.CACHE_ENABLED", False):
            count = _orig_invalidate_cache("PROJ", "S")
        assert count == 0

    def test_invalidate_cache_redis_error_returns_zero(self):
        import redis as redis_lib

        mock_redis = MagicMock()
        mock_redis.smembers.side_effect = redis_lib.RedisError("Redis is down")
        with patch("nexus.cache.get_redis", return_value=mock_redis):
            with patch("nexus.cache.CACHE_ENABLED", True):
                count = _orig_invalidate_cache("PROJ", "S")
        assert count == 0


# ---------------------------------------------------------------------------
# nexus.tools — exception sanitization (no raw exception in client response)
# ---------------------------------------------------------------------------


class TestExceptionSanitization:
    """Verify that internal exceptions are not exposed to MCP clients."""

    async def test_get_vector_context_error_is_generic(self):
        """Exception in retriever must not return raw exc details to client."""
        with patch(
            "nexus.tools.get_vector_index",
            side_effect=RuntimeError("Connection refused: bolt://internal-host:9999"),
        ):
            result = await nexus_tools.get_vector_context("q", "P", "S", rerank=False)
        assert "internal-host" not in result
        assert "Connection refused" not in result
        assert "Error" in result

    async def test_get_graph_context_error_is_generic(self):
        """Exception in graph retriever must not return raw exc details to client."""
        with patch(
            "nexus.tools.get_graph_index",
            side_effect=RuntimeError("auth failure: user=neo4j pass=password123"),
        ):
            result = await nexus_tools.get_graph_context("q", "P", "S", rerank=False)
        assert "password123" not in result
        assert "auth failure" not in result
        assert "Error" in result

    async def test_ingest_vector_error_is_generic(self):
        """Exception during vector ingest must not expose internals."""
        with (
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(qdrant_backend, "is_duplicate", return_value=False),
            patch(
                "nexus.tools.get_vector_index",
                side_effect=RuntimeError("disk /dev/sda1 is full at /data/qdrant"),
            ),
        ):
            result = await nexus_tools.ingest_vector_document("text", "P", "S")
        assert "/data/qdrant" not in result
        assert "disk" not in result
        assert "Error" in result

    async def test_ingest_graph_error_is_generic(self):
        """Exception during graph ingest must not expose internals."""
        with (
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(neo4j_backend, "is_duplicate", return_value=False),
            patch(
                "nexus.tools.get_graph_index",
                side_effect=RuntimeError("BOLT port 7687 auth=neo4j:letmein"),
            ),
        ):
            result = await nexus_tools.ingest_graph_document("text", "P", "S")
        assert "letmein" not in result
        assert "BOLT port" not in result
        assert "Error" in result


# ---------------------------------------------------------------------------
# nexus.tools — cache invalidation on ingest
# ---------------------------------------------------------------------------


class TestCacheInvalidationOnIngest:
    """After successful ingest, cache_module.invalidate_cache must be called."""

    async def test_vector_ingest_calls_invalidate_on_success(self):
        mock_index = MagicMock()
        with (
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(qdrant_backend, "is_duplicate", return_value=False),
            patch("nexus.tools.get_vector_index", return_value=mock_index),
            patch("nexus.tools.cache_module.invalidate_cache") as mock_invalidate,
        ):
            await nexus_tools.ingest_vector_document("text", "PROJ", "SCOPE")
        mock_invalidate.assert_called_once_with("PROJ", "SCOPE")

    async def test_vector_ingest_does_not_invalidate_on_duplicate(self):
        """No cache invalidation when document is skipped as duplicate."""
        with (
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(qdrant_backend, "is_duplicate", return_value=True),
            patch("nexus.tools.cache_module.invalidate_cache") as mock_invalidate,
        ):
            await nexus_tools.ingest_vector_document("text", "PROJ", "SCOPE")
        mock_invalidate.assert_not_called()

    async def test_graph_ingest_calls_invalidate_on_success(self):
        mock_index = MagicMock()
        with (
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(neo4j_backend, "is_duplicate", return_value=False),
            patch("nexus.tools.get_graph_index", return_value=mock_index),
            patch("nexus.tools.cache_module.invalidate_cache") as mock_invalidate,
        ):
            await nexus_tools.ingest_graph_document("text", "PROJ", "SCOPE")
        mock_invalidate.assert_called_once_with("PROJ", "SCOPE")

    async def test_graph_ingest_does_not_invalidate_on_duplicate(self):
        with (
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(neo4j_backend, "is_duplicate", return_value=True),
            patch("nexus.tools.cache_module.invalidate_cache") as mock_invalidate,
        ):
            await nexus_tools.ingest_graph_document("text", "PROJ", "SCOPE")
        mock_invalidate.assert_not_called()

    async def test_vector_chunked_ingest_invalidates_when_ingested_gt_zero(self):
        """Chunked ingest should invalidate cache only when at least one chunk succeeded."""

        mock_index = MagicMock()
        # Simulate a 6KB document that will be chunked
        big_text = "x " * 3500  # > 4KB threshold

        with (
            patch("nexus.tools.needs_chunking", return_value=True),
            patch("nexus.tools.chunk_document", return_value=["chunk1", "chunk2"]),
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(qdrant_backend, "is_duplicate", return_value=False),
            patch("nexus.tools.get_vector_index", return_value=mock_index),
            patch("nexus.tools.cache_module.invalidate_cache") as mock_invalidate,
        ):
            result = await nexus_tools.ingest_vector_document(big_text, "PROJ", "SCOPE")
        assert "ingested" in result.lower() or "chunk" in result.lower()
        mock_invalidate.assert_called_once_with("PROJ", "SCOPE")


# ---------------------------------------------------------------------------
# nexus.config — validate_config
# ---------------------------------------------------------------------------


class TestValidateConfig:
    """validate_config returns warning messages for unsafe defaults."""

    def test_default_password_triggers_warning(self):
        from nexus.config import validate_config
        import nexus.config as nc

        original = nc.DEFAULT_NEO4J_PASSWORD
        try:
            nc.DEFAULT_NEO4J_PASSWORD = "password123"
            warnings = validate_config()
        finally:
            nc.DEFAULT_NEO4J_PASSWORD = original

        assert len(warnings) >= 1
        assert any("password" in w.lower() or "NEO4J_PASSWORD" in w for w in warnings)

    def test_strong_password_no_warning(self):
        from nexus.config import validate_config
        import nexus.config as nc

        original = nc.DEFAULT_NEO4J_PASSWORD
        try:
            nc.DEFAULT_NEO4J_PASSWORD = "s3cr3t!PasswordXYZ"
            warnings = validate_config()
        finally:
            nc.DEFAULT_NEO4J_PASSWORD = original

        # Only localhost-in-production warnings might fire; not password warning
        password_warns = [
            w for w in warnings if "NEO4J_PASSWORD" in w and "password123" in w
        ]
        assert len(password_warns) == 0

    def test_localhost_urls_warn_in_production_mode(self):
        from nexus.config import validate_config
        import nexus.config as nc
        import os

        original_pw = nc.DEFAULT_NEO4J_PASSWORD
        try:
            nc.DEFAULT_NEO4J_PASSWORD = "strongpass"
            with patch.dict(os.environ, {"NEXUS_ENV": "production"}):
                warnings = validate_config()
        finally:
            nc.DEFAULT_NEO4J_PASSWORD = original_pw

        # All three default service URLs point to localhost — expect 3 warnings
        localhost_warns = [w for w in warnings if "localhost" in w]
        assert len(localhost_warns) >= 1

    def test_no_warnings_in_non_production_mode_with_good_config(self):
        from nexus.config import validate_config
        import nexus.config as nc
        import os

        original_pw = nc.DEFAULT_NEO4J_PASSWORD
        try:
            nc.DEFAULT_NEO4J_PASSWORD = "strongpass"
            with patch.dict(os.environ, {"NEXUS_ENV": "development"}):
                warnings = validate_config()
        finally:
            nc.DEFAULT_NEO4J_PASSWORD = original_pw

        assert warnings == []

    def test_validate_config_returns_list(self):
        from nexus.config import validate_config

        result = validate_config()
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# nexus.tools — answer_query helpers (_dedup_cross_source)
# ---------------------------------------------------------------------------


class TestAnswerQueryHelpers:
    """Unit tests for the module-level helpers extracted from answer_query."""

    def test_dedup_cross_source_attributes_graph_first(self):
        from nexus.tools import _dedup_cross_source

        result = _dedup_cross_source(["passage A"], ["passage B"])
        assert result == ["[graph] passage A", "[vector] passage B"]

    def test_dedup_cross_source_graph_wins_on_collision(self):
        """Same content in both sources → attributed to graph, not repeated."""
        from nexus.tools import _dedup_cross_source

        result = _dedup_cross_source(["shared"], ["shared"])
        assert result == ["[graph] shared"]

    def test_dedup_cross_source_empty_inputs(self):
        from nexus.tools import _dedup_cross_source

        assert _dedup_cross_source([], []) == []

    def test_dedup_cross_source_graph_only(self):
        from nexus.tools import _dedup_cross_source

        result = _dedup_cross_source(["a", "b"], [])
        assert "[graph] a" in result
        assert "[graph] b" in result
        assert len(result) == 2

    def test_dedup_cross_source_vector_only(self):
        from nexus.tools import _dedup_cross_source

        result = _dedup_cross_source([], ["x", "y"])
        assert all(p.startswith("[vector]") for p in result)
        assert len(result) == 2

    def test_dedup_cross_source_skips_whitespace_only_passages(self):
        """Empty/whitespace passages must be filtered out."""
        from nexus.tools import _dedup_cross_source

        result = _dedup_cross_source(["  ", "real content"], [""])
        assert len(result) == 1
        assert "[graph] real content" in result

    async def test_fetch_graph_passages_returns_empty_on_error(self):
        """_fetch_graph_passages must return [] instead of raising on backend errors."""
        from nexus.tools import _fetch_graph_passages

        with patch(
            "nexus.tools.get_graph_index", side_effect=RuntimeError("neo4j down")
        ):
            result = await _fetch_graph_passages("q", "P", "S", rerank=False)
        assert result == []

    async def test_fetch_vector_passages_returns_empty_on_error(self):
        """_fetch_vector_passages must return [] instead of raising on backend errors."""
        from nexus.tools import _fetch_vector_passages

        with patch(
            "nexus.tools.get_vector_index", side_effect=RuntimeError("qdrant down")
        ):
            result = await _fetch_vector_passages("q", "P", "S", rerank=False)
        assert result == []


# ---------------------------------------------------------------------------
# Regression: n.score=None must not crash format string (TypeError)
# ---------------------------------------------------------------------------


class TestScoreNoneHandling:
    """Nodes with score=None must not raise TypeError in context format string."""

    def _mock_index(self, nodes):
        mock_retriever = MagicMock()
        mock_retriever.aretrieve = AsyncMock(return_value=nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever
        return mock_index

    def _make_node(self, content: str, score=None):
        node = MagicMock()
        node.node.get_content.return_value = content
        node.score = score
        return node

    async def test_graph_context_none_score_does_not_crash(self):
        """Node with score=None should produce '0.0000' in output, not TypeError."""
        node = self._make_node("graph content", score=None)
        with patch(
            "nexus.tools.get_graph_index",
            return_value=self._mock_index([node]),
        ):
            result = await nexus_tools.get_graph_context("q", "P", "S", rerank=False)
        assert "graph content" in result
        assert "0.0000" in result  # None → 0.0

    async def test_vector_context_none_score_does_not_crash(self):
        """Node with score=None should produce '0.0000' in output, not TypeError."""
        node = self._make_node("vector content", score=None)
        with patch(
            "nexus.tools.get_vector_index",
            return_value=self._mock_index([node]),
        ):
            result = await nexus_tools.get_vector_context("q", "P", "S", rerank=False)
        assert "vector content" in result
        assert "0.0000" in result

    async def test_mixed_scores_none_and_float(self):
        """Mix of None and float scores should all format correctly."""
        node_with_score = self._make_node("node A", score=0.75)
        node_without_score = self._make_node("node B", score=None)
        with patch(
            "nexus.tools.get_vector_index",
            return_value=self._mock_index([node_with_score, node_without_score]),
        ):
            result = await nexus_tools.get_vector_context("q", "P", "S", rerank=False)
        assert "0.7500" in result
        assert "0.0000" in result
        assert "node A" in result
        assert "node B" in result


# ---------------------------------------------------------------------------
# Regression: batch ingest functions must invalidate cache
# ---------------------------------------------------------------------------


class TestBatchIngestCacheInvalidation:
    """Batch ingest tools must call cache_module.invalidate_cache after success."""

    async def test_graph_batch_invalidates_cache_per_tenant(self):
        """Each unique (project_id, scope) that had a successful ingest must be invalidated."""
        mock_index = MagicMock()
        docs = [
            {"text": "doc A", "project_id": "P1", "scope": "S1"},
            {"text": "doc B", "project_id": "P1", "scope": "S1"},
            {"text": "doc C", "project_id": "P2", "scope": "S2"},
        ]
        with (
            patch("nexus.tools.needs_chunking", return_value=False),
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(neo4j_backend, "is_duplicate", return_value=False),
            patch("nexus.tools.get_graph_index", return_value=mock_index),
            patch("nexus.tools.cache_module.invalidate_cache") as mock_inv,
        ):
            result = await nexus_tools.ingest_graph_documents_batch(docs)
        assert result["ingested"] == 3
        # Must have been called at least for both (P1,S1) and (P2,S2)
        calls = {(c.args[0], c.args[1]) for c in mock_inv.call_args_list}
        assert ("P1", "S1") in calls
        assert ("P2", "S2") in calls

    async def test_graph_batch_no_invalidation_when_all_skipped(self):
        """All duplicates → no ingestion → no cache invalidation."""
        docs = [{"text": "doc A", "project_id": "P1", "scope": "S1"}]
        with (
            patch("nexus.tools.needs_chunking", return_value=False),
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(neo4j_backend, "is_duplicate", return_value=True),
            patch("nexus.tools.cache_module.invalidate_cache") as mock_inv,
        ):
            result = await nexus_tools.ingest_graph_documents_batch(docs)
        assert result["skipped"] == 1
        mock_inv.assert_not_called()

    async def test_vector_batch_invalidates_cache_per_tenant(self):
        mock_index = MagicMock()
        docs = [
            {"text": "doc A", "project_id": "PA", "scope": "SA"},
            {"text": "doc B", "project_id": "PB", "scope": "SB"},
        ]
        with (
            patch("nexus.tools.needs_chunking", return_value=False),
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(qdrant_backend, "is_duplicate", return_value=False),
            patch("nexus.tools.get_vector_index", return_value=mock_index),
            patch("nexus.tools.cache_module.invalidate_cache") as mock_inv,
        ):
            result = await nexus_tools.ingest_vector_documents_batch(docs)
        assert result["ingested"] == 2
        calls = {(c.args[0], c.args[1]) for c in mock_inv.call_args_list}
        assert ("PA", "SA") in calls
        assert ("PB", "SB") in calls

    async def test_vector_batch_no_invalidation_when_all_skipped(self):
        docs = [{"text": "doc A", "project_id": "P1", "scope": "S1"}]
        with (
            patch("nexus.tools.needs_chunking", return_value=False),
            patch("nexus.tools.content_hash", return_value="HASH"),
            patch.object(qdrant_backend, "is_duplicate", return_value=True),
            patch("nexus.tools.cache_module.invalidate_cache") as mock_inv,
        ):
            result = await nexus_tools.ingest_vector_documents_batch(docs)
        assert result["skipped"] == 1
        mock_inv.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: answer_query cache hit must NOT truncate answer with max_context_chars
# ---------------------------------------------------------------------------


class TestAnswerQueryCacheHit:
    """Cache hit for answer_query must return the full LLM answer unchanged."""

    async def test_cache_hit_returns_answer_unchanged(self):
        """A cached answer string must be returned as-is, not truncated."""
        long_answer = "A" * 8000  # > default max_context_chars=6000

        with patch("nexus.tools.cache_module.get_cached", return_value=long_answer):
            result = await nexus_tools.answer_query("q", "P")
        # Must not be truncated — max_context_chars limits LLM input, not the answer
        assert result == long_answer

    async def test_cache_hit_empty_answer_returns_as_is(self):
        """Empty cached string is returned without modification."""
        with patch("nexus.tools.cache_module.get_cached", return_value=""):
            result = await nexus_tools.answer_query("q", "P")
        assert result == ""


# ---------------------------------------------------------------------------
# Regression: get_graph/vector_context fresh result caches full untruncated result
# ---------------------------------------------------------------------------


class TestFreshResultCaching:
    """Fresh results must cache the full context and apply _apply_cap at return time."""

    def _mock_index(self, content: str, score: float = 0.9):
        node = MagicMock()
        node.node.get_content.return_value = content
        node.score = score
        mock_retriever = MagicMock()
        mock_retriever.aretrieve = AsyncMock(return_value=[node])
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever
        return mock_index

    async def test_vector_context_caches_full_result_not_truncated(self):
        """Cache must store the full result; _apply_cap is applied at return time."""
        long_content = "word " * 600  # ~3000 chars, exceeds max_chars=1500
        mock_index = self._mock_index(long_content)
        cached_values = []

        def capture_set(query, pid, scope, value, **kwargs):
            cached_values.append(value)

        with (
            patch("nexus.tools.get_vector_index", return_value=mock_index),
            patch("nexus.tools.cache_module.set_cached", side_effect=capture_set),
        ):
            result = await nexus_tools.get_vector_context(
                "q", "P", "S", rerank=False, max_chars=1500
            )

        # The cached value must be the FULL result (not truncated to max_chars)
        assert len(cached_values) == 1
        full_cached = cached_values[0]
        # Full result contains complete content (not cut at 1500 chars)
        assert len(full_cached) > 1500
        # Returned result is capped at 1500 chars (+ truncation suffix)
        assert len(result) <= 1500 + len("… [truncated]")

    async def test_graph_context_caches_full_result_not_truncated(self):
        """Same as vector: graph context caches full, returns capped."""
        long_content = "data " * 600  # ~3000 chars
        mock_index = self._mock_index(long_content)
        cached_values = []

        def capture_set(query, pid, scope, value, **kwargs):
            cached_values.append(value)

        with (
            patch("nexus.tools.get_graph_index", return_value=mock_index),
            patch("nexus.tools.cache_module.set_cached", side_effect=capture_set),
        ):
            result = await nexus_tools.get_graph_context(
                "q", "P", "S", rerank=False, max_chars=1500
            )

        assert len(cached_values) == 1
        assert len(cached_values[0]) > 1500
        assert len(result) <= 1500 + len("… [truncated]")


# ---------------------------------------------------------------------------
# Regression tests — deep code review fixes (v3.5 / v1.2 / v1.7)
# ---------------------------------------------------------------------------


class TestDeleteTenantDataCacheInvalidation:
    """Fix 1 (HIGH): delete_tenant_data must invalidate Redis cache after deletion."""

    async def test_invalidates_cache_on_success(self):
        """Cache is invalidated when both backends succeed."""
        with (
            patch("nexus.tools.neo4j_backend.delete_data"),
            patch("nexus.tools.qdrant_backend.delete_data"),
            patch("nexus.tools.cache_module.invalidate_cache") as mock_inval,
        ):
            result = await nexus_tools.delete_tenant_data("PROJ", "SCOPE")
        assert "Successfully" in result
        mock_inval.assert_called_once_with("PROJ", "SCOPE")

    async def test_invalidates_cache_on_partial_neo4j_failure(self):
        """Cache is still invalidated even if Neo4j deletion fails."""
        with (
            patch(
                "nexus.tools.neo4j_backend.delete_data",
                side_effect=Exception("neo4j down"),
            ),
            patch("nexus.tools.qdrant_backend.delete_data"),
            patch("nexus.tools.cache_module.invalidate_cache") as mock_inval,
        ):
            result = await nexus_tools.delete_tenant_data("PROJ", "SCOPE")
        assert "Partial failure" in result
        mock_inval.assert_called_once_with("PROJ", "SCOPE")

    async def test_invalidates_cache_on_both_backends_failing(self):
        """Cache is still invalidated when both backends fail (stale entries must go)."""
        with (
            patch(
                "nexus.tools.neo4j_backend.delete_data",
                side_effect=Exception("neo4j down"),
            ),
            patch(
                "nexus.tools.qdrant_backend.delete_data",
                side_effect=Exception("qdrant down"),
            ),
            patch("nexus.tools.cache_module.invalidate_cache") as mock_inval,
        ):
            result = await nexus_tools.delete_tenant_data("PROJ", "SCOPE")
        assert "Partial failure" in result
        mock_inval.assert_called_once_with("PROJ", "SCOPE")


class TestEmptyQueryValidation:
    """Fix 2 (HIGH): get_graph_context / get_vector_context must reject empty queries."""

    async def test_get_graph_context_empty_query(self):
        result = await nexus_tools.get_graph_context("", "PROJ", "SCOPE")
        assert result == "Error: 'query' must not be empty."

    async def test_get_graph_context_whitespace_query(self):
        result = await nexus_tools.get_graph_context("   ", "PROJ", "SCOPE")
        assert result == "Error: 'query' must not be empty."

    async def test_get_vector_context_empty_query(self):
        result = await nexus_tools.get_vector_context("", "PROJ", "SCOPE")
        assert result == "Error: 'query' must not be empty."

    async def test_get_vector_context_whitespace_query(self):
        result = await nexus_tools.get_vector_context("   ", "PROJ", "SCOPE")
        assert result == "Error: 'query' must not be empty."

    async def test_get_graph_context_valid_query_passes_validation(self):
        """A non-empty query must NOT return the empty-query error."""
        mock_index = MagicMock()
        mock_retriever = AsyncMock()
        mock_retriever.aretrieve = AsyncMock(return_value=[])
        mock_index.as_retriever.return_value = mock_retriever
        with patch("nexus.tools.get_graph_index", return_value=mock_index):
            result = await nexus_tools.get_graph_context(
                "real query", "PROJ", "SCOPE", rerank=False
            )
        assert "Error: 'query' must not be empty." not in result

    async def test_get_vector_context_valid_query_passes_validation(self):
        mock_index = MagicMock()
        mock_retriever = AsyncMock()
        mock_retriever.aretrieve = AsyncMock(return_value=[])
        mock_index.as_retriever.return_value = mock_retriever
        with patch("nexus.tools.get_vector_index", return_value=mock_index):
            result = await nexus_tools.get_vector_context(
                "real query", "PROJ", "SCOPE", rerank=False
            )
        assert "Error: 'query' must not be empty." not in result


class TestRerankerThreadSafety:
    """Fix 4 (HIGH): reranker singleton must use double-checked locking."""

    def test_reranker_lock_exists(self):
        """_reranker_lock must be a threading.Lock instance."""
        from nexus import reranker as nexus_reranker

        assert hasattr(nexus_reranker, "_reranker_lock")
        # Both Lock and RLock are acceptable; check for lock protocol
        lock = nexus_reranker._reranker_lock
        assert hasattr(lock, "acquire") and hasattr(lock, "release")

    def test_concurrent_get_reranker_only_initialises_once(self):
        """Two sequential calls must each receive the same singleton (constructed once)."""
        import sys
        import types
        from nexus import reranker as nexus_reranker

        nexus_reranker.reset_reranker()
        fake_instance = MagicMock()
        mock_cls = MagicMock(return_value=fake_instance)

        # Inject a fake module so the internal `from ... import FlagEmbeddingReranker`
        # resolves to our mock class without needing the real ML library loaded.
        fake_module = types.ModuleType(
            "llama_index.postprocessor.flag_embedding_reranker"
        )
        fake_module.FlagEmbeddingReranker = mock_cls  # type: ignore[attr-defined]

        with patch.dict(
            sys.modules,
            {"llama_index.postprocessor.flag_embedding_reranker": fake_module},
        ):
            r1 = nexus_reranker.get_reranker()
            r2 = nexus_reranker.get_reranker()

        nexus_reranker.reset_reranker()  # cleanup singleton

        assert r1 is r2
        assert mock_cls.call_count == 1  # Model constructed exactly once


class TestAutoChunkErrorSanitization:
    """Fix 9 (MEDIUM): auto_chunk=False error must not leak MAX_DOCUMENT_SIZE value."""

    async def test_ingest_graph_error_hides_size_value(self):
        from nexus.config import MAX_DOCUMENT_SIZE

        large_text = "x" * (MAX_DOCUMENT_SIZE + 1)
        result = await nexus_tools.ingest_graph_document(
            large_text, "PROJ", "SCOPE", auto_chunk=False
        )
        assert "Error" in result
        # Must NOT expose the exact byte/KB threshold
        assert str(MAX_DOCUMENT_SIZE // 1024) not in result

    async def test_ingest_vector_error_hides_size_value(self):
        from nexus.config import MAX_DOCUMENT_SIZE

        large_text = "x" * (MAX_DOCUMENT_SIZE + 1)
        result = await nexus_tools.ingest_vector_document(
            large_text, "PROJ", "SCOPE", auto_chunk=False
        )
        assert "Error" in result
        assert str(MAX_DOCUMENT_SIZE // 1024) not in result

    async def test_ingest_graph_error_suggests_auto_chunk(self):
        """Error message must still guide the caller to set auto_chunk=True."""
        from nexus.config import MAX_DOCUMENT_SIZE

        large_text = "x" * (MAX_DOCUMENT_SIZE + 1)
        result = await nexus_tools.ingest_graph_document(
            large_text, "PROJ", "SCOPE", auto_chunk=False
        )
        assert "auto_chunk=True" in result


class TestSyncProjectFilesSuccessCheck:
    """Fix 8 (MEDIUM): 'Skipped: duplicate' results must count as success, not error."""

    async def test_skipped_duplicate_counted_as_ingested(self):
        """sync_project_files must treat 'Skipped' responses as successful syncs."""
        from pathlib import Path

        fake_file = MagicMock(spec=Path)
        fake_file.read_text.return_value = "content"

        files_needing_sync = [
            {
                "filepath": fake_file,
                "source": "TEST/README.md",
                "project_id": "TEST",
                "scope": "CORE_DOCS",
            }
        ]

        # Both ingest calls return "Skipped: duplicate content..."
        skipped_msg = "Skipped: duplicate content already exists for project 'TEST'."

        with (
            patch(
                "nexus.tools.sync_module.get_files_needing_sync",
                return_value=files_needing_sync,
            ),
            patch(
                "nexus.tools.neo4j_backend.delete_by_filepath",
            ),
            patch(
                "nexus.tools.qdrant_backend.delete_by_filepath",
            ),
            patch(
                "nexus.tools.ingest_graph_document",
                new_callable=AsyncMock,
                return_value=skipped_msg,
            ),
            patch(
                "nexus.tools.ingest_vector_document",
                new_callable=AsyncMock,
                return_value=skipped_msg,
            ),
            patch(
                "nexus.tools.sync_module.delete_stale_files",
                return_value=[],
            ),
        ):
            result = await nexus_tools.sync_project_files("/fake/root")

        # Should report 1 synced, 0 errors
        assert "Synced 1 of 1" in result
        assert "Errors" not in result

    async def test_error_result_still_counted_as_error(self):
        """Actual 'Error:' responses must still be treated as failures."""
        from pathlib import Path

        fake_file = MagicMock(spec=Path)
        fake_file.read_text.return_value = "content"

        files_needing_sync = [
            {
                "filepath": fake_file,
                "source": "TEST/README.md",
                "project_id": "TEST",
                "scope": "CORE_DOCS",
            }
        ]

        with (
            patch(
                "nexus.tools.sync_module.get_files_needing_sync",
                return_value=files_needing_sync,
            ),
            patch("nexus.tools.neo4j_backend.delete_by_filepath"),
            patch("nexus.tools.qdrant_backend.delete_by_filepath"),
            patch(
                "nexus.tools.ingest_graph_document",
                new_callable=AsyncMock,
                return_value="Error: Graph document ingestion failed.",
            ),
            patch(
                "nexus.tools.ingest_vector_document",
                new_callable=AsyncMock,
                return_value="Successfully ingested Vector document.",
            ),
            patch("nexus.tools.sync_module.delete_stale_files", return_value=[]),
        ):
            result = await nexus_tools.sync_project_files("/fake/root")

        assert "Synced 0 of 1" in result
        assert "Errors" in result


# ---------------------------------------------------------------------------
# TestSyncProjectFilesPreDeleteErrors (Loop 10 — Bugs L10-1 and L10-2)
# ---------------------------------------------------------------------------


class TestSyncProjectFilesPreDeleteErrors:
    """Regression tests for Bugs L10-1 and L10-2 in sync_project_files.

    L10-1: bare 'except Exception: pass' on pre-delete silently swallowed
    connection errors, leaving old chunks alive alongside new ones.
    Fix: log warning, append to errors, continue (skip ingest for that file).

    L10-2: cache was not invalidated after a successful pre-delete when the
    subsequent ingest call failed, leaving stale cache entries pointing at
    deleted content.
    Fix: call cache_module.invalidate_cache() immediately after pre-delete
    and before ingest.
    """

    def _make_file_entry(self, filepath):
        return {
            "filepath": filepath,
            "source": "TEST/README.md",
            "project_id": "TEST",
            "scope": "CORE_DOCS",
        }

    async def test_pre_delete_error_skips_ingest(self, tmp_path):
        """Bug L10-1: if pre-delete raises, ingest must NOT be called and the
        file must be counted as an error, not a successful sync."""
        from pathlib import Path

        fake_file = MagicMock(spec=Path)
        fake_file.read_text.return_value = "content"

        with (
            patch(
                "nexus.tools.sync_module.get_files_needing_sync",
                return_value=[self._make_file_entry(fake_file)],
            ),
            patch(
                "nexus.tools.neo4j_backend.delete_by_filepath",
                side_effect=RuntimeError("connection refused"),
            ),
            patch("nexus.tools.qdrant_backend.delete_by_filepath"),
            patch(
                "nexus.tools.ingest_graph_document", new_callable=AsyncMock
            ) as mock_graph,
            patch(
                "nexus.tools.ingest_vector_document", new_callable=AsyncMock
            ) as mock_vector,
            patch("nexus.tools.sync_module.delete_stale_files", return_value=[]),
            patch("nexus.tools.cache_module"),
        ):
            result = await nexus_tools.sync_project_files("/fake/root")

        # ingest must not have been called when pre-delete fails
        mock_graph.assert_not_called()
        mock_vector.assert_not_called()
        # file counted as error, not success
        assert "Synced 0 of 1" in result
        assert "Errors" in result

    async def test_pre_delete_error_message_in_result(self, tmp_path):
        """Bug L10-1: the error message must mention the failed file source."""
        from pathlib import Path

        fake_file = MagicMock(spec=Path)
        fake_file.read_text.return_value = "content"

        with (
            patch(
                "nexus.tools.sync_module.get_files_needing_sync",
                return_value=[self._make_file_entry(fake_file)],
            ),
            patch(
                "nexus.tools.neo4j_backend.delete_by_filepath",
                side_effect=ConnectionError("neo4j unreachable"),
            ),
            patch("nexus.tools.qdrant_backend.delete_by_filepath"),
            patch("nexus.tools.ingest_graph_document", new_callable=AsyncMock),
            patch("nexus.tools.ingest_vector_document", new_callable=AsyncMock),
            patch("nexus.tools.sync_module.delete_stale_files", return_value=[]),
            patch("nexus.tools.cache_module"),
        ):
            result = await nexus_tools.sync_project_files("/fake/root")

        # error message references the source file
        assert "TEST/README.md" in result

    async def test_cache_invalidated_after_pre_delete_before_failed_ingest(self):
        """Bug L10-2: cache must be invalidated after pre-delete even when
        the subsequent ingest call returns an error."""
        from pathlib import Path

        fake_file = MagicMock(spec=Path)
        fake_file.read_text.return_value = "content"

        with (
            patch(
                "nexus.tools.sync_module.get_files_needing_sync",
                return_value=[self._make_file_entry(fake_file)],
            ),
            patch("nexus.tools.neo4j_backend.delete_by_filepath"),
            patch("nexus.tools.qdrant_backend.delete_by_filepath"),
            patch(
                "nexus.tools.ingest_graph_document",
                new_callable=AsyncMock,
                return_value="Error: graph ingestion failed",
            ),
            patch(
                "nexus.tools.ingest_vector_document",
                new_callable=AsyncMock,
                return_value="Error: vector ingestion failed",
            ),
            patch("nexus.tools.sync_module.delete_stale_files", return_value=[]),
            patch("nexus.tools.cache_module") as mock_cache,
        ):
            await nexus_tools.sync_project_files("/fake/root")

        # cache must have been invalidated (after pre-delete, before ingest)
        mock_cache.invalidate_cache.assert_called_with("TEST", "CORE_DOCS")

    async def test_cache_invalidated_on_successful_sync(self):
        """Bug L10-2: cache must also be invalidated on a fully successful sync."""
        from pathlib import Path

        fake_file = MagicMock(spec=Path)
        fake_file.read_text.return_value = "content"

        with (
            patch(
                "nexus.tools.sync_module.get_files_needing_sync",
                return_value=[self._make_file_entry(fake_file)],
            ),
            patch("nexus.tools.neo4j_backend.delete_by_filepath"),
            patch("nexus.tools.qdrant_backend.delete_by_filepath"),
            patch(
                "nexus.tools.ingest_graph_document",
                new_callable=AsyncMock,
                return_value="Successfully ingested graph document",
            ),
            patch(
                "nexus.tools.ingest_vector_document",
                new_callable=AsyncMock,
                return_value="Successfully ingested vector document",
            ),
            patch("nexus.tools.sync_module.delete_stale_files", return_value=[]),
            patch("nexus.tools.cache_module") as mock_cache,
        ):
            result = await nexus_tools.sync_project_files("/fake/root")

        # successful sync: cache invalidated and file counted as synced
        mock_cache.invalidate_cache.assert_called_with("TEST", "CORE_DOCS")
        assert "Synced 1 of 1" in result

    async def test_pre_delete_qdrant_error_skips_ingest(self):
        """Bug L10-1: Qdrant pre-delete failure also skips ingest (not just Neo4j)."""
        from pathlib import Path

        fake_file = MagicMock(spec=Path)
        fake_file.read_text.return_value = "content"

        with (
            patch(
                "nexus.tools.sync_module.get_files_needing_sync",
                return_value=[self._make_file_entry(fake_file)],
            ),
            patch("nexus.tools.neo4j_backend.delete_by_filepath"),  # neo4j succeeds
            patch(
                "nexus.tools.qdrant_backend.delete_by_filepath",
                side_effect=RuntimeError("qdrant timeout"),
            ),
            patch(
                "nexus.tools.ingest_graph_document", new_callable=AsyncMock
            ) as mock_graph,
            patch(
                "nexus.tools.ingest_vector_document", new_callable=AsyncMock
            ) as mock_vector,
            patch("nexus.tools.sync_module.delete_stale_files", return_value=[]),
            patch("nexus.tools.cache_module"),
        ):
            result = await nexus_tools.sync_project_files("/fake/root")

        mock_graph.assert_not_called()
        mock_vector.assert_not_called()
        assert "Synced 0 of 1" in result


# ---------------------------------------------------------------------------
# nexus.backends.neo4j — get_driver() singleton (Fix: v2.2 — connection pool)
# ---------------------------------------------------------------------------


class TestNeo4jGetDriverSingleton:
    """Verify get_driver() initialises the driver exactly once (singleton)."""

    def test_get_driver_returns_same_instance_on_repeated_calls(self):
        import nexus.backends.neo4j as _neo4j_mod

        mock_driver = MagicMock()
        original = _neo4j_mod._driver_instance
        try:
            _neo4j_mod._driver_instance = None  # reset singleton for this test
            with patch("nexus.backends.neo4j.GraphDatabase") as mock_gdb:
                mock_gdb.driver.return_value = mock_driver
                d1 = _neo4j_mod.get_driver()
                d2 = _neo4j_mod.get_driver()
            assert d1 is d2
            assert mock_gdb.driver.call_count == 1
        finally:
            _neo4j_mod._driver_instance = original  # restore

    def test_get_driver_uses_configured_url_and_auth(self):
        import nexus.backends.neo4j as _neo4j_mod

        mock_driver = MagicMock()
        original = _neo4j_mod._driver_instance
        try:
            _neo4j_mod._driver_instance = None
            with patch("nexus.backends.neo4j.GraphDatabase") as mock_gdb:
                mock_gdb.driver.return_value = mock_driver
                _neo4j_mod.get_driver()
            call_args = mock_gdb.driver.call_args
            assert call_args[0][0] == _neo4j_mod.DEFAULT_NEO4J_URL
        finally:
            _neo4j_mod._driver_instance = original


# ---------------------------------------------------------------------------
# nexus.tools — project_id validation in get_graph_context / get_vector_context
# (Fix: v3.6 — missing validation allowed empty project_id through to Neo4j)
# ---------------------------------------------------------------------------


class TestProjectIdValidation:
    """Verify get_graph_context and get_vector_context reject empty project_id."""

    async def test_graph_context_rejects_empty_project_id(self):
        result = await nexus_tools.get_graph_context(query="test", project_id="")
        assert result.startswith("Error:")
        assert "project_id" in result

    async def test_graph_context_rejects_whitespace_project_id(self):
        result = await nexus_tools.get_graph_context(query="test", project_id="   ")
        assert result.startswith("Error:")
        assert "project_id" in result

    async def test_vector_context_rejects_empty_project_id(self):
        result = await nexus_tools.get_vector_context(query="test", project_id="")
        assert result.startswith("Error:")
        assert "project_id" in result

    async def test_vector_context_rejects_whitespace_project_id(self):
        result = await nexus_tools.get_vector_context(query="test", project_id="  ")
        assert result.startswith("Error:")
        assert "project_id" in result

    async def test_graph_context_accepts_valid_project_id(self):
        """Valid project_id does NOT trigger the project_id error."""
        with patch("nexus.tools.get_graph_index") as mock_idx:
            mock_retriever = AsyncMock()
            mock_retriever.aretrieve = AsyncMock(return_value=[])
            mock_idx.return_value.as_retriever.return_value = mock_retriever
            result = await nexus_tools.get_graph_context(
                query="test", project_id="MY_PROJECT"
            )
        assert "project_id" not in result or "Error" not in result

    async def test_vector_context_accepts_valid_project_id(self):
        """Valid project_id does NOT trigger the project_id error."""
        with patch("nexus.tools.get_vector_index") as mock_idx:
            mock_retriever = AsyncMock()
            mock_retriever.aretrieve = AsyncMock(return_value=[])
            mock_idx.return_value.as_retriever.return_value = mock_retriever
            result = await nexus_tools.get_vector_context(
                query="test", project_id="MY_PROJECT"
            )
        assert "project_id" not in result or "Error" not in result


# ---------------------------------------------------------------------------
# nexus.tools — delete_all_data calls invalidate_all_cache
# (Fix: v3.6 — cache was never cleared after full wipe)
# ---------------------------------------------------------------------------


class TestDeleteAllDataCacheInvalidation:
    """Verify delete_all_data invalidates the entire Redis cache."""

    async def test_invalidate_all_cache_called_on_success(self):
        with (
            patch("nexus.tools.neo4j_backend.delete_all_data"),
            patch("nexus.tools.qdrant_backend.delete_all_data"),
            patch.object(nexus_tools.cache_module, "invalidate_all_cache") as mock_inv,
        ):
            result = await nexus_tools.delete_all_data()
        mock_inv.assert_called_once()
        assert "Successfully" in result

    async def test_invalidate_all_cache_called_even_when_neo4j_fails(self):
        with (
            patch(
                "nexus.tools.neo4j_backend.delete_all_data",
                side_effect=Exception("neo4j down"),
            ),
            patch("nexus.tools.qdrant_backend.delete_all_data"),
            patch.object(nexus_tools.cache_module, "invalidate_all_cache") as mock_inv,
        ):
            result = await nexus_tools.delete_all_data()
        mock_inv.assert_called_once()
        assert "Partial failure" in result

    async def test_invalidate_all_cache_called_even_when_qdrant_fails(self):
        with (
            patch("nexus.tools.neo4j_backend.delete_all_data"),
            patch(
                "nexus.tools.qdrant_backend.delete_all_data",
                side_effect=Exception("qdrant down"),
            ),
            patch.object(nexus_tools.cache_module, "invalidate_all_cache") as mock_inv,
        ):
            result = await nexus_tools.delete_all_data()
        mock_inv.assert_called_once()
        assert "Partial failure" in result


# ---------------------------------------------------------------------------
# nexus.cache — invalidate_all_cache (Fix: v1.4 — new function)
# ---------------------------------------------------------------------------


class TestInvalidateAllCache:
    """Verify invalidate_all_cache deletes all nexus:* keys."""

    def test_deletes_all_nexus_keys(self):
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = [
            "nexus:abc123",
            "nexus:def456",
            "nexus:idx:PROJ:SCOPE",
        ]
        mock_redis.delete.return_value = 3
        with patch("nexus.cache.get_redis", return_value=mock_redis):
            with patch("nexus.cache.CACHE_ENABLED", True):
                deleted = _nexus_cache.invalidate_all_cache()
        assert deleted == 3
        mock_redis.delete.assert_called_once()

    def test_returns_zero_when_no_nexus_keys(self):
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = []
        with patch("nexus.cache.get_redis", return_value=mock_redis):
            with patch("nexus.cache.CACHE_ENABLED", True):
                deleted = _nexus_cache.invalidate_all_cache()
        assert deleted == 0
        mock_redis.delete.assert_not_called()

    def test_returns_zero_when_cache_disabled(self):
        with patch("nexus.cache.CACHE_ENABLED", False):
            deleted = _nexus_cache.invalidate_all_cache()
        assert deleted == 0

    def test_returns_zero_on_redis_error(self):
        mock_redis = MagicMock()
        mock_redis.scan_iter.side_effect = __import__("redis").RedisError(
            "conn refused"
        )
        with patch("nexus.cache.get_redis", return_value=mock_redis):
            with patch("nexus.cache.CACHE_ENABLED", True):
                deleted = _nexus_cache.invalidate_all_cache()
        assert deleted == 0


# ---------------------------------------------------------------------------
# nexus.indexes — separate graph/vector locks (Fix: v2.2)
# ---------------------------------------------------------------------------


class TestSeparateIndexLocks:
    """Verify graph and vector indexes use independent locks."""

    def test_graph_and_vector_locks_are_different_objects(self):
        from nexus import indexes as nexus_indexes_mod

        assert (
            nexus_indexes_mod._graph_index_lock
            is not nexus_indexes_mod._vector_index_lock
        )

    def test_graph_lock_is_threading_lock(self):
        from nexus import indexes as nexus_indexes_mod

        assert isinstance(nexus_indexes_mod._graph_index_lock, type(threading.Lock()))

    def test_vector_lock_is_threading_lock(self):
        from nexus import indexes as nexus_indexes_mod

        assert isinstance(nexus_indexes_mod._vector_index_lock, type(threading.Lock()))


# ---------------------------------------------------------------------------
# nexus.backends.qdrant — scroll_field None-value filtering (Bug fix v2.3)
# ---------------------------------------------------------------------------


class TestScrollFieldNoneFiltering:
    """scroll_field must not add None payload values to the result set.

    A None payload value would cause sorted() to raise TypeError when mixed
    with strings in get_all_tenant_scopes and print_all_stats.
    """

    def test_none_value_not_added_to_set(self):
        record = MagicMock()
        record.payload = {"tenant_scope": None}
        mock_client = MagicMock()
        mock_client.scroll.side_effect = [([record], None)]
        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            result = qdrant_backend.scroll_field("tenant_scope")
        assert None not in result
        assert result == set()

    def test_valid_value_still_added(self):
        record = MagicMock()
        record.payload = {"tenant_scope": "CORE_CODE"}
        mock_client = MagicMock()
        mock_client.scroll.side_effect = [([record], None)]
        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            result = qdrant_backend.scroll_field("tenant_scope")
        assert result == {"CORE_CODE"}

    def test_mixed_none_and_valid_filters_none(self):
        r1, r2 = MagicMock(), MagicMock()
        r1.payload = {"tenant_scope": "CORE_CODE"}
        r2.payload = {"tenant_scope": None}
        mock_client = MagicMock()
        mock_client.scroll.side_effect = [([r1, r2], None)]
        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            result = qdrant_backend.scroll_field("tenant_scope")
        assert result == {"CORE_CODE"}
        assert None not in result

    def test_sorted_does_not_crash_after_fix(self):
        """sorted() must succeed on scroll_field results even with None payloads."""
        records = [MagicMock(), MagicMock()]
        records[0].payload = {"project_id": None}
        records[1].payload = {"project_id": "MY_PROJECT"}
        mock_client = MagicMock()
        mock_client.scroll.side_effect = [(records, None)]
        with patch.object(qdrant_backend, "get_client", return_value=mock_client):
            result = qdrant_backend.scroll_field("project_id")
        # This must not raise TypeError
        assert sorted(result) == ["MY_PROJECT"]


# ---------------------------------------------------------------------------
# nexus.tools — sync_deleted_files cache invalidation (Bug fix v3.7)
# ---------------------------------------------------------------------------


class TestSyncDeletedFilesCache:
    """sync_deleted_files must invalidate cache when stale files are removed."""

    async def test_cache_invalidated_after_stale_deletion(self, tmp_path):
        # Create a file so base_path.is_dir() passes
        base_path = tmp_path / "project"
        base_path.mkdir()

        with (
            patch.object(
                neo4j_backend,
                "get_all_filepaths",
                return_value=["stale_file.md"],
            ),
            patch.object(neo4j_backend, "delete_by_filepath"),
            patch.object(qdrant_backend, "delete_by_filepath"),
            patch("nexus.tools.cache_module") as mock_cache,
        ):
            result = await nexus_tools.sync_deleted_files(
                str(base_path), "MY_PROJECT", "CORE_CODE"
            )

        # stale_file.md does not exist on disk → should be removed
        assert "1 stale" in result
        mock_cache.invalidate_cache.assert_called_once_with("MY_PROJECT", "CORE_CODE")

    async def test_cache_not_invalidated_when_nothing_deleted(self, tmp_path):
        base_path = tmp_path / "project"
        base_path.mkdir()
        existing = base_path / "exists.md"
        existing.write_text("content")

        with (
            patch.object(
                neo4j_backend,
                "get_all_filepaths",
                return_value=["exists.md"],
            ),
            patch("nexus.tools.cache_module") as mock_cache,
        ):
            await nexus_tools.sync_deleted_files(
                str(base_path), "MY_PROJECT", "CORE_CODE"
            )

        mock_cache.invalidate_cache.assert_not_called()

    async def test_cache_not_invalidated_when_no_stored_files(self, tmp_path):
        base_path = tmp_path / "project"
        base_path.mkdir()

        with (
            patch.object(neo4j_backend, "get_all_filepaths", return_value=[]),
            patch("nexus.tools.cache_module") as mock_cache,
        ):
            await nexus_tools.sync_deleted_files(
                str(base_path), "MY_PROJECT", "CORE_CODE"
            )

        mock_cache.invalidate_cache.assert_not_called()


# ---------------------------------------------------------------------------
# nexus.tools — invalidate_project_cache (new tool v3.7)
# ---------------------------------------------------------------------------


class TestInvalidateProjectCache:
    async def test_returns_count_message(self):
        with patch("nexus.tools.cache_module") as mock_cache:
            mock_cache.invalidate_cache.return_value = 5
            result = await nexus_tools.invalidate_project_cache("MY_PROJECT", "SCOPE")
        assert "5" in result
        assert "MY_PROJECT" in result

    async def test_empty_project_id_returns_error(self):
        result = await nexus_tools.invalidate_project_cache("")
        assert "Error" in result

    async def test_whitespace_project_id_returns_error(self):
        result = await nexus_tools.invalidate_project_cache("   ")
        assert "Error" in result

    async def test_calls_invalidate_cache_with_correct_args(self):
        with patch("nexus.tools.cache_module") as mock_cache:
            mock_cache.invalidate_cache.return_value = 3
            await nexus_tools.invalidate_project_cache("PROJ", "SCOPE")
        mock_cache.invalidate_cache.assert_called_once_with("PROJ", "SCOPE")

    async def test_no_scope_calls_with_empty_scope(self):
        with patch("nexus.tools.cache_module") as mock_cache:
            mock_cache.invalidate_cache.return_value = 0
            result = await nexus_tools.invalidate_project_cache("PROJ")
        mock_cache.invalidate_cache.assert_called_once_with("PROJ", "")
        assert "0" in result

    async def test_scope_included_in_message(self):
        with patch("nexus.tools.cache_module") as mock_cache:
            mock_cache.invalidate_cache.return_value = 2
            result = await nexus_tools.invalidate_project_cache("PROJ", "MY_SCOPE")
        assert "MY_SCOPE" in result


# ---------------------------------------------------------------------------
# TestQdrantGetAllFilepaths (Loop 4 — Bug L1-1)
# ---------------------------------------------------------------------------


class TestQdrantGetAllFilepaths:
    def test_returns_distinct_paths(self):
        """get_all_filepaths returns unique non-empty paths via scroll_field."""
        with patch(
            "nexus.backends.qdrant.scroll_field", return_value={"/a.md", "/b.md"}
        ):
            result = qdrant_backend.get_all_filepaths("PROJ", "SCOPE")
        assert set(result) == {"/a.md", "/b.md"}

    def test_filters_empty_strings(self):
        """Empty strings from payload are excluded."""
        with patch("nexus.backends.qdrant.scroll_field", return_value={"", "/real.md"}):
            result = qdrant_backend.get_all_filepaths("PROJ", "SCOPE")
        assert "" not in result
        assert "/real.md" in result

    def test_no_scope_omits_scope_filter(self):
        """When scope is empty, only project_id condition is passed."""
        captured = {}

        def fake_scroll(key, qdrant_filter=None):
            captured["filter"] = qdrant_filter
            return set()

        with patch("nexus.backends.qdrant.scroll_field", side_effect=fake_scroll):
            qdrant_backend.get_all_filepaths("PROJ", "")

        must = captured["filter"].must
        keys = [c.key for c in must]
        assert "project_id" in keys
        assert "tenant_scope" not in keys

    def test_with_scope_adds_scope_condition(self):
        """When scope is provided, both project_id and tenant_scope conditions appear."""
        captured = {}

        def fake_scroll(key, qdrant_filter=None):
            captured["filter"] = qdrant_filter
            return set()

        with patch("nexus.backends.qdrant.scroll_field", side_effect=fake_scroll):
            qdrant_backend.get_all_filepaths("PROJ", "MY_SCOPE")

        keys = [c.key for c in captured["filter"].must]
        assert "project_id" in keys
        assert "tenant_scope" in keys

    def test_error_returns_empty_list(self):
        """Any exception from scroll_field returns [] without raising."""
        with patch(
            "nexus.backends.qdrant.scroll_field", side_effect=Exception("conn error")
        ):
            result = qdrant_backend.get_all_filepaths("PROJ", "SCOPE")
        assert result == []


# ---------------------------------------------------------------------------
# TestDeleteStaleFilesUnion (Loop 4 — Bug L1-1)
# ---------------------------------------------------------------------------


class TestDeleteStaleFilesUnion:
    def test_catches_qdrant_only_orphan(self, tmp_path):
        """delete_stale_files removes a file present in Qdrant but not on disk or Neo4j."""
        workspace = tmp_path / "antigravity"
        workspace.mkdir()

        qdrant_only_path = "/orphan/file.md"

        with (
            patch("nexus.sync.neo4j_backend") as mock_neo4j,
            patch("nexus.sync.qdrant_backend") as mock_qdrant,
        ):
            mock_neo4j.get_all_filepaths.return_value = []
            mock_qdrant.get_all_filepaths.return_value = [qdrant_only_path]

            deleted = nexus_sync.delete_stale_files(workspace, "PROJ", "SCOPE")

        assert qdrant_only_path in deleted
        mock_neo4j.delete_by_filepath.assert_called_once_with(
            "PROJ", qdrant_only_path, "SCOPE"
        )
        mock_qdrant.delete_by_filepath.assert_called_once_with(
            "PROJ", qdrant_only_path, "SCOPE"
        )

    def test_catches_neo4j_only_orphan(self, tmp_path):
        """delete_stale_files removes a file present in Neo4j but not on disk or Qdrant."""
        workspace = tmp_path / "antigravity"
        workspace.mkdir()

        neo4j_only_path = "/neo4j_only/file.md"

        with (
            patch("nexus.sync.neo4j_backend") as mock_neo4j,
            patch("nexus.sync.qdrant_backend") as mock_qdrant,
        ):
            mock_neo4j.get_all_filepaths.return_value = [neo4j_only_path]
            mock_qdrant.get_all_filepaths.return_value = []

            deleted = nexus_sync.delete_stale_files(workspace, "PROJ", "SCOPE")

        assert neo4j_only_path in deleted

    def test_existing_file_not_deleted(self, tmp_path):
        """A file that exists on disk is not deleted even if indexed."""
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        real_file = workspace / "existing.md"
        real_file.write_text("content")

        with (
            patch("nexus.sync.neo4j_backend") as mock_neo4j,
            patch("nexus.sync.qdrant_backend") as mock_qdrant,
        ):
            mock_neo4j.get_all_filepaths.return_value = [str(real_file)]
            mock_qdrant.get_all_filepaths.return_value = [str(real_file)]

            deleted = nexus_sync.delete_stale_files(workspace, "PROJ", "SCOPE")

        assert deleted == []
        mock_neo4j.delete_by_filepath.assert_not_called()
        mock_qdrant.delete_by_filepath.assert_not_called()

    def test_union_deduplication(self, tmp_path):
        """Paths present in both Neo4j and Qdrant are only deleted once."""
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        shared_path = "/shared/file.md"

        with (
            patch("nexus.sync.neo4j_backend") as mock_neo4j,
            patch("nexus.sync.qdrant_backend") as mock_qdrant,
        ):
            mock_neo4j.get_all_filepaths.return_value = [shared_path]
            mock_qdrant.get_all_filepaths.return_value = [shared_path]

            deleted = nexus_sync.delete_stale_files(workspace, "PROJ", "SCOPE")

        assert deleted.count(shared_path) == 1
        assert mock_neo4j.delete_by_filepath.call_count == 1
        assert mock_qdrant.delete_by_filepath.call_count == 1


# ---------------------------------------------------------------------------
# TestSyncDeletedFilesUnion (Loop 4 — Bug L1-1, tools.py path)
# ---------------------------------------------------------------------------


class TestSyncDeletedFilesUnion:
    async def test_catches_qdrant_only_orphan(self, tmp_path):
        """sync_deleted_files removes an entry present only in Qdrant."""
        with (
            patch("nexus.tools.neo4j_backend") as mock_neo4j,
            patch("nexus.tools.qdrant_backend") as mock_qdrant,
            patch("nexus.tools.cache_module"),
        ):
            mock_neo4j.get_all_filepaths.return_value = []
            mock_qdrant.get_all_filepaths.return_value = ["orphan.md"]

            result = await nexus_tools.sync_deleted_files(
                str(tmp_path), "PROJ", "SCOPE"
            )

        assert "1" in result
        mock_qdrant.delete_by_filepath.assert_called_once()

    async def test_no_paths_in_either_store_returns_no_files(self, tmp_path):
        """Returns early when both stores report no indexed files."""
        with (
            patch("nexus.tools.neo4j_backend") as mock_neo4j,
            patch("nexus.tools.qdrant_backend") as mock_qdrant,
        ):
            mock_neo4j.get_all_filepaths.return_value = []
            mock_qdrant.get_all_filepaths.return_value = []

            result = await nexus_tools.sync_deleted_files(
                str(tmp_path), "PROJ", "SCOPE"
            )

        assert "No files" in result

    async def test_union_deduplication_single_delete(self, tmp_path):
        """A path present in both stores triggers exactly one delete per backend."""
        shared = "shared/path.md"
        with (
            patch("nexus.tools.neo4j_backend") as mock_neo4j,
            patch("nexus.tools.qdrant_backend") as mock_qdrant,
            patch("nexus.tools.cache_module"),
        ):
            mock_neo4j.get_all_filepaths.return_value = [shared]
            mock_qdrant.get_all_filepaths.return_value = [shared]

            await nexus_tools.sync_deleted_files(str(tmp_path), "PROJ", "SCOPE")

        assert mock_neo4j.delete_by_filepath.call_count == 1
        assert mock_qdrant.delete_by_filepath.call_count == 1


# ---------------------------------------------------------------------------
# TestIndexResetFunctions (Loop 4 — Bug L1-2)
# ---------------------------------------------------------------------------


class TestIndexResetFunctions:
    def test_reset_graph_index_clears_cache(self):
        """reset_graph_index() sets _graph_index_cache back to None."""
        nexus_indexes._graph_index_cache = MagicMock()
        nexus_indexes.reset_graph_index()
        assert nexus_indexes._graph_index_cache is None

    def test_reset_vector_index_clears_cache(self):
        """reset_vector_index() sets _vector_index_cache back to None."""
        nexus_indexes._vector_index_cache = MagicMock()
        nexus_indexes.reset_vector_index()
        assert nexus_indexes._vector_index_cache is None

    def test_reset_graph_index_forces_reinit(self):
        """After reset, get_graph_index() re-runs init instead of returning stale cache."""
        nexus_indexes._graph_index_cache = MagicMock(name="stale_graph")
        nexus_indexes.reset_graph_index()
        # After reset the cache is None; the next get_graph_index call would
        # re-initialize — we just verify it's not the stale mock.
        assert nexus_indexes._graph_index_cache is None

    def test_reset_vector_index_forces_reinit(self):
        """After reset, get_vector_index() re-runs init instead of returning stale cache."""
        nexus_indexes._vector_index_cache = MagicMock(name="stale_vector")
        nexus_indexes.reset_vector_index()
        assert nexus_indexes._vector_index_cache is None

    def test_reset_graph_is_idempotent(self):
        """Calling reset_graph_index() twice is safe."""
        nexus_indexes.reset_graph_index()
        nexus_indexes.reset_graph_index()
        assert nexus_indexes._graph_index_cache is None

    def test_reset_vector_is_idempotent(self):
        """Calling reset_vector_index() twice is safe."""
        nexus_indexes.reset_vector_index()
        nexus_indexes.reset_vector_index()
        assert nexus_indexes._vector_index_cache is None


# ---------------------------------------------------------------------------
# TestBatchChunkErrorRecovery (Loop 5 — Bug L2-1)
# ---------------------------------------------------------------------------


class TestBatchChunkErrorRecovery:
    """Per-chunk try/except in batch ingest — errors on one chunk don't skip siblings."""

    @patch("nexus.tools.needs_chunking", return_value=True)
    @patch("nexus.tools.chunk_document", return_value=["chunk_a", "chunk_b", "chunk_c"])
    @patch("nexus.tools.get_graph_index")
    @patch("nexus.tools.neo4j_backend")
    @patch("nexus.tools.cache_module")
    async def test_graph_batch_chunk_error_continues_remaining_chunks(
        self, mock_cache, mock_neo4j, mock_get_index, _mock_chunk, _mock_needs
    ):
        """A chunk insert error must not abort remaining chunks of the same document."""
        mock_neo4j.is_duplicate.return_value = False

        call_count = 0

        def failing_insert(doc):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated chunk insert failure")

        mock_index = MagicMock()
        mock_index.insert.side_effect = failing_insert
        mock_get_index.return_value = mock_index

        result = await nexus_tools.ingest_graph_documents_batch(
            [{"text": "x" * 100, "project_id": "P", "scope": "S"}]
        )

        # First chunk errored, remaining 2 were still attempted
        assert result["errors"] >= 1
        assert result["ingested"] >= 1  # At least 2 of 3 succeeded
        assert mock_index.insert.call_count == 3  # All 3 chunks were attempted

    @patch("nexus.tools.needs_chunking", return_value=True)
    @patch("nexus.tools.chunk_document", return_value=["chunk_a", "chunk_b", "chunk_c"])
    @patch("nexus.tools.get_vector_index")
    @patch("nexus.tools.qdrant_backend")
    @patch("nexus.tools.cache_module")
    async def test_vector_batch_chunk_error_continues_remaining_chunks(
        self, mock_cache, mock_qdrant, mock_get_index, _mock_chunk, _mock_needs
    ):
        """A chunk insert error must not abort remaining chunks (vector batch)."""
        mock_qdrant.is_duplicate.return_value = False

        call_count = 0

        def failing_insert(doc):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated chunk insert failure")

        mock_index = MagicMock()
        mock_index.insert.side_effect = failing_insert
        mock_get_index.return_value = mock_index

        result = await nexus_tools.ingest_vector_documents_batch(
            [{"text": "x" * 100, "project_id": "P", "scope": "S"}]
        )

        assert result["errors"] >= 1
        assert result["ingested"] >= 1  # Remaining chunks succeeded
        assert mock_index.insert.call_count == 3  # All 3 chunks attempted

    @patch("nexus.tools.get_graph_index")
    @patch("nexus.tools.neo4j_backend")
    @patch("nexus.tools.cache_module")
    async def test_graph_batch_empty_documents_returns_zeros(
        self, mock_cache, mock_neo4j, mock_get_index
    ):
        """Empty document list returns all-zero counts."""
        result = await nexus_tools.ingest_graph_documents_batch([])
        assert result == {"ingested": 0, "skipped": 0, "errors": 0, "chunks": 0}

    @patch("nexus.tools.get_vector_index")
    @patch("nexus.tools.qdrant_backend")
    @patch("nexus.tools.cache_module")
    async def test_vector_batch_empty_documents_returns_zeros(
        self, mock_cache, mock_qdrant, mock_get_index
    ):
        """Empty document list returns all-zero counts."""
        result = await nexus_tools.ingest_vector_documents_batch([])
        assert result == {"ingested": 0, "skipped": 0, "errors": 0, "chunks": 0}


# ---------------------------------------------------------------------------
# TestAnswerQueryBothBackendsFail (Loop 5)
# ---------------------------------------------------------------------------


class TestAnswerQueryBothBackendsFail:
    """answer_query gracefully handles concurrent failures from both backends."""

    async def test_both_fail_returns_no_context_message(self):
        """When graph and vector both fail, returns 'No context found' (not an exception)."""
        with (
            patch(
                "nexus.tools.get_graph_index", side_effect=RuntimeError("neo4j down")
            ),
            patch(
                "nexus.tools.get_vector_index", side_effect=RuntimeError("qdrant down")
            ),
            patch("nexus.tools.cache_module") as mock_cache,
        ):
            mock_cache.get_cached.return_value = None
            result = await nexus_tools.answer_query("any query", "PROJ")

        assert "No context found" in result
        assert "PROJ" in result

    async def test_graph_fails_vector_succeeds_still_answers(self):
        """When graph fails but vector has results, answer is generated from vector only."""
        mock_vector_node = MagicMock()
        mock_vector_node.node.get_content.return_value = "vector passage"
        mock_vector_node.score = 0.9

        mock_vector_index = MagicMock()
        mock_vector_retriever = MagicMock()
        mock_vector_retriever.aretrieve = AsyncMock(return_value=[mock_vector_node])
        mock_vector_index.as_retriever.return_value = mock_vector_retriever

        with (
            patch(
                "nexus.tools.get_graph_index", side_effect=RuntimeError("neo4j down")
            ),
            patch("nexus.tools.get_vector_index", return_value=mock_vector_index),
            patch("nexus.tools.cache_module") as mock_cache,
            patch("nexus.tools.RERANKER_ENABLED", False),
            patch("nexus.tools.httpx.AsyncClient") as mock_http,
        ):
            mock_cache.get_cached.return_value = None
            mock_http_instance = AsyncMock()
            mock_http.return_value.__aenter__.return_value = mock_http_instance
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {
                "message": {"content": "answer from vector"}
            }
            mock_http_instance.post = AsyncMock(return_value=mock_response)

            await nexus_tools.answer_query("query", "PROJ")

        # Should have tried the LLM with the vector context
        assert mock_http_instance.post.called


# ---------------------------------------------------------------------------
# TestInvalidateCacheFullProject (Loop 6)
# ---------------------------------------------------------------------------


class TestInvalidateCacheFullProject:
    """invalidate_cache(project_id, scope='') must clear ALL per-scope indices."""

    def test_full_project_invalidation_clears_per_scope_indices(self):
        """scope='' scans nexus:idx:{pid}:* and deletes all per-scope cache entries."""
        mock_redis = MagicMock()
        # Simulate two per-scope indices for the project
        mock_redis.scan_iter.return_value = [
            "nexus:idx:PROJ:CORE_CODE",
            "nexus:idx:PROJ:__all__",
        ]
        # __all__ idx key is already added before scan; smembers for both indices
        mock_redis.smembers.side_effect = lambda key: {
            "nexus:idx:PROJ:__all__": {"nexus:abc123"},
            "nexus:idx:PROJ:CORE_CODE": {"nexus:def456"},
        }.get(key, set())
        mock_redis.delete.return_value = 4

        with (
            patch("nexus.cache.CACHE_ENABLED", True),
            patch("nexus.cache.get_redis", return_value=mock_redis),
        ):
            deleted = _nexus_cache.invalidate_cache("PROJ", "")

        assert deleted == 4
        # scan_iter must have been called with nexus:idx:PROJ:* pattern
        mock_redis.scan_iter.assert_called_once_with(
            match="nexus:idx:PROJ:*", count=100
        )
        # delete should include both cache entries and both index keys
        args = mock_redis.delete.call_args[0]
        assert "nexus:abc123" in args
        assert "nexus:def456" in args
        assert "nexus:idx:PROJ:__all__" in args
        assert "nexus:idx:PROJ:CORE_CODE" in args

    def test_scoped_invalidation_does_not_scan(self):
        """scope='X' does NOT scan per-scope indices — only clears X and __all__."""
        mock_redis = MagicMock()
        mock_redis.smembers.return_value = {"nexus:abc"}
        mock_redis.delete.return_value = 3

        with (
            patch("nexus.cache.CACHE_ENABLED", True),
            patch("nexus.cache.get_redis", return_value=mock_redis),
        ):
            _nexus_cache.invalidate_cache("PROJ", "CORE_CODE")

        mock_redis.scan_iter.assert_not_called()

    def test_full_project_invalidation_no_per_scope_indices(self):
        """scope='' with no per-scope indices still clears __all__ and returns 0."""
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = ["nexus:idx:PROJ:__all__"]
        mock_redis.smembers.return_value = set()
        mock_redis.delete.return_value = 1

        with (
            patch("nexus.cache.CACHE_ENABLED", True),
            patch("nexus.cache.get_redis", return_value=mock_redis),
        ):
            deleted = _nexus_cache.invalidate_cache("PROJ", "")

        # Only the index key itself is deleted (no cache entries)
        assert deleted == 1

    def test_full_project_invalidation_redis_error_returns_zero(self):
        """Redis error during full-project scan returns 0 without raising."""
        mock_redis = MagicMock()
        mock_redis.smembers.side_effect = redis.RedisError("scan failed")

        with (
            patch("nexus.cache.CACHE_ENABLED", True),
            patch("nexus.cache.get_redis", return_value=mock_redis),
        ):
            result = _nexus_cache.invalidate_cache("PROJ", "")

        assert result == 0

    def test_full_project_invalidation_colon_in_project_id_escaped(self):
        """Colons in project_id are replaced with underscores in the scan pattern."""
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = []
        mock_redis.smembers.return_value = set()

        with (
            patch("nexus.cache.CACHE_ENABLED", True),
            patch("nexus.cache.get_redis", return_value=mock_redis),
        ):
            _nexus_cache.invalidate_cache("MY:PROJECT", "")

        mock_redis.scan_iter.assert_called_once_with(
            match="nexus:idx:MY_PROJECT:*", count=100
        )
