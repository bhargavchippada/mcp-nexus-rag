# Version: v3.0
import asyncio
import shutil
from pathlib import Path

import pytest

from nexus import indexes as nexus_indexes
from nexus.backends import memgraph as graph_backend
from nexus.backends import pgvector as vector_backend
from nexus.tools import (
    ingest_graph_document,
    ingest_project_directory,
    sync_deleted_files,
)


@pytest.fixture(autouse=True)
def reset_index_caches():
    """Reset index caches before each test to ensure clean state."""
    nexus_indexes._graph_index_cache = None
    nexus_indexes._vector_index_cache = None
    nexus_indexes._settings_initialized = False
    yield
    # Cleanup after test
    nexus_indexes._graph_index_cache = None
    nexus_indexes._vector_index_cache = None
    nexus_indexes._settings_initialized = False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_directory_ingestion_logic():
    """Verify that ingest_project_directory correctly filters files and ingests them."""
    test_dir = Path("/tmp/test_nexus_logic")
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir(parents=True)

    # Files to ingest
    (test_dir / "valid.py").write_text("print('test')")
    (test_dir / "useful.md").write_text("# Documentation")

    # Ignored by default or by .gitignore
    (test_dir / "ignored.tmp").write_text("ignore me")

    # Custom .gitignore
    (test_dir / ".gitignore").write_text("*.tmp\nnode_modules/")
    node_modules = test_dir / "node_modules"
    node_modules.mkdir()
    (node_modules / "pkg.js").write_text("console.log('pkg')")

    project_id = "LOGIC_TEST"
    scope = "UI_TEST"

    try:
        # Run ingestion
        result = await ingest_project_directory(
            directory_path=str(test_dir),
            project_id=project_id,
            scope=scope,
        )
        print(f"Ingestion result: {result}")

        # Give it a tiny bit of time for safety (though shouldn't be needed)
        await asyncio.sleep(1)

        # Verify Memgraph has the paths
        paths = graph_backend.get_all_filepaths(project_id, scope)
        print(f"Stored paths: {paths}")

        assert "valid.py" in paths
        assert "useful.md" in paths
        assert "ignored.tmp" not in paths
        assert "node_modules/pkg.js" not in paths

        # Verify sync_deleted_files
        (test_dir / "valid.py").unlink()
        sync_result = await sync_deleted_files(str(test_dir), project_id, scope)
        print(f"Sync result: {sync_result}")

        paths_after = graph_backend.get_all_filepaths(project_id, scope)
        assert len(paths_after) == 1
        assert "useful.md" in paths_after
        assert "valid.py" not in paths_after

    finally:
        # Cleanup
        if test_dir.exists():
            shutil.rmtree(test_dir)
        graph_backend.delete_data(project_id, scope)
        vector_backend.delete_data(project_id, scope)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backend_file_operations():
    """Directly test the backend delete_by_filepath and get_all_filepaths."""
    project_id = "BACKEND_TEST"
    scope = "UNIT_TEST"

    # Ingest manually to verify paths
    await ingest_graph_document("content 1", project_id, scope, file_path="test1.py")
    await ingest_graph_document("content 2", project_id, scope, file_path="test2.py")

    try:
        await asyncio.sleep(1)
        paths = graph_backend.get_all_filepaths(project_id, scope)
        print(f"Stored paths (manual): {paths}")
        assert "test1.py" in paths
        assert "test2.py" in paths

        # Test Memgraph deletion
        graph_backend.delete_by_filepath(project_id, "test1.py", scope)
        paths_after = graph_backend.get_all_filepaths(project_id, scope)
        assert "test1.py" not in paths_after
        assert "test2.py" in paths_after

    finally:
        graph_backend.delete_data(project_id, scope)
        vector_backend.delete_data(project_id, scope)
