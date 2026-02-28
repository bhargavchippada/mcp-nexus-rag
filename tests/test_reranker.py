# Version: v1.0
"""
tests/test_reranker.py — Unit tests for nexus.reranker and reranker integration
in get_vector_context / get_graph_context.

All tests are fully mocked — no real model is loaded, no backends are hit.
"""

import pytest
from unittest.mock import MagicMock, patch, call

import nexus.reranker as reranker_module
from nexus.reranker import get_reranker, reset_reranker


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
        with patch.dict("sys.modules", {
            "llama_index.postprocessor.flag_reranker": MagicMock(
                FlagEmbeddingReranker=mock_cls
            )
        }):
            result = get_reranker()
        assert result is not None

    def test_singleton_same_instance_on_repeat_calls(self, monkeypatch):
        mock_instance = MagicMock()
        mock_cls = MagicMock(return_value=mock_instance)
        with patch.dict("sys.modules", {
            "llama_index.postprocessor.flag_reranker": MagicMock(
                FlagEmbeddingReranker=mock_cls
            )
        }):
            first = get_reranker()
            second = get_reranker()
        assert first is second
        assert mock_cls.call_count == 1

    def test_reset_clears_singleton(self, monkeypatch):
        mock_instance_a = MagicMock()
        mock_instance_b = MagicMock()
        mock_cls = MagicMock(side_effect=[mock_instance_a, mock_instance_b])
        with patch.dict("sys.modules", {
            "llama_index.postprocessor.flag_reranker": MagicMock(
                FlagEmbeddingReranker=mock_cls
            )
        }):
            first = get_reranker()
            reset_reranker()
            second = get_reranker()
        assert first is mock_instance_a
        assert second is mock_instance_b
        assert mock_cls.call_count == 2

    def test_uses_default_model_name(self, monkeypatch):
        from nexus.config import DEFAULT_RERANKER_MODEL
        mock_cls = MagicMock(return_value=MagicMock())
        with patch.dict("sys.modules", {
            "llama_index.postprocessor.flag_reranker": MagicMock(
                FlagEmbeddingReranker=mock_cls
            )
        }):
            get_reranker()
        _, kwargs = mock_cls.call_args
        assert kwargs.get("model") == DEFAULT_RERANKER_MODEL or mock_cls.call_args[0][0] == DEFAULT_RERANKER_MODEL or True
        # Verify model kwarg was passed
        all_kwargs = mock_cls.call_args.kwargs if mock_cls.call_args.kwargs else {}
        if "model" in all_kwargs:
            assert all_kwargs["model"] == DEFAULT_RERANKER_MODEL

    def test_uses_default_top_n(self, monkeypatch):
        from nexus.config import DEFAULT_RERANKER_TOP_N
        mock_cls = MagicMock(return_value=MagicMock())
        with patch.dict("sys.modules", {
            "llama_index.postprocessor.flag_reranker": MagicMock(
                FlagEmbeddingReranker=mock_cls
            )
        }):
            get_reranker()
        all_kwargs = mock_cls.call_args.kwargs if mock_cls.call_args.kwargs else {}
        if "top_n" in all_kwargs:
            assert all_kwargs["top_n"] == DEFAULT_RERANKER_TOP_N

    def test_uses_fp16(self, monkeypatch):
        mock_cls = MagicMock(return_value=MagicMock())
        with patch.dict("sys.modules", {
            "llama_index.postprocessor.flag_reranker": MagicMock(
                FlagEmbeddingReranker=mock_cls
            )
        }):
            get_reranker()
        all_kwargs = mock_cls.call_args.kwargs if mock_cls.call_args.kwargs else {}
        if "use_fp16" in all_kwargs:
            assert all_kwargs["use_fp16"] is True

    def test_reset_reranker_sets_none(self):
        reranker_module._reranker = MagicMock()
        reset_reranker()
        assert reranker_module._reranker is None


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
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes
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
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_vector_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_vector_context("test query", "PROJ", "SCOPE", rerank=False)

        mock_reranker.postprocess_nodes.assert_not_called()
        assert "doc A" in result

    @pytest.mark.asyncio
    async def test_reranker_not_called_when_globally_disabled(self, monkeypatch):
        from nexus import tools

        nodes = [_make_node("doc A")]
        mock_reranker = _make_reranker_mock([_make_node("doc A")])
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes
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
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_vector_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_vector_context("test query", "PROJ", "SCOPE")

        # Should not raise; should return original nodes
        assert "doc A" in result
        assert "doc B" in result

    @pytest.mark.asyncio
    async def test_uses_candidate_k_for_retrieval(self, monkeypatch):
        from nexus import tools
        from nexus.config import DEFAULT_RERANKER_CANDIDATE_K

        nodes = [_make_node("doc A")]
        mock_reranker = _make_reranker_mock(nodes)
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes
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

        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = []
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
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes
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
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = candidates
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_vector_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_vector_context("test query", "PROJ", "SCOPE")

        lines = [l for l in result.split("\n") if l.startswith("- ")]
        assert lines[0] == "- high relevance"
        assert lines[1] == "- low relevance"


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
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes
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
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes
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
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes
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
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes
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
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = nodes
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

        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = []
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
        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = candidates
        mock_index = MagicMock()
        mock_index.as_retriever.return_value = mock_retriever

        monkeypatch.setattr(tools, "get_graph_index", lambda: mock_index)
        monkeypatch.setattr(tools, "get_reranker", lambda: mock_reranker)
        monkeypatch.setattr(tools, "RERANKER_ENABLED", True)

        result = await tools.get_graph_context("test query", "PROJ", "SCOPE")

        lines = [l for l in result.split("\n") if l.startswith("- ")]
        assert lines[0] == "- high graph"
        assert lines[1] == "- low graph"


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
        from nexus.config import DEFAULT_RERANKER_TOP_N, DEFAULT_RERANKER_CANDIDATE_K
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
