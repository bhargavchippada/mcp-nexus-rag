# Version: v1.5
"""
nexus.sync — File synchronization for core documentation.

Provides pattern-based file watching and ingestion for project documentation.
Only ingests core files (README.md, MEMORY.md, AGENTS.md, TODO.md) to keep
the RAG focused on high-signal content.
"""

from pathlib import Path
from typing import Optional

from nexus.backends import neo4j as neo4j_backend
from nexus.backends import qdrant as qdrant_backend
from nexus.config import logger
from nexus.dedup import content_hash

# ---------------------------------------------------------------------------
# Core documentation patterns
# ---------------------------------------------------------------------------

# Files to ingest from each project
CORE_DOC_PATTERNS = [
    "README.md",
    "MEMORY.md",
    "AGENTS.md",
    "TODO.md",
]

# Project ID mapping (directory name -> project_id)
PROJECT_MAPPINGS = {
    "mcp-nexus-rag": "MCP_NEXUS_RAG",
    "gravity-claw": "GRAVITY_CLAW",
    "mission-control": "MISSION_CONTROL",
    "web-scrapers": "WEB_SCRAPERS",
    "agentic-trader": "AGENTIC_TRADER",
}

# Workspace-level persona files (relative to antigravity root)
PERSONA_FILES = [
    "CLAUDE.md",
    "mission.md",
    "MEMORY.md",
    ".claude/rules/rules.md",
]


def _classify_file(filepath: Path, workspace_root: Path) -> Optional[tuple[str, str]]:
    """Return (project_id, scope) if filepath is a tracked core doc, else None.

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

    # Persona files (CLAUDE.md, rules.md, MEMORY.md, mission.md, …)
    if rel_str in PERSONA_FILES:
        return ("AGENT", "PERSONA")

    # Project core docs: projects/<name>/README.md | MEMORY.md | AGENTS.md | TODO.md
    parts = rel.parts
    if (
        len(parts) == 3
        and parts[0] == "projects"
        and filepath.name in CORE_DOC_PATTERNS
    ):
        project_dir = parts[1]
        project_id = PROJECT_MAPPINGS.get(
            project_dir, project_dir.upper().replace("-", "_")
        )
        return (project_id, "CORE_DOCS")

    return None


def _read_file_content(filepath: Path) -> Optional[str]:
    """Read file content, return None on error."""
    try:
        return filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to read {filepath}: {e}")
        return None


def _project_id_from_path(filepath: Path, workspace_root: Path) -> str:
    """Derive project_id from file path.

    Args:
        filepath: Absolute path to the file.
        workspace_root: Antigravity workspace root.

    Returns:
        Project ID string (e.g., 'GRAVITY_CLAW').
    """
    try:
        rel = filepath.relative_to(workspace_root)
        parts = rel.parts
        if len(parts) >= 2 and parts[0] == "projects":
            project_dir = parts[1]
            return PROJECT_MAPPINGS.get(
                project_dir, project_dir.upper().replace("-", "_")
            )
    except ValueError:
        pass
    return "AGENT"  # Default for workspace-level files


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
    """Scan workspace and return all core documentation files.

    Args:
        workspace_root: Path to antigravity workspace root.

    Returns:
        List of dicts with keys: filepath, project_id, scope, source.
    """
    root = Path(workspace_root)
    files = []

    # 1. Persona files (workspace-level)
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

    # 2. Project documentation files
    projects_dir = root / "projects"
    if projects_dir.exists():
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            if project_dir.name.startswith("."):
                continue

            project_id = PROJECT_MAPPINGS.get(
                project_dir.name, project_dir.name.upper().replace("-", "_")
            )

            for pattern in CORE_DOC_PATTERNS:
                filepath = project_dir / pattern
                if filepath.exists():
                    files.append(
                        {
                            "filepath": filepath,
                            "project_id": project_id,
                            "scope": "CORE_DOCS",
                            "source": f"project:{project_dir.name}/{pattern}",
                        }
                    )

    logger.info(f"Found {len(files)} core documentation files")
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
        needs_graph: True if Neo4j is missing this content
        needs_vector: True if Qdrant is missing this content

    This allows callers to selectively ingest only into the store that
    needs it, preventing duplicates from partial-failure recovery.
    """
    content = _read_file_content(filepath)
    if content is None:
        return {"changed": False, "needs_graph": False, "needs_vector": False}

    chash = content_hash(content, project_id, scope)

    neo4j_dup = neo4j_backend.is_duplicate(chash, project_id, scope)
    qdrant_dup = qdrant_backend.is_duplicate(chash, project_id, scope)

    return {
        "changed": not (neo4j_dup and qdrant_dup),
        "needs_graph": not neo4j_dup,
        "needs_vector": not qdrant_dup,
    }


def get_files_needing_sync(workspace_root: str | Path) -> list[dict]:
    """Return list of core doc files that need to be synced (changed or new).

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

    # Union Neo4j + Qdrant — catch orphans in either store
    neo4j_paths = set(neo4j_backend.get_all_filepaths(project_id, scope))
    qdrant_paths = set(qdrant_backend.get_all_filepaths(project_id, scope))
    indexed_paths = neo4j_paths | qdrant_paths

    for indexed_path in indexed_paths:
        full_path = (
            root / indexed_path
            if not Path(indexed_path).is_absolute()
            else Path(indexed_path)
        )
        if not full_path.exists():
            # File was deleted - remove from both stores
            try:
                neo4j_backend.delete_by_filepath(project_id, indexed_path, scope)
                qdrant_backend.delete_by_filepath(project_id, indexed_path, scope)
                deleted.append(indexed_path)
                logger.info(f"Deleted stale document: {indexed_path}")
            except Exception as e:
                logger.error(f"Failed to delete stale document {indexed_path}: {e}")

    return deleted
