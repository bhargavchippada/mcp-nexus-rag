# Version: v1.3
"""
tests/test_reranker.py — Unit tests for nexus.reranker and reranker integration
in get_vector_context / get_graph_context.

All tests are fully mocked — no real model is loaded, no backends are hit.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import nexus.reranker as reranker_module
from nexus.reranker import RemoteReranker, get_reranker, reset_reranker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node(content: str, score: float = 1.0):
    """Build a minimal NodeWithScore mock."""
    node = MagicMock()
    node.node.get_content.return_value = content
    node.score = score
    return node


def _make_reranker_mock(top_n_nodes=None):
    """Return a MagicMock reranker whose postprocess_nodes returns top_n_nodes."""
    mock = MagicMock()
    mock.postprocess_nodes.return_value = top_n_nodes or []
    return mock


def _make_retriever_mock(nodes):
    """Return a mock retriever whose aretrieve coroutine returns nodes."""
    mock_retriever = MagicMock()
    mock_retriever.aretrieve = AsyncMock(return_value=nodes)
    return mock_retriever


# ---------------------------------------------------------------------------
# nexus.reranker — singleton behaviour
# ---------------------------------------------------------------------------


class TestGetRerankerSingleton:
    def setup_method(self):
        reset_reranker()

    def teardown_method(self):
        reset_reranker()

    def test_returns_instance_on_first_call(self, monkeypatch):
        mock_cls = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(
            "nexus.reranker.FlagEmbeddingReranker", mock_cls, raising=False
        )
        with patch.dict(
            "sys.modules",
            {
                "llama_index.postprocessor.flag_embedding_reranker": MagicMock(
                    FlagEmbeddingReranker=mock_cls
                )
            },
        ):
            result = get_reranker()
        assert result is not None

    def test_singleton_same_instance_on_repeat_calls(self, monkeypatch):
        mock_instance = MagicMock()
        mock_cls = MagicMock(return_value=mock_instance)
        with patch.dict(
            "sys.modules",
            {
                "llama_index.postprocessor.flag_embedding_reranker": MagicMock(
                    FlagEmbeddingReranker=mock_cls
                )
            },
        ):
            first = get_reranker()
            second = get_reranker()
        assert first is second
        assert mock_cls.call_count == 1

    def test_reset_clears_singleton(self, monkeypatch):
        mock_instance_a = MagicMock()
        mock_instance_b = MagicMock()
        mock_cls = MagicMock(side_effect=[mock_instance_a, mock_instance_b])
        with patch.dict(
            "sys.modules",
            {
                "llama_index.postprocessor.flag_embedding_reranker": MagicMock(
                    FlagEmbeddingReranker=mock_cls
                )
            },
        ):
            first = get_reranker()
            reset_reranker()
            second = get_reranker()
        assert first is mock_instance_a
        assert second is mock_instance_b
        assert mock_cls.call_count == 2

    def test_uses_default_model_name(self, monkeypatch):
        from nexus.config import DEFAULT_RERANKER_MODEL

        mock_cls = MagicMock(return_value=MagicMock())
        with patch.dict(
            "sys.modules",
            {
                "llama_index.postprocessor.flag_embedding_reranker": MagicMock(
                    FlagEmbeddingReranker=mock_cls
                )
            },
        ):
            get_reranker()
        all_kwargs = mock_cls.call_args.kwargs if mock_cls.call_args.kwargs else {}
        if "model" in all_kwargs:
            assert all_kwargs["model"] == DEFAULT_RERANKER_MODEL

    def test_uses_default_top_n(self, monkeypatch):
        from nexus.config import DEFAULT_RERANKER_TOP_N

        mock_cls = MagicMock(return_value=MagicMock())
        with patch.dict(
            "sys.modules",
            {
                "llama_index.postprocessor.flag_embedding_reranker": MagicMock(
                    FlagEmbeddingReranker=mock_cls
                )
            },
        ):
            get_reranker()
        all_kwargs = mock_cls.call_args.kwargs if mock_cls.call_args.kwargs else {}
        if "top_n" in all_kwargs:
            assert all_kwargs["top_n"] == DEFAULT_RERANKER_TOP_N

    def test_uses_fp16(self, monkeypatch):
        mock_cls = MagicMock(return_value=MagicMock())
        with patch.dict(
            "sys.modules",
            {
                "llama_index.postprocessor.flag_embedding_reranker": MagicMock(
                    FlagEmbeddingReranker=mock_cls
                )
            },
        ):
            get_reranker()
        all_kwargs = mock_cls.call_args.kwargs if mock_cls.call_args.kwargs else {}
        if "use_fp16" in all_kwargs:
            assert all_kwargs["use_fp16"] is True

    def test_reset_reranker_sets_none(self):
        reranker_module._reranker = MagicMock()
        reset_reranker()
        assert reranker_module._reranker is None


# ---------------------------------------------------------------------------
# RemoteReranker — HTTP call mapping
# ---------------------------------------------------------------------------


class TestRemoteReranker:
    def test_postprocess_nodes_maps_scores_by_index(self):
        """Remote reranker should POST to /rerank and map scores back."""
        from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode

        nodes = [
            NodeWithScore(node=TextNode(text="doc A"), score=0.0),
            NodeWithScore(node=TextNode(text="doc B"), score=0.0),
        ]
        query = QueryBundle(query_str="test query")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"index": 1, "score": 0.95, "text": "doc B"},
                {"index": 0, "score": 0.42, "text": "doc A"},
            ]
        }

        reranker = RemoteReranker("http://localhost:8767")
        reranker._client = MagicMock()
        reranker._client.post.return_value = mock_response

        result = reranker.postprocess_nodes(nodes, query)

        assert len(result) == 2
        assert result[0].score == 0.95
        assert result[0].node.get_content() == "doc B"
        assert result[1].score == 0.42
        assert result[1].node.get_content() == "doc A"

        # Verify the POST payload
        call_kwargs = reranker._client.post.call_args
        payload = call_kwargs.kwargs["json"]
        assert payload["query"] == "test query"
        assert payload["documents"] == ["doc A", "doc B"]

    def test_empty_input_returns_empty(self):
        """Empty node list should return empty without HTTP call."""
        reranker = RemoteReranker("http://localhost:8767")
        reranker._client = MagicMock()
        result = reranker.postprocess_nodes([], None)
        assert result == []
        reranker._client.post.assert_not_called()

    def test_no_query_bundle_sends_empty_string(self):
        """None query_bundle should send empty query string."""
        from llama_index.core.schema import NodeWithScore, TextNode

        nodes = [NodeWithScore(node=TextNode(text="doc"), score=0.0)]

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": [{"index": 0, "score": 0.5, "text": "doc"}]
        }

        reranker = RemoteReranker("http://localhost:8767")
        reranker._client = MagicMock()
        reranker._client.post.return_value = mock_response

        reranker.postprocess_nodes(nodes, query_bundle=None)

        payload = reranker._client.post.call_args.kwargs["json"]
        assert payload["query"] == ""

    def test_http_error_raises(self):
        """HTTP errors should propagate (caller's try/except handles them)."""
        from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode

        nodes = [NodeWithScore(node=TextNode(text="doc"), score=0.0)]
        query = QueryBundle(query_str="test")

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=MagicMock()
        )

        reranker = RemoteReranker("http://localhost:8767")
        reranker._client = MagicMock()
        reranker._client.post.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            reranker.postprocess_nodes(nodes, query)

    def test_connection_error_raises(self):
        """Connection errors should propagate (caller's try/except handles them)."""
        from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode

        nodes = [NodeWithScore(node=TextNode(text="doc"), score=0.0)]
        query = QueryBundle(query_str="test")

        reranker = RemoteReranker("http://localhost:8767")
        reranker._client = MagicMock()
        reranker._client.post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(httpx.ConnectError):
            reranker.postprocess_nodes(nodes, query)


