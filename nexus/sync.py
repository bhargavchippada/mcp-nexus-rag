# Version: v1.0
"""
nexus.sync — File synchronization for core documentation.

Provides pattern-based file watching and ingestion for project documentation.
Only ingests core files (README.md, MEMORY.md, AGENTS.md, TODO.md) to keep
the RAG focused on high-signal content.
"""

from pathlib import Path
from typing import Optional

from nexus.config import logger
from nexus.dedup import content_hash
from nexus.backends import neo4j as neo4j_backend
from nexus.backends import qdrant as qdrant_backend

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
}

# Workspace-level persona files (relative to antigravity root)
PERSONA_FILES = [
    "CLAUDE.md",
    "mission.md",
    "MEMORY.md",
    ".claude/persona/GEMINI.md",
    ".claude/rules/rules.md",
]


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
    content = _read_file_content(filepath)
    if content is None:
        return False

    chash = content_hash(content, project_id, scope)

    # Check both stores
    neo4j_dup = neo4j_backend.is_duplicate(chash, project_id, scope)
    qdrant_dup = qdrant_backend.is_duplicate(chash, project_id, scope)

    # File changed if not in either store
    return not (neo4j_dup and qdrant_dup)


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

    # Get all indexed file paths from Neo4j
    indexed_paths = neo4j_backend.get_all_filepaths(project_id, scope)

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
