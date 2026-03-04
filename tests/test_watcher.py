# Version: v1.3
"""
Tests for nexus.watcher — RAG sync daemon.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.sync import (
    _classify_file,
)
from nexus.watcher import (
    CoreDocEventHandler,
    _delete_from_rag,
    _sync_changed,
    _sync_deleted,
)

WORKSPACE = Path("/home/turiya/antigravity")


# ---------------------------------------------------------------------------
# _classify_file
# ---------------------------------------------------------------------------


class TestClassifyFile:
    def test_persona_claude_md(self):
        p = WORKSPACE / "CLAUDE.md"
        assert _classify_file(p, WORKSPACE) == ("AGENT", "PERSONA")

    def test_persona_rules_md(self):
        p = WORKSPACE / ".claude/rules/rules.md"
        assert _classify_file(p, WORKSPACE) == ("AGENT", "PERSONA")

    def test_persona_memory_md(self):
        p = WORKSPACE / "MEMORY.md"
        assert _classify_file(p, WORKSPACE) == ("AGENT", "PERSONA")

    def test_persona_mission_md(self):
        p = WORKSPACE / "mission.md"
        assert _classify_file(p, WORKSPACE) == ("AGENT", "PERSONA")

    def test_project_readme(self):
        p = WORKSPACE / "projects/gravity-claw/README.md"
        result = _classify_file(p, WORKSPACE)
        assert result == ("GRAVITY_CLAW", "CORE_DOCS")

    def test_project_memory(self):
        p = WORKSPACE / "projects/mcp-nexus-rag/MEMORY.md"
        result = _classify_file(p, WORKSPACE)
        assert result == ("MCP_NEXUS_RAG", "CORE_DOCS")

    def test_project_todo(self):
        p = WORKSPACE / "projects/web-scrapers/TODO.md"
        assert _classify_file(p, WORKSPACE) == ("WEB_SCRAPERS", "CORE_DOCS")

    def test_project_agents(self):
        p = WORKSPACE / "projects/mission-control/AGENTS.md"
        assert _classify_file(p, WORKSPACE) == ("MISSION_CONTROL", "CORE_DOCS")

    def test_unknown_project_uses_fallback(self):
        p = WORKSPACE / "projects/some-new-project/README.md"
        result = _classify_file(p, WORKSPACE)
        assert result == ("SOME_NEW_PROJECT", "CORE_DOCS")

    def test_agentic_trader_in_mappings(self):
        p = WORKSPACE / "projects/agentic-trader/README.md"
        result = _classify_file(p, WORKSPACE)
        assert result == ("AGENTIC_TRADER", "CORE_DOCS")

    def test_nested_project_file_not_tracked(self):
        # Nested README (depth > 1 under project dir) should not be tracked
        p = WORKSPACE / "projects/gravity-claw/src/README.md"
        assert _classify_file(p, WORKSPACE) is None

    def test_non_core_doc_ignored(self):
        p = WORKSPACE / "projects/gravity-claw/package.json"
        assert _classify_file(p, WORKSPACE) is None

    def test_outside_workspace_returns_none(self):
        p = Path("/tmp/CLAUDE.md")
        assert _classify_file(p, WORKSPACE) is None

    def test_source_file_not_tracked(self):
        p = WORKSPACE / "projects/mcp-nexus-rag/nexus/tools.py"
        assert _classify_file(p, WORKSPACE) is None

    def test_deleted_file_classified_by_path(self):
        # Classification works even if file doesn't exist on disk
        p = WORKSPACE / "projects/gravity-claw/MEMORY.md"
        assert _classify_file(p, WORKSPACE) == ("GRAVITY_CLAW", "CORE_DOCS")


# ---------------------------------------------------------------------------
# CoreDocEventHandler
# ---------------------------------------------------------------------------


class TestCoreDocEventHandler:
    def _handler(self):
        return CoreDocEventHandler(WORKSPACE)

    def _make_event(self, path: str, is_dir: bool = False):
        e = MagicMock()
        e.src_path = path
        e.is_directory = is_dir
        return e

    def test_modified_core_doc_queued(self):
        h = self._handler()
        e = self._make_event(str(WORKSPACE / "CLAUDE.md"))
        h.on_modified(e)
        changed, deleted = h.pop_ready(debounce=0.0)
        assert str(WORKSPACE / "CLAUDE.md") in changed
        assert deleted == []

    def test_modified_non_core_doc_ignored(self):
        h = self._handler()
        e = self._make_event(str(WORKSPACE / "projects/gravity-claw/src/index.ts"))
        h.on_modified(e)
        changed, deleted = h.pop_ready(debounce=0.0)
        assert changed == []

    def test_deleted_queued_separately(self):
        h = self._handler()
        e = self._make_event(str(WORKSPACE / "projects/gravity-claw/README.md"))
        h.on_deleted(e)
        changed, deleted = h.pop_ready(debounce=0.0)
        assert changed == []
        assert str(WORKSPACE / "projects/gravity-claw/README.md") in deleted

    def test_delete_overrides_pending_change(self):
        h = self._handler()
        path = str(WORKSPACE / "CLAUDE.md")
        mod_event = self._make_event(path)
        del_event = self._make_event(path)
        h.on_modified(mod_event)
        h.on_deleted(del_event)
        changed, deleted = h.pop_ready(debounce=0.0)
        assert path not in changed
        assert path in deleted

    def test_change_after_delete_overrides_delete(self):
        h = self._handler()
        path = str(WORKSPACE / "CLAUDE.md")
        del_event = self._make_event(path)
        mod_event = self._make_event(path)
        h.on_deleted(del_event)
        h.on_modified(mod_event)
        changed, deleted = h.pop_ready(debounce=0.0)
        assert path in changed
        assert path not in deleted

    def test_debounce_holds_back_recent_event(self):
        h = self._handler()
        path = str(WORKSPACE / "CLAUDE.md")
        e = self._make_event(path)
        h.on_modified(e)
        # With 60-second debounce, nothing should be ready immediately
        changed, deleted = h.pop_ready(debounce=60.0)
        assert changed == []

    def test_debounce_releases_after_window(self):
        h = self._handler()
        path = str(WORKSPACE / "CLAUDE.md")
        e = self._make_event(path)
        h.on_modified(e)
        # With 0-second debounce, should be ready immediately
        changed, deleted = h.pop_ready(debounce=0.0)
        assert path in changed

    def test_directory_events_ignored(self):
        h = self._handler()
        e = self._make_event(str(WORKSPACE / "projects/gravity-claw"), is_dir=True)
        h.on_modified(e)
        h.on_deleted(e)
        changed, deleted = h.pop_ready(debounce=0.0)
        assert changed == []
        assert deleted == []

    def test_moved_event_queues_delete_and_change(self):
        h = self._handler()
        src = str(WORKSPACE / "CLAUDE.md")
        dst = str(WORKSPACE / "projects/gravity-claw/README.md")
        e = MagicMock()
        e.src_path = src
        e.dest_path = dst
        e.is_directory = False
        h.on_moved(e)
        changed, deleted = h.pop_ready(debounce=0.0)
        assert src in deleted
        assert dst in changed

    def test_pop_clears_state(self):
        h = self._handler()
        path = str(WORKSPACE / "CLAUDE.md")
        e = self._make_event(path)
        h.on_modified(e)
        h.pop_ready(debounce=0.0)  # first call drains
        changed, deleted = h.pop_ready(debounce=0.0)  # second call is empty
        assert changed == []
        assert deleted == []


# ---------------------------------------------------------------------------
# _delete_from_rag
# ---------------------------------------------------------------------------


class TestDeleteFromRag:
    def test_calls_both_backends(self):
        with (
            patch("nexus.watcher.neo4j_backend") as mock_neo4j,
            patch("nexus.watcher.qdrant_backend") as mock_qdrant,
        ):
            _delete_from_rag("PROJ", "/some/path.md", "CORE_DOCS")
            mock_neo4j.delete_by_filepath.assert_called_once_with(
                "PROJ", "/some/path.md", "CORE_DOCS"
            )
            mock_qdrant.delete_by_filepath.assert_called_once_with(
                "PROJ", "/some/path.md", "CORE_DOCS"
            )

    def test_neo4j_error_does_not_raise(self):
        with (
            patch("nexus.watcher.neo4j_backend") as mock_neo4j,
            patch("nexus.watcher.qdrant_backend"),
        ):
            mock_neo4j.delete_by_filepath.side_effect = Exception("connection lost")
            # Should not raise
            _delete_from_rag("PROJ", "/some/path.md", "CORE_DOCS")

    def test_qdrant_error_does_not_raise(self):
        with (
            patch("nexus.watcher.neo4j_backend"),
            patch("nexus.watcher.qdrant_backend") as mock_qdrant,
        ):
            mock_qdrant.delete_by_filepath.side_effect = Exception("timeout")
            _delete_from_rag("PROJ", "/some/path.md", "CORE_DOCS")


# ---------------------------------------------------------------------------
# _sync_changed
# ---------------------------------------------------------------------------


class TestSyncChanged:
    @pytest.fixture
    def mock_ingest(self):
        with (
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
            patch("nexus.watcher.neo4j_backend"),
            patch("nexus.watcher.qdrant_backend"),
            patch("nexus.watcher.check_file_changed", return_value=True),
        ):
            yield

    async def test_skips_nonexistent_file(self, tmp_path):
        ghost = str(tmp_path / "CLAUDE.md")
        with patch("nexus.watcher.check_file_changed", return_value=True):
            # No exception — just logs warning
            await _sync_changed([ghost], WORKSPACE)

    async def test_skips_unclassified_file(self, tmp_path):
        f = tmp_path / "random.txt"
        f.write_text("hello")
        with patch("nexus.watcher.check_file_changed", return_value=True):
            await _sync_changed([str(f)], WORKSPACE)

    async def test_skips_unchanged_file(self, tmp_path):
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        f = workspace / "CLAUDE.md"
        f.write_text("no change")
        with (
            patch("nexus.watcher.check_file_changed", return_value=False),
            patch(
                "nexus.tools.ingest_graph_document", new_callable=AsyncMock
            ) as mock_g,
        ):
            await _sync_changed([str(f)], workspace)
            mock_g.assert_not_called()

    async def test_ingests_changed_persona_file(self, tmp_path):
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        f = workspace / "CLAUDE.md"
        f.write_text("updated content")
        with (
            patch("nexus.watcher.check_file_changed", return_value=True),
            patch("nexus.watcher.neo4j_backend"),
            patch("nexus.watcher.qdrant_backend"),
            patch(
                "nexus.tools.ingest_graph_document",
                new_callable=AsyncMock,
                return_value="Successfully ingested graph document",
            ) as mock_g,
            patch(
                "nexus.tools.ingest_vector_document",
                new_callable=AsyncMock,
                return_value="Successfully ingested vector document",
            ) as mock_v,
        ):
            await _sync_changed([str(f)], workspace)
            mock_g.assert_called_once()
            mock_v.assert_called_once()
            # Verify correct project_id and scope
            call_kwargs = mock_g.call_args.kwargs
            assert call_kwargs["project_id"] == "AGENT"
            assert call_kwargs["scope"] == "PERSONA"

    async def test_ingests_changed_project_file(self, tmp_path):
        workspace = tmp_path / "antigravity"
        proj = workspace / "projects" / "gravity-claw"
        proj.mkdir(parents=True)
        f = proj / "README.md"
        f.write_text("project docs")
        with (
            patch("nexus.watcher.check_file_changed", return_value=True),
            patch("nexus.watcher.neo4j_backend"),
            patch("nexus.watcher.qdrant_backend"),
            patch(
                "nexus.tools.ingest_graph_document",
                new_callable=AsyncMock,
                return_value="Successfully ingested graph document",
            ) as mock_g,
            patch(
                "nexus.tools.ingest_vector_document",
                new_callable=AsyncMock,
                return_value="Successfully ingested vector document",
            ),
        ):
            await _sync_changed([str(f)], workspace)
            call_kwargs = mock_g.call_args.kwargs
            assert call_kwargs["project_id"] == "GRAVITY_CLAW"
            assert call_kwargs["scope"] == "CORE_DOCS"

    async def test_deletes_old_chunks_before_ingest(self, tmp_path):
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        f = workspace / "CLAUDE.md"
        f.write_text("updated")
        with (
            patch("nexus.watcher.check_file_changed", return_value=True),
            patch("nexus.watcher.neo4j_backend") as mock_neo4j,
            patch("nexus.watcher.qdrant_backend") as mock_qdrant,
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
        ):
            await _sync_changed([str(f)], workspace)
            mock_neo4j.delete_by_filepath.assert_called_once()
            mock_qdrant.delete_by_filepath.assert_called_once()


# ---------------------------------------------------------------------------
# _sync_deleted
# ---------------------------------------------------------------------------


class TestSyncDeleted:
    async def test_deletes_from_both_stores(self, tmp_path):
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        f = workspace / "CLAUDE.md"  # doesn't need to exist for delete

        with (
            patch("nexus.watcher.neo4j_backend") as mock_neo4j,
            patch("nexus.watcher.qdrant_backend") as mock_qdrant,
        ):
            await _sync_deleted([str(f)], workspace)
            mock_neo4j.delete_by_filepath.assert_called_once_with(
                "AGENT", "CLAUDE.md", "PERSONA"
            )
            mock_qdrant.delete_by_filepath.assert_called_once_with(
                "AGENT", "CLAUDE.md", "PERSONA"
            )

    async def test_skips_unclassified_path(self, tmp_path):
        untracked = str(tmp_path / "random.txt")
        with (
            patch("nexus.watcher.neo4j_backend") as mock_neo4j,
            patch("nexus.watcher.qdrant_backend") as mock_qdrant,
        ):
            await _sync_deleted([untracked], tmp_path)
            mock_neo4j.delete_by_filepath.assert_not_called()
            mock_qdrant.delete_by_filepath.assert_not_called()

    async def test_deletes_project_core_doc(self, tmp_path):
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        f = workspace / "projects" / "mission-control" / "MEMORY.md"

        with (
            patch("nexus.watcher.neo4j_backend") as mock_neo4j,
            patch("nexus.watcher.qdrant_backend"),
            patch("nexus.watcher.cache_module"),
        ):
            await _sync_deleted([str(f)], workspace)
            mock_neo4j.delete_by_filepath.assert_called_once_with(
                "MISSION_CONTROL", "projects/mission-control/MEMORY.md", "CORE_DOCS"
            )

    async def test_cache_invalidated_after_delete(self, tmp_path):
        """_sync_deleted must invalidate cache so stale results are not served."""
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        f = workspace / "CLAUDE.md"

        with (
            patch("nexus.watcher.neo4j_backend"),
            patch("nexus.watcher.qdrant_backend"),
            patch("nexus.watcher.cache_module") as mock_cache,
        ):
            await _sync_deleted([str(f)], workspace)
        mock_cache.invalidate_cache.assert_called_once_with("AGENT", "PERSONA")

    async def test_cache_not_invalidated_for_unclassified_path(self, tmp_path):
        """Unclassified paths must not trigger cache invalidation."""
        untracked = str(tmp_path / "some_random_file.txt")
        with (
            patch("nexus.watcher.neo4j_backend"),
            patch("nexus.watcher.qdrant_backend"),
            patch("nexus.watcher.cache_module") as mock_cache,
        ):
            await _sync_deleted([untracked], tmp_path)
        mock_cache.invalidate_cache.assert_not_called()


# ---------------------------------------------------------------------------
# TestSyncChangedSuccessCheck (Loop 12 — Bug L12-4)
# ---------------------------------------------------------------------------


class TestSyncChangedSuccessCheck:
    """_sync_changed success check must use 'Error' not in result instead of
    'Successfully' in.  'Skipped: duplicate' is a valid non-error outcome
    (content already ingested by a concurrent call) and must NOT trigger a
    WARNING log.
    """

    async def test_skipped_duplicate_does_not_log_warning(self, tmp_path):
        """'Skipped: duplicate' must log INFO (success), not WARNING."""
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        f = workspace / "CLAUDE.md"
        f.write_text("content")

        skipped = "Skipped: duplicate content already exists."
        with (
            patch("nexus.watcher.neo4j_backend"),
            patch("nexus.watcher.qdrant_backend"),
            patch("nexus.watcher.cache_module"),
            patch("nexus.watcher.check_file_changed", return_value=True),
            patch("nexus.tools.ingest_graph_document", AsyncMock(return_value=skipped)),
            patch(
                "nexus.tools.ingest_vector_document", AsyncMock(return_value=skipped)
            ),
            patch("nexus.watcher.logger") as mock_logger,
        ):
            await _sync_changed([str(f)], workspace)

        # info logged for the synced file, warning must NOT have been logged
        mock_logger.info.assert_called()
        mock_logger.warning.assert_not_called()

    async def test_error_result_logs_warning(self, tmp_path):
        """'Error:' in either ingest result must trigger a WARNING log."""
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        f = workspace / "CLAUDE.md"
        f.write_text("content")

        with (
            patch("nexus.watcher.neo4j_backend"),
            patch("nexus.watcher.qdrant_backend"),
            patch("nexus.watcher.cache_module"),
            patch("nexus.watcher.check_file_changed", return_value=True),
            patch(
                "nexus.tools.ingest_graph_document",
                AsyncMock(return_value="Error: neo4j unavailable"),
            ),
            patch(
                "nexus.tools.ingest_vector_document",
                AsyncMock(return_value="Successfully ingested vector document"),
            ),
            patch("nexus.watcher.logger") as mock_logger,
        ):
            await _sync_changed([str(f)], workspace)

        mock_logger.warning.assert_called_once()
        assert "partial sync" in mock_logger.warning.call_args[0][0]

    async def test_both_successfully_logs_info_not_warning(self, tmp_path):
        """Full success path: both 'Successfully' results log INFO, no WARNING."""
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        f = workspace / "CLAUDE.md"
        f.write_text("content")

        with (
            patch("nexus.watcher.neo4j_backend"),
            patch("nexus.watcher.qdrant_backend"),
            patch("nexus.watcher.cache_module"),
            patch("nexus.watcher.check_file_changed", return_value=True),
            patch(
                "nexus.tools.ingest_graph_document",
                AsyncMock(return_value="Successfully ingested graph document"),
            ),
            patch(
                "nexus.tools.ingest_vector_document",
                AsyncMock(return_value="Successfully ingested vector document"),
            ),
            patch("nexus.watcher.logger") as mock_logger,
        ):
            await _sync_changed([str(f)], workspace)

        mock_logger.info.assert_called()
        mock_logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# TestSyncChangedCacheInvalidation (Loop 7)
# ---------------------------------------------------------------------------


class TestSyncChangedCacheInvalidation:
    """_sync_changed must invalidate cache right after _delete_from_rag,
    before ingest, so stale cached results aren't served if ingest fails."""

    async def test_cache_invalidated_before_ingest_on_ingest_failure(self, tmp_path):
        """If both ingest calls fail, cache is still invalidated after delete."""
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        f = workspace / "CLAUDE.md"
        f.write_text("some content")

        with (
            patch("nexus.watcher.neo4j_backend"),
            patch("nexus.watcher.qdrant_backend"),
            patch("nexus.watcher.cache_module") as mock_cache,
            patch("nexus.watcher.check_file_changed", return_value=True),
            patch(
                "nexus.tools.ingest_graph_document",
                AsyncMock(side_effect=RuntimeError("neo4j down")),
            ),
            patch(
                "nexus.tools.ingest_vector_document",
                AsyncMock(side_effect=RuntimeError("qdrant down")),
            ),
        ):
            await _sync_changed([str(f)], workspace)

        # Cache should be invalidated even though both ingests failed
        mock_cache.invalidate_cache.assert_called_with("AGENT", "PERSONA")

    async def test_cache_invalidated_before_ingest_on_ingest_success(self, tmp_path):
        """On successful ingest, cache is invalidated (at least once) after delete."""
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        f = workspace / "CLAUDE.md"
        f.write_text("updated content")

        with (
            patch("nexus.watcher.neo4j_backend"),
            patch("nexus.watcher.qdrant_backend"),
            patch("nexus.watcher.cache_module") as mock_cache,
            patch("nexus.watcher.check_file_changed", return_value=True),
            patch(
                "nexus.tools.ingest_graph_document",
                AsyncMock(return_value="Successfully ingested"),
            ),
            patch(
                "nexus.tools.ingest_vector_document",
                AsyncMock(return_value="Successfully ingested"),
            ),
        ):
            await _sync_changed([str(f)], workspace)

        # At minimum the pre-ingest invalidation must have fired
        assert mock_cache.invalidate_cache.call_count >= 1

    async def test_unchanged_file_does_not_invalidate_cache(self, tmp_path):
        """If check_file_changed returns False, cache must not be invalidated."""
        workspace = tmp_path / "antigravity"
        workspace.mkdir()
        f = workspace / "CLAUDE.md"
        f.write_text("same content")

        with (
            patch("nexus.watcher.neo4j_backend"),
            patch("nexus.watcher.qdrant_backend"),
            patch("nexus.watcher.cache_module") as mock_cache,
            patch("nexus.watcher.check_file_changed", return_value=False),
        ):
            await _sync_changed([str(f)], workspace)

        mock_cache.invalidate_cache.assert_not_called()
