# Version: v3.2
"""
nexus.sync — File synchronization for core documentation files.

Tracks CLAUDE.md at workspace root and per-project for auto-ingestion.
Other docs (README, MEMORY, AGENTS, TODO) excluded — they change too
frequently, causing constant re-indexing that saturates the LLM pipeline.

Includes a per-file asyncio lock to prevent concurrent ingestion of the same
file from racing (e.g. watcher + manual sync_project_files overlap).
"""

import asyncio
from pathlib import Path
from typing import Optional

from nexus.backends import memgraph as graph_backend
from nexus.backends import pgvector as vector_backend
from nexus.config import logger
from nexus.dedup import content_hash

# Per-file asyncio lock to prevent concurrent ingestion of the same file.
# Key: canonical file path string, Value: asyncio.Lock.
_sync_locks: dict[str, asyncio.Lock] = {}


def get_sync_lock(file_key: str) -> asyncio.Lock:
    """Return a per-file asyncio lock (created on first access)."""
    if file_key not in _sync_locks:
        _sync_locks[file_key] = asyncio.Lock()
    return _sync_locks[file_key]


# ---------------------------------------------------------------------------
# Tracked files
# ---------------------------------------------------------------------------

# Core documentation file tracked for auto-ingestion.
# Only CLAUDE.md is tracked — other project docs change too frequently,
# causing constant re-indexing that saturates the Ollama LLM pipeline.
PERSONA_FILES = [
    "CLAUDE.md",
]


def _classify_file(filepath: Path, workspace_root: Path) -> Optional[tuple[str, str]]:
    """Return (project_id, scope) if filepath is a tracked file, else None.

    Works for both existing and deleted files — no filesystem I/O.

    Args:
        filepath: Absolute (or workspace-relative) path to the file.
        workspace_root: Antigravity workspace root.

    Returns:
        (project_id, scope) tuple, or None if the file is not tracked.
    """
    try:
        rel = filepath.relative_to(workspace_root)
    except ValueError:
        return None  # Outside workspace root

    rel_str = str(rel)
    parts = rel.parts

    # Persona files (CLAUDE.md only)
    if rel_str in PERSONA_FILES:
        return ("AGENT", "PERSONA")

    # Per-project CLAUDE.md: projects/<name>/CLAUDE.md
    if len(parts) == 3 and parts[0] == "projects" and parts[2] in PERSONA_FILES:
        project_name = parts[1]
        project_id = project_name.upper().replace("-", "_")
        return (project_id, "PERSONA")

    return None


def _read_file_content(filepath: Path) -> Optional[str]:
    """Read file content, return None on error."""
    try:
        return filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to read {filepath}: {e}")
        return None


def canonical_file_path(filepath: Path, workspace_root: Path) -> str:
    """Return a canonical workspace-relative file path when possible.

    Storing relative paths avoids absolute/relative drift between sync paths
    (manual sync vs watcher), which can otherwise break delete-by-filepath
    cleanup and lead to duplicate chunks.
    """
    try:
        return str(filepath.resolve().relative_to(workspace_root.resolve()))
    except ValueError:
        return str(filepath)


