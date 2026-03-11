# Version: v1.0
"""
nexus.backends.memgraph — All Memgraph driver, query, and mutation helpers.

Drop-in replacement for nexus.backends.neo4j. Memgraph uses the neo4j
Python driver and is compatible with Cypher, so most queries work as-is.

Key differences from Neo4j:
  - No APOC procedures
  - Index creation syntax differs (handled by LlamaIndex integration)
  - Default database is "memgraph" (not "neo4j")
"""

import logging
import threading

from neo4j import GraphDatabase

from nexus.config import (
    ALLOWED_META_KEYS,
    DEFAULT_MEMGRAPH_PASSWORD,
    DEFAULT_MEMGRAPH_URL,
    DEFAULT_MEMGRAPH_USER,
)

logger = logging.getLogger("mcp-nexus-rag")

# ---------------------------------------------------------------------------
# Singleton driver — one connection pool for the entire process lifetime
# ---------------------------------------------------------------------------
_driver_instance = None
_driver_lock = threading.Lock()


def get_driver():
    """Return the process-level Memgraph driver singleton (thread-safe).

    Uses double-checked locking so concurrent callers block only on the very
    first initialisation.  The returned driver must NOT be used as a context
    manager (that would call close() and destroy the pool).  Acquire sessions
    via ``get_driver().session()`` instead.

    Returns:
        neo4j.Driver instance (shared, long-lived).
    """
    global _driver_instance
    if _driver_instance is None:
        with _driver_lock:
            if _driver_instance is None:
                _driver_instance = GraphDatabase.driver(
                    DEFAULT_MEMGRAPH_URL,
                    auth=(DEFAULT_MEMGRAPH_USER, DEFAULT_MEMGRAPH_PASSWORD),
                )
    return _driver_instance


def get_distinct_metadata(key: str) -> list[str]:
    """Return distinct values for *key* across all Memgraph nodes.

    Args:
        key: A metadata key (must be in ALLOWED_META_KEYS).

    Returns:
        List of unique string values, empty list on connection error.

    Raises:
        ValueError: If *key* is not in ALLOWED_META_KEYS.
    """
    if key not in ALLOWED_META_KEYS:
        raise ValueError(f"Disallowed metadata key: {key!r}")
    try:
        with get_driver().session() as session:
            result = session.run(
                f"MATCH (n) WHERE n.{key} IS NOT NULL RETURN DISTINCT n.{key} AS value"
            )
            return [record["value"] for record in result]
    except Exception as e:
        logger.warning(f"Memgraph distinct '{key}' error: {e}")
        return []


def get_scopes_for_project(project_id: str) -> list[str]:
    """Return distinct tenant_scope values for a specific project_id.

    Args:
        project_id: Tenant project ID to filter by.

    Returns:
        List of unique scope strings, empty list on connection error.
    """
    try:
        with get_driver().session() as session:
            result = session.run(
                "MATCH (n {project_id: $project_id}) WHERE n.tenant_scope IS NOT NULL "
                "RETURN DISTINCT n.tenant_scope AS value",
                project_id=project_id,
            )
            return [record["value"] for record in result]
    except Exception as e:
        logger.warning(f"Memgraph scopes error: {e}")
        return []


def delete_data(project_id: str, scope: str = "") -> None:
    """Delete Memgraph nodes matching project_id (and optionally scope).

    Args:
        project_id: Tenant project ID to target.
        scope: If non-empty, restricts deletion to this tenant_scope.

    Raises:
        Exception: Propagated from the Memgraph driver on failure.
    """
    if scope:
        cypher = (
            "MATCH (n {project_id: $project_id, tenant_scope: $scope}) DETACH DELETE n"
        )
        params = {"project_id": project_id, "scope": scope}
    else:
        cypher = "MATCH (n {project_id: $project_id}) DETACH DELETE n"
        params = {"project_id": project_id}
    try:
        with get_driver().session() as session:
            session.run(cypher, **params)
    except Exception as e:
        logger.error(f"Memgraph delete error: {e}")
        raise


