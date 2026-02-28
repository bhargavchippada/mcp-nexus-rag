
import os
import shutil
import asyncio
from pathlib import Path
import pytest
from nexus.tools import ingest_project_directory, sync_deleted_files, ingest_graph_document
from nexus.backends import neo4j as neo4j_backend
from nexus.backends import qdrant as qdrant_backend

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
        
        # Verify Neo4j has the paths
        paths = neo4j_backend.get_all_filepaths(project_id, scope)
        print(f"Stored paths: {paths}")
        
        assert "valid.py" in paths
        assert "useful.md" in paths
        assert "ignored.tmp" not in paths
        assert "node_modules/pkg.js" not in paths
        
        # Verify sync_deleted_files
        (test_dir / "valid.py").unlink()
        sync_result = await sync_deleted_files(str(test_dir), project_id, scope)
        print(f"Sync result: {sync_result}")
        
        paths_after = neo4j_backend.get_all_filepaths(project_id, scope)
        assert len(paths_after) == 1
        assert "useful.md" in paths_after
        assert "valid.py" not in paths_after
        
    finally:
        # Cleanup
        if test_dir.exists():
            shutil.rmtree(test_dir)
        neo4j_backend.delete_data(project_id, scope)
        qdrant_backend.delete_data(project_id, scope)

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
        paths = neo4j_backend.get_all_filepaths(project_id, scope)
        print(f"Stored paths (manual): {paths}")
        assert "test1.py" in paths
        assert "test2.py" in paths
        
        # Test Neo4j deletion
        neo4j_backend.delete_by_filepath(project_id, "test1.py", scope)
        paths_after = neo4j_backend.get_all_filepaths(project_id, scope)
        assert "test1.py" not in paths_after
        assert "test2.py" in paths_after
        
    finally:
        neo4j_backend.delete_data(project_id, scope)
        qdrant_backend.delete_data(project_id, scope)
