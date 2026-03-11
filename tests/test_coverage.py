# Version: v2.0
"""
tests/test_coverage.py — Targeted tests to close coverage gaps.

v2.0: Migrated from Neo4j/Qdrant to Memgraph/pgvector backends.

No live services required — all backends are mocked.
asyncio_mode=auto (pyproject.toml) — no @pytest.mark.asyncio needed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from nexus import indexes as nexus_indexes
from nexus import tools as nexus_tools
from nexus.backends import memgraph as graph_backend
from nexus.backends import pgvector as vector_backend
from tests.conftest import make_graph_driver

# ---------------------------------------------------------------------------
# nexus.backends.memgraph — get_scopes_for_project
# ---------------------------------------------------------------------------


class TestGetScopesForProject:
    """Covers memgraph.py get_scopes_for_project (happy path + error branch)."""

    def test_returns_scopes_for_project(self):
        records = [{"value": "CORE_CODE"}, {"value": "SYSTEM_LOGS"}]
        mock_driver, _ = make_graph_driver(records)
        with patch.object(graph_backend, "get_driver", return_value=mock_driver):
            result = graph_backend.get_scopes_for_project("TRADING_BOT")
        assert set(result) == {"CORE_CODE", "SYSTEM_LOGS"}

    def test_returns_empty_list_on_connection_error(self):
        with patch.object(
            graph_backend, "get_driver", side_effect=Exception("bolt closed")
        ):
            result = graph_backend.get_scopes_for_project("ANY_PROJECT")
        assert result == []

    def test_query_filters_by_project_id(self):
        """Verifies the Cypher uses a project_id parameter (not a literal)."""
        mock_driver, mock_session = make_graph_driver([])
        with patch.object(graph_backend, "get_driver", return_value=mock_driver):
            graph_backend.get_scopes_for_project("MY_PROJECT")
        _, kwargs = mock_session.run.call_args
        assert kwargs.get("project_id") == "MY_PROJECT"

    def test_returns_empty_when_no_scopes(self):
        mock_driver, _ = make_graph_driver([])
        with patch.object(graph_backend, "get_driver", return_value=mock_driver):
            result = graph_backend.get_scopes_for_project("EMPTY_PROJECT")
        assert result == []


# ---------------------------------------------------------------------------
# nexus.indexes — get_vector_index body
# ---------------------------------------------------------------------------


class TestGetVectorIndex:
    """Covers nexus/indexes.py — ensure vector store wires correctly."""

    def test_returns_vector_store_index(self):
        mock_store = MagicMock()
        mock_index = MagicMock()

        with (
            patch.object(nexus_indexes, "setup_settings"),
            patch("nexus.indexes.PGVectorStore") as mock_pgv_cls,
            patch(
                "nexus.indexes.VectorStoreIndex.from_vector_store",
                return_value=mock_index,
            ) as mock_factory,
        ):
            mock_pgv_cls.from_params.return_value = mock_store
            # Reset cache before test
            nexus_indexes._vector_index_cache = None
            result = nexus_indexes.get_vector_index()

        mock_factory.assert_called_once_with(vector_store=mock_store)
        assert result is mock_index


# ---------------------------------------------------------------------------
# nexus.tools — get_graph_context with results (hit path)
# ---------------------------------------------------------------------------


class TestGetGraphContextWithResults:
    def _mock_index_with_content(self, content: str):
        node = MagicMock()
        node.node.get_content.return_value = content
        node.score = 0.95
        mock_retriever = MagicMock()
        mock_retriever.aretrieve = AsyncMock(return_value=[node])
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever
        return mock_index

    async def test_get_graph_context_with_results_contains_content(self):
        index = self._mock_index_with_content("entity relationship data")
        with patch("nexus.tools.get_graph_index", return_value=index):
            result = await nexus_tools.get_graph_context("query", "PROJ", "SCOPE")
        assert "entity relationship data" in result
        assert "Graph Context retrieved" in result

    async def test_get_graph_context_includes_project_and_scope(self):
        index = self._mock_index_with_content("data")
        with patch("nexus.tools.get_graph_index", return_value=index):
            result = await nexus_tools.get_graph_context(
                "query", "MY_PROJECT", "MY_SCOPE"
            )
        assert "MY_PROJECT" in result
        assert "MY_SCOPE" in result

    async def test_get_graph_context_multiple_nodes_joined(self):
        """Multiple retrieved nodes should each appear as a bullet line."""
        contents = ["node A content", "node B content", "node C content"]
        nodes = []
        for i, c in enumerate(contents):
            n = MagicMock()
            n.node.get_content.return_value = c
            n.score = 0.9 - i * 0.1
            nodes.append(n)
        mock_retriever = MagicMock()
        mock_retriever.aretrieve = AsyncMock(return_value=nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever
        with patch("nexus.tools.get_graph_index", return_value=mock_index):
            result = await nexus_tools.get_graph_context("query", "PROJ", "SCOPE")
        for c in contents:
            assert c in result


# ---------------------------------------------------------------------------
# nexus.tools — ingest_graph_document metadata integrity
# ---------------------------------------------------------------------------


class TestIngestGraphMetadata:
    async def test_graph_doc_has_all_metadata_fields(self):
        mock_index = MagicMock()
        with (
            patch("nexus.tools.content_hash", return_value="GRAPHHASH99"),
            patch.object(graph_backend, "is_duplicate", return_value=False),
            patch("nexus.tools.get_graph_index", return_value=mock_index),
        ):
            await nexus_tools.ingest_graph_document(
                "text", "PROJ", "SCOPE", "test_source"
            )
        doc = mock_index.insert.call_args[0][0]
        assert doc.metadata["project_id"] == "PROJ"
        assert doc.metadata["tenant_scope"] == "SCOPE"
        assert doc.metadata["source"] == "test_source"
        assert doc.metadata["content_hash"] == "GRAPHHASH99"

    async def test_vector_doc_source_identifier_stored(self):
        mock_index = MagicMock()
        with (
            patch("nexus.tools.content_hash", return_value="VH"),
            patch.object(vector_backend, "is_duplicate", return_value=False),
            patch("nexus.tools.get_vector_index", return_value=mock_index),
        ):
            await nexus_tools.ingest_vector_document(
                "text", "PROJ", "SCOPE", "my_source"
            )
        doc = mock_index.insert.call_args[0][0]
        assert doc.metadata["source"] == "my_source"


# ---------------------------------------------------------------------------
# nexus.tools — _validate_ingest_inputs edge cases
# ---------------------------------------------------------------------------


class TestValidateIngestInputsEdgeCases:
    async def test_newline_only_text_is_rejected(self):
        result = await nexus_tools.ingest_vector_document("\n\n\n", "PROJ", "SCOPE")
        assert "Error" in result

    async def test_tab_only_text_is_rejected(self):
        result = await nexus_tools.ingest_vector_document("\t\t", "PROJ", "SCOPE")
        assert "Error" in result

    async def test_whitespace_scope_rejected_graph(self):
        result = await nexus_tools.ingest_graph_document("text", "PROJ", "   ")
        assert "Error" in result


# ---------------------------------------------------------------------------
# nexus.indexes — inner double-checked lock guard
# ---------------------------------------------------------------------------


class TestSetupSettingsInnerGuard:
    def test_inner_guard_hits_early_return(self):
        import threading

        original = nexus_indexes._settings_initialized

        t_a_has_lock = threading.Event()
        t_a_release = threading.Event()
        errors = []

        def thread_a():
            with nexus_indexes._settings_lock:
                t_a_has_lock.set()
                t_a_release.wait(timeout=3)
                nexus_indexes._settings_initialized = True

        def thread_b():
            try:
                with (
                    patch("nexus.indexes.Ollama") as mock_llm,
                    patch("nexus.indexes.OllamaEmbedding") as mock_embed,
                    patch("nexus.indexes.SentenceSplitter"),
                ):
                    nexus_indexes.setup_settings()
                    assert mock_llm.call_count == 0, (
                        "Ollama was called despite inner guard"
                    )
                    assert mock_embed.call_count == 0, (
                        "OllamaEmbedding was called despite inner guard"
                    )
            except Exception as exc:
                errors.append(exc)

        try:
            nexus_indexes._settings_initialized = False

            ta = threading.Thread(target=thread_a, daemon=True)
            tb = threading.Thread(target=thread_b, daemon=True)

            ta.start()
            t_a_has_lock.wait(timeout=3)

            tb.start()
            import time

            time.sleep(0.05)

            t_a_release.set()
            ta.join(timeout=3)
            tb.join(timeout=3)

        finally:
            nexus_indexes._settings_initialized = original

        assert not errors, f"thread_b raised: {errors[0]}"


# ---------------------------------------------------------------------------
# server — __main__ guard
# ---------------------------------------------------------------------------


class TestServerMainGuard:
    def test_main_guard_calls_main(self):
        import runpy

        import nexus.config as nexus_config

        with patch.object(nexus_config.mcp, "run"):
            runpy.run_path(
                "/home/turiya/antigravity/projects/mcp-nexus-rag/server.py",
                run_name="__main__",
            )