def get_all_filepaths(project_id: str, scope: str = "") -> list[str]:
    """Return distinct file_path values for a specific project_id/scope."""
    try:
        with get_driver().session() as session:
            if scope:
                result = session.run(
                    "MATCH (n {project_id: $project_id, tenant_scope: $scope}) "
                    "WHERE n.file_path IS NOT NULL RETURN DISTINCT n.file_path AS value",
                    project_id=project_id,
                    scope=scope,
                )
            else:
                result = session.run(
                    "MATCH (n {project_id: $project_id}) "
                    "WHERE n.file_path IS NOT NULL RETURN DISTINCT n.file_path AS value",
                    project_id=project_id,
                )
            return [record["value"] for record in result]
    except Exception as e:
        logger.warning(f"Memgraph get_all_filepaths error: {e}")
        return []


def delete_by_filepath(project_id: str, filepath: str, scope: str = "") -> None:
    """Delete Memgraph nodes matching project_id, scope, and file_path.

    Also deletes chunked variants with ``:chunk_`` suffix so pre-delete works
    for auto-chunked ingest paths.
    """
    try:
        with get_driver().session() as session:
            chunk_prefix = f"{filepath}:chunk_"
            if scope:
                session.run(
                    "MATCH (n) "
                    "WHERE n.project_id = $project_id "
                    "AND n.tenant_scope = $scope "
                    "AND n.file_path IS NOT NULL "
                    "AND (n.file_path = $filepath OR n.file_path STARTS WITH $chunk_prefix) "
                    "DETACH DELETE n",
                    project_id=project_id,
                    scope=scope,
                    filepath=filepath,
                    chunk_prefix=chunk_prefix,
                )
            else:
                session.run(
                    "MATCH (n) "
                    "WHERE n.project_id = $project_id "
                    "AND n.file_path IS NOT NULL "
                    "AND (n.file_path = $filepath OR n.file_path STARTS WITH $chunk_prefix) "
                    "DETACH DELETE n",
                    project_id=project_id,
                    filepath=filepath,
                    chunk_prefix=chunk_prefix,
                )
    except Exception as e:
        logger.error(f"Memgraph delete_by_filepath error: {e}")
        raise


def backfill_file_metadata(project_id: str, scope: str, filepath: str) -> int:
    """Backfill missing project/scope metadata for nodes from *filepath*.

    Returns number of updated nodes.
    """
    if not filepath:
        return 0

    try:
        with get_driver().session() as session:
            chunk_prefix = f"{filepath}:chunk_"
            result = session.run(
                "MATCH (n) "
                "WHERE n.file_path IS NOT NULL "
                "AND (n.file_path = $filepath OR n.file_path STARTS WITH $chunk_prefix) "
                "AND (n.project_id IS NULL OR n.tenant_scope IS NULL "
                "OR trim(toString(n.project_id)) = '' OR trim(toString(n.tenant_scope)) = '') "
                "SET n.project_id = $project_id, n.tenant_scope = $scope "
                "RETURN count(n) AS updated",
                filepath=filepath,
                chunk_prefix=chunk_prefix,
                project_id=project_id,
                scope=scope,
            ).single()
            return int(result["updated"]) if result else 0
    except Exception as e:
        logger.warning(f"Memgraph metadata backfill error for '{filepath}': {e}")
        return 0


def backfill_all_unscoped(project_id: str, scope: str) -> int:
    """Tag ALL unscoped nodes (no project_id) with the given tenant metadata.

    Returns:
        Number of nodes updated.
    """
    try:
        with get_driver().session() as session:
            result = session.run(
                "MATCH (n) "
                "WHERE n.project_id IS NULL "
                "SET n.project_id = $project_id, n.tenant_scope = $scope "
                "RETURN count(n) AS updated",
                project_id=project_id,
                scope=scope,
            ).single()
            return int(result["updated"]) if result else 0
    except Exception as e:
        logger.warning(f"Memgraph backfill_all_unscoped error: {e}")
        return 0