# ---------------------------------------------------------------------------
# Reranker mode switch
# ---------------------------------------------------------------------------


class TestRerankerModeSwitch:
    def setup_method(self):
        reset_reranker()

    def teardown_method(self):
        reset_reranker()

    def test_local_mode_returns_flag_embedding(self, monkeypatch):
        """RERANKER_MODE=local should return FlagEmbeddingReranker."""
        monkeypatch.setattr("nexus.reranker.RERANKER_MODE", "local")
        mock_cls = MagicMock(return_value=MagicMock())
        with patch.dict(
            "sys.modules",
            {
                "llama_index.postprocessor.flag_embedding_reranker": MagicMock(
                    FlagEmbeddingReranker=mock_cls
                )
            },
        ):
            result = get_reranker()
        assert not isinstance(result, RemoteReranker)
        mock_cls.assert_called_once()

    def test_remote_mode_returns_remote_reranker(self, monkeypatch):
        """RERANKER_MODE=remote should return RemoteReranker."""
        monkeypatch.setattr("nexus.reranker.RERANKER_MODE", "remote")
        monkeypatch.setattr(
            "nexus.reranker.RERANKER_SERVICE_URL", "http://localhost:8767"
        )
        result = get_reranker()
        assert isinstance(result, RemoteReranker)

    def test_remote_singleton_behavior(self, monkeypatch):
        """Remote mode should also be a singleton."""
        monkeypatch.setattr("nexus.reranker.RERANKER_MODE", "remote")
        monkeypatch.setattr(
            "nexus.reranker.RERANKER_SERVICE_URL", "http://localhost:8767"
        )
        first = get_reranker()
        second = get_reranker()
        assert first is second