def get_core_doc_files(workspace_root: str | Path) -> list[dict]:
    """Scan workspace and return tracked documentation files.

    Args:
        workspace_root: Path to antigravity workspace root.

    Returns:
        List of dicts with keys: filepath, project_id, scope, source.
    """
    root = Path(workspace_root)
    files = []

    # Persona files (workspace-level)
    for rel_path in PERSONA_FILES:
        filepath = root / rel_path
        if filepath.exists():
            files.append(
                {
                    "filepath": filepath,
                    "project_id": "AGENT",
                    "scope": "PERSONA",
                    "source": f"workspace:{rel_path}",
                }
            )

    # Per-project persona files: projects/<name>/CLAUDE.md
    projects_dir = root / "projects"
    if projects_dir.is_dir():
        for project_dir in sorted(projects_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            for rel_path in PERSONA_FILES:
                filepath = project_dir / rel_path
                if filepath.exists():
                    project_id = project_dir.name.upper().replace("-", "_")
                    files.append(
                        {
                            "filepath": filepath,
                            "project_id": project_id,
                            "scope": "PERSONA",
                            "source": f"project:{project_dir.name}/{rel_path}",
                        }
                    )

    logger.info(f"Found {len(files)} tracked documentation files")
    return files


def check_file_changed(filepath: Path, project_id: str, scope: str) -> bool:
    """Check if file content has changed since last ingestion.

    Uses content hash to detect changes. Returns True if:
    - File has never been ingested
    - File content has changed

    Args:
        filepath: Path to the file.
        project_id: Tenant project ID.
        scope: Tenant scope.

    Returns:
        True if file should be re-ingested.
    """
    result = check_file_sync_status(filepath, project_id, scope)
    return result["changed"]


def check_file_sync_status(
    filepath: Path, project_id: str, scope: str
) -> dict[str, bool]:
    """Check file sync status across both stores.

    Returns a dict with:
        changed: True if any store needs updating
        needs_graph: True if Memgraph is missing this content
        needs_vector: True if pgvector is missing this content

    This allows callers to selectively ingest only into the store that
    needs it, preventing duplicates from partial-failure recovery.

    Uses ``file_content_hash`` (whole-file hash stored on every chunk during
    ingestion) rather than ``content_hash`` (per-chunk hash). This fixes the
    whole-file vs per-chunk hash mismatch that previously caused every sync
    call to re-ingest, creating duplicates on concurrent calls.
    """
    content = _read_file_content(filepath)
    if content is None:
        return {"changed": False, "needs_graph": False, "needs_vector": False}

    chash = content_hash(content, project_id, scope)

    graph_dup = graph_backend.is_file_content_duplicate(chash, project_id, scope)
    vector_dup = vector_backend.is_file_content_duplicate(chash, project_id, scope)

    return {
        "changed": not (graph_dup and vector_dup),
        "needs_graph": not graph_dup,
        "needs_vector": not vector_dup,
    }


def get_files_needing_sync(workspace_root: str | Path) -> list[dict]:
    """Return list of tracked files that need to be synced (changed or new).

    Args:
        workspace_root: Path to antigravity workspace root.

    Returns:
        List of file dicts that need ingestion.
    """
    all_files = get_core_doc_files(workspace_root)
    changed = []

    for f in all_files:
        if check_file_changed(f["filepath"], f["project_id"], f["scope"]):
            changed.append(f)

    logger.info(f"{len(changed)} of {len(all_files)} files need sync")
    return changed


def delete_stale_files(
    workspace_root: str | Path,
    project_id: str,
    scope: str,
) -> list[str]:
    """Delete documents for files that no longer exist on disk.

    Args:
        workspace_root: Path to antigravity workspace root.
        project_id: Tenant project ID to check.
        scope: Tenant scope to check.

    Returns:
        List of deleted file paths.
    """
    root = Path(workspace_root)
    deleted = []

    # Union Memgraph + pgvector — catch orphans in either store
    graph_paths = set(graph_backend.get_all_filepaths(project_id, scope))
    vector_paths = set(vector_backend.get_all_filepaths(project_id, scope))
    indexed_paths = graph_paths | vector_paths

    for indexed_path in indexed_paths:
        full_path = (
            root / indexed_path
            if not Path(indexed_path).is_absolute()
            else Path(indexed_path)
        )
        if not full_path.exists():
            # File was deleted - remove from both stores
            try:
                graph_backend.delete_by_filepath(project_id, indexed_path, scope)
                vector_backend.delete_by_filepath(project_id, indexed_path, scope)
                deleted.append(indexed_path)
                logger.info(f"Deleted stale document: {indexed_path}")
            except Exception as e:
                logger.error(f"Failed to delete stale document {indexed_path}: {e}")

    return deleted