def is_duplicate(content_hash: str, project_id: str, scope: str) -> bool:
    """Return True if this content hash already exists in Memgraph.

    Fails open (returns False) on any error.
    """
    try:
        with get_driver().session() as session:
            result = session.run(
                "MATCH (n {project_id: $project_id, tenant_scope: $scope, "
                "content_hash: $content_hash}) RETURN COUNT(n) > 0 AS exists",
                project_id=project_id,
                scope=scope,
                content_hash=content_hash,
            )
            record = result.single()
            return bool(record["exists"]) if record else False
    except Exception as e:
        logger.warning(f"Memgraph dedup check failed (fail-open): {e}")
        return False


def is_file_content_duplicate(
    file_content_hash: str, project_id: str, scope: str
) -> bool:
    """Return True if this whole-file content hash already exists in Memgraph.

    Fails open (returns False) on any error.
    """
    try:
        with get_driver().session() as session:
            result = session.run(
                "MATCH (n {project_id: $project_id, tenant_scope: $scope, "
                "file_content_hash: $fch}) RETURN COUNT(n) > 0 AS exists",
                project_id=project_id,
                scope=scope,
                fch=file_content_hash,
            )
            record = result.single()
            return bool(record["exists"]) if record else False
    except Exception as e:
        logger.warning(f"Memgraph file_content_hash check failed (fail-open): {e}")
        return False


def delete_all_data() -> None:
    """Delete ALL nodes from Memgraph across every project and scope.

    This is a destructive, irreversible operation. Use only for full resets.

    Raises:
        Exception: Propagated from the Memgraph driver on failure.
    """
    try:
        with get_driver().session() as session:
            session.run("MATCH (n) DETACH DELETE n")
        logger.warning("Memgraph: deleted ALL nodes from the database")
    except Exception as e:
        logger.error(f"Memgraph delete_all error: {e}")
        raise


def get_document_count(project_id: str, scope: str = "") -> int:
    """Return the count of documents for a project/scope in Memgraph."""
    try:
        with get_driver().session() as session:
            if scope:
                result = session.run(
                    "MATCH (n {project_id: $project_id, tenant_scope: $scope}) "
                    "RETURN COUNT(n) AS count",
                    project_id=project_id,
                    scope=scope,
                )
            else:
                result = session.run(
                    "MATCH (n {project_id: $project_id}) RETURN COUNT(n) AS count",
                    project_id=project_id,
                )
            record = result.single()
            return int(record["count"]) if record else 0
    except Exception as e:
        logger.warning(f"Memgraph document count error: {e}")
        return 0


def get_chunk_node_count(project_id: str, scope: str = "") -> int:
    """Count source chunk nodes (those with content_hash) for a project/scope."""
    try:
        with get_driver().session() as session:
            if scope:
                result = session.run(
                    "MATCH (n {project_id: $project_id, tenant_scope: $scope}) "
                    "WHERE n.content_hash IS NOT NULL RETURN COUNT(n) AS count",
                    project_id=project_id,
                    scope=scope,
                )
            else:
                result = session.run(
                    "MATCH (n {project_id: $project_id}) "
                    "WHERE n.content_hash IS NOT NULL RETURN COUNT(n) AS count",
                    project_id=project_id,
                )
            record = result.single()
            return int(record["count"]) if record else 0
    except Exception as e:
        logger.warning(f"Memgraph chunk count error: {e}")
        return 0


def get_entity_node_count(project_id: str, scope: str = "") -> int:
    """Count LLM-extracted entity nodes connected to chunk nodes."""
    try:
        with get_driver().session() as session:
            if scope:
                result = session.run(
                    "MATCH (chunk {project_id: $project_id, tenant_scope: $scope})"
                    "-[]-(entity) "
                    "WHERE entity.content_hash IS NULL "
                    "RETURN COUNT(DISTINCT entity) AS count",
                    project_id=project_id,
                    scope=scope,
                )
            else:
                result = session.run(
                    "MATCH (chunk {project_id: $project_id})-[]-(entity) "
                    "WHERE entity.content_hash IS NULL "
                    "RETURN COUNT(DISTINCT entity) AS count",
                    project_id=project_id,
                )
            record = result.single()
            return int(record["count"]) if record else 0
    except Exception as e:
        logger.warning(f"Memgraph entity count error: {e}")
        return 0