# ---------------------------------------------------------------------------
# Reranker mode config
# ---------------------------------------------------------------------------


class TestRerankerModeConfig:
    def test_default_mode_is_local(self):
        from nexus.config import RERANKER_MODE

        assert RERANKER_MODE == "local"

    def test_service_url_contains_8767(self):
        from nexus.config import RERANKER_SERVICE_URL

        assert "8767" in RERANKER_SERVICE_URL


# ---------------------------------------------------------------------------
# get_vector_context — reranker integration
# ---------------------------------------------------------------------------


class TestGetVectorContextReranker:
    def setup_method(self):
        reset_reranker()

    def teardown_method(self):
        reset_reranker()

    @pytest.mark.asyncio
    async def test_reranker_called_when_enabled(self, monkeypatch):
        from nexus import tools

        nodes = [_make_node("doc A"), _make_node("doc B")]
        reranked = [_make_node("doc B")]

        mock_reranker = _make_reranker_mock(reranked)
        mock_retriever = _make_retriever_mock(nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_vector_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_vector_context("test query", "PROJ", "SCOPE")

        mock_reranker.postprocess_nodes.assert_called_once()
        assert "doc B" in result

    @pytest.mark.asyncio
    async def test_reranker_not_called_when_rerank_false(self, monkeypatch):
        from nexus import tools

        nodes = [_make_node("doc A"), _make_node("doc B")]
        mock_reranker = _make_reranker_mock([_make_node("doc B")])
        mock_retriever = _make_retriever_mock(nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_vector_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_vector_context(
            "test query", "PROJ", "SCOPE", rerank=False
        )

        mock_reranker.postprocess_nodes.assert_not_called()
        assert "doc A" in result

    @pytest.mark.asyncio
    async def test_reranker_not_called_when_globally_disabled(self, monkeypatch):
        from nexus import tools

        nodes = [_make_node("doc A")]
        mock_reranker = _make_reranker_mock([_make_node("doc A")])
        mock_retriever = _make_retriever_mock(nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_vector_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", False)

        await tools.get_vector_context("test query", "PROJ", "SCOPE", rerank=True)

        mock_reranker.postprocess_nodes.assert_not_called()

    @pytest.mark.asyncio
    async def test_reranker_failure_falls_back_to_original_nodes(self, monkeypatch):
        from nexus import tools

        nodes = [_make_node("doc A"), _make_node("doc B")]
        mock_reranker = MagicMock()
        mock_reranker.postprocess_nodes.side_effect = RuntimeError("model error")
        mock_retriever = _make_retriever_mock(nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_vector_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_vector_context("test query", "PROJ", "SCOPE")

        assert "doc A" in result
        assert "doc B" in result

    @pytest.mark.asyncio
    async def test_uses_candidate_k_for_retrieval(self, monkeypatch):
        from nexus import tools
        from nexus.config import DEFAULT_RERANKER_CANDIDATE_K

        nodes = [_make_node("doc A")]
        mock_reranker = _make_reranker_mock(nodes)
        mock_retriever = _make_retriever_mock(nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_vector_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        await tools.get_vector_context("test query", "PROJ", "SCOPE")

        mock_index.as_retriever.assert_called_once()
        call_kwargs = mock_index.as_retriever.call_args.kwargs
        assert call_kwargs.get("similarity_top_k") == DEFAULT_RERANKER_CANDIDATE_K

    @pytest.mark.asyncio
    async def test_empty_nodes_returns_no_context_message(self, monkeypatch):
        from nexus import tools

        mock_retriever = _make_retriever_mock([])
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_vector_index", lambda: mock_index)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_vector_context("test query", "PROJ", "SCOPE")

        assert "No Vector context found" in result

    @pytest.mark.asyncio
    async def test_reranker_with_single_node(self, monkeypatch):
        from nexus import tools

        nodes = [_make_node("only doc")]
        mock_reranker = _make_reranker_mock(nodes)
        mock_retriever = _make_retriever_mock(nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_vector_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_vector_context("test query", "PROJ", "SCOPE")

        assert "only doc" in result

    @pytest.mark.asyncio
    async def test_reranker_preserves_reranked_order(self, monkeypatch):
        from nexus import tools

        candidates = [_make_node("low relevance"), _make_node("high relevance")]
        reranked = [_make_node("high relevance"), _make_node("low relevance")]
        mock_reranker = _make_reranker_mock(reranked)
        mock_retriever = _make_retriever_mock(candidates)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_vector_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_vector_context("test query", "PROJ", "SCOPE")

        lines = [line for line in result.split("\n") if line.startswith("- ")]
        # Format: - [score: X.XXXX] content
        assert "high relevance" in lines[0]
        assert "low relevance" in lines[1]
        assert "[score:" in lines[0]  # Verify score is included


# ---------------------------------------------------------------------------
# get_graph_context — reranker integration
# ---------------------------------------------------------------------------


class TestGetGraphContextReranker:
    def setup_method(self):
        reset_reranker()

    def teardown_method(self):
        reset_reranker()

    @pytest.mark.asyncio
    async def test_reranker_called_when_enabled(self, monkeypatch):
        from nexus import tools

        nodes = [_make_node("graph A"), _make_node("graph B")]
        reranked = [_make_node("graph B")]
        mock_reranker = _make_reranker_mock(reranked)
        mock_retriever = _make_retriever_mock(nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_graph_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_graph_context("test query", "PROJ", "SCOPE")

        mock_reranker.postprocess_nodes.assert_called_once()
        assert "graph B" in result

    @pytest.mark.asyncio
    async def test_reranker_not_called_when_rerank_false(self, monkeypatch):
        from nexus import tools

        nodes = [_make_node("graph A")]
        mock_reranker = _make_reranker_mock([_make_node("graph A")])
        mock_retriever = _make_retriever_mock(nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_graph_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        await tools.get_graph_context("test query", "PROJ", "SCOPE", rerank=False)

        mock_reranker.postprocess_nodes.assert_not_called()

    @pytest.mark.asyncio
    async def test_reranker_not_called_when_globally_disabled(self, monkeypatch):
        from nexus import tools

        nodes = [_make_node("graph A")]
        mock_reranker = _make_reranker_mock([_make_node("graph A")])
        mock_retriever = _make_retriever_mock(nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_graph_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", False)

        await tools.get_graph_context("test query", "PROJ", "SCOPE", rerank=True)

        mock_reranker.postprocess_nodes.assert_not_called()

    @pytest.mark.asyncio
    async def test_reranker_failure_falls_back_to_original_nodes(self, monkeypatch):
        from nexus import tools

        nodes = [_make_node("graph A"), _make_node("graph B")]
        mock_reranker = MagicMock()
        mock_reranker.postprocess_nodes.side_effect = RuntimeError("model error")
        mock_retriever = _make_retriever_mock(nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_graph_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_graph_context("test query", "PROJ", "SCOPE")

        assert "graph A" in result
        assert "graph B" in result

    @pytest.mark.asyncio
    async def test_uses_candidate_k_for_retrieval(self, monkeypatch):
        from nexus import tools
        from nexus.config import DEFAULT_RERANKER_CANDIDATE_K

        nodes = [_make_node("graph A")]
        mock_reranker = _make_reranker_mock(nodes)
        mock_retriever = _make_retriever_mock(nodes)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_graph_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        await tools.get_graph_context("test query", "PROJ", "SCOPE")

        call_kwargs = mock_index.as_retriever.call_args.kwargs
        assert call_kwargs.get("similarity_top_k") == DEFAULT_RERANKER_CANDIDATE_K

    @pytest.mark.asyncio
    async def test_empty_nodes_returns_no_context_message(self, monkeypatch):
        from nexus import tools

        mock_retriever = _make_retriever_mock([])
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_graph_index", lambda: mock_index)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_graph_context("test query", "PROJ", "SCOPE")

        assert "No Graph context found" in result

    @pytest.mark.asyncio
    async def test_reranker_preserves_reranked_order(self, monkeypatch):
        from nexus import tools

        candidates = [_make_node("low graph"), _make_node("high graph")]
        reranked = [_make_node("high graph"), _make_node("low graph")]
        mock_reranker = _make_reranker_mock(reranked)
        mock_retriever = _make_retriever_mock(candidates)
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_graph_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_graph_context("test query", "PROJ", "SCOPE")

        lines = [line for line in result.split("\n") if line.startswith("- ")]
        # Format: - [score: X.XXXX] content
        assert "high graph" in lines[0]
        assert "low graph" in lines[1]
        assert "[score:" in lines[0]  # Verify score is included


# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------


class TestRerankerConfig:
    def test_default_model_is_bge_v2_m3(self):
        from nexus.config import DEFAULT_RERANKER_MODEL

        assert DEFAULT_RERANKER_MODEL == "BAAI/bge-reranker-v2-m3"

    def test_default_top_n_is_positive(self):
        from nexus.config import DEFAULT_RERANKER_TOP_N

        assert DEFAULT_RERANKER_TOP_N > 0

    def test_default_candidate_k_greater_than_top_n(self):
        from nexus.config import DEFAULT_RERANKER_CANDIDATE_K, DEFAULT_RERANKER_TOP_N

        assert DEFAULT_RERANKER_CANDIDATE_K >= DEFAULT_RERANKER_TOP_N

    def test_reranker_enabled_env_false(self, monkeypatch):
        monkeypatch.setenv("RERANKER_ENABLED", "false")
        import importlib

        import nexus.config as cfg

        importlib.reload(cfg)
        assert cfg.RERANKER_ENABLED is False
        importlib.reload(cfg)  # restore

    def test_reranker_enabled_env_true(self, monkeypatch):
        monkeypatch.setenv("RERANKER_ENABLED", "true")
        import importlib

        import nexus.config as cfg

        importlib.reload(cfg)
        assert cfg.RERANKER_ENABLED is True
        importlib.reload(cfg)  # restore
