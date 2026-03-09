# Version: v2.4
"""
nexus.backends.neo4j — All Neo4j driver, query, and mutation helpers.

v2.2: Add get_driver() singleton to avoid creating a new connection pool per
call. Previously every function did ``with neo4j_driver() as driver:`` which
called driver.close() on __exit__, destroying the pool on every query.
v2.3: delete_by_filepath now removes chunk-suffixed variants; added
backfill_file_metadata() for unscoped node repair by file_path.
v2.4: Added backfill_all_unscoped() to catch entity nodes without file_path;
broadened backfill_file_metadata() to also tag entity nodes connected to
scoped chunks.
"""

import logging
import threading

from neo4j import GraphDatabase

from nexus.config import (
    ALLOWED_META_KEYS,
    DEFAULT_NEO4J_PASSWORD,
    DEFAULT_NEO4J_URL,
    DEFAULT_NEO4J_USER,
)

logger = logging.getLogger("mcp-nexus-rag")

# ---------------------------------------------------------------------------
# Singleton driver — one connection pool for the entire process lifetime
# ---------------------------------------------------------------------------
_driver_instance = None
_driver_lock = threading.Lock()


def get_driver():
    """Return the process-level Neo4j driver singleton (thread-safe).

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
                    DEFAULT_NEO4J_URL,
                    auth=(DEFAULT_NEO4J_USER, DEFAULT_NEO4J_PASSWORD),
                )
    return _driver_instance


def neo4j_driver():
    """Create a *new* Neo4j driver configured for use as a context manager.

    .. deprecated::
        Prefer :func:`get_driver` which reuses a process-level singleton and
        avoids the connection-pool setup/teardown overhead on every query.
        This function remains for backward compatibility and tests that verify
        the ``GraphDatabase.driver()`` call signature.

    Returns:
        neo4j.Driver instance (new, caller-owned).
    """
    return GraphDatabase.driver(
        DEFAULT_NEO4J_URL,
        auth=(DEFAULT_NEO4J_USER, DEFAULT_NEO4J_PASSWORD),
    )


def get_distinct_metadata(key: str) -> list[str]:
    """Return distinct values for *key* across all Neo4j nodes.

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
        logger.warning(f"Neo4j distinct '{key}' error: {e}")
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
        logger.warning(f"Neo4j scopes error: {e}")
        return []


def delete_data(project_id: str, scope: str = "") -> None:
    """Delete Neo4j nodes matching project_id (and optionally scope).

    Bug fix v1.7: re-raises on exception so delete_tenant_data can
    detect and report Neo4j failures (symmetric with Qdrant behaviour).

    Args:
        project_id: Tenant project ID to target.
        scope: If non-empty, restricts deletion to this tenant_scope.

    Raises:
        Exception: Propagated from the Neo4j driver on failure.
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
        logger.error(f"Neo4j delete error: {e}")
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
        logger.warning(f"Neo4j get_all_filepaths error: {e}")
        return []


def delete_by_filepath(project_id: str, filepath: str, scope: str = "") -> None:
    """Delete Neo4j nodes matching project_id, scope, and file_path.

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
        logger.error(f"Neo4j delete_by_filepath error: {e}")
        raise


def backfill_file_metadata(project_id: str, scope: str, filepath: str) -> int:
    """Backfill missing project/scope metadata for nodes from *filepath*.

    Returns number of updated nodes. This is used as a safety net when graph
    extraction inserts Chunk-like nodes without tenant metadata.
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
        logger.warning(f"Neo4j metadata backfill error for '{filepath}': {e}")
        return 0


def backfill_all_unscoped(project_id: str, scope: str) -> int:
    """Tag ALL unscoped nodes (no project_id) with the given tenant metadata.

    PropertyGraphIndex entity extraction creates nodes without tenant metadata.
    Unlike backfill_file_metadata() which only fixes nodes with a file_path,
    this catches orphan entity/chunk nodes that have no file_path at all.

    Scoped to nodes that were recently created (no project_id set yet) to
    avoid accidentally re-tagging nodes from other tenants.

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
        logger.warning(f"Neo4j backfill_all_unscoped error: {e}")
        return 0


def is_duplicate(content_hash: str, project_id: str, scope: str) -> bool:
    """Return True if this content hash already exists in Neo4j.

    Fails open (returns False) on any error — ingestion is never
    silently blocked by a connectivity issue.

    Args:
        content_hash: SHA-256 hex digest from nexus.dedup.content_hash().
        project_id: Tenant project ID.
        scope: Tenant scope.

    Returns:
        True if a duplicate was found, False otherwise.
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
        logger.warning(f"Neo4j dedup check failed (fail-open): {e}")
        return False


def delete_all_data() -> None:
    """Delete ALL nodes from Neo4j across every project and scope.

    This is a destructive, irreversible operation. Use only for full resets.

    Raises:
        Exception: Propagated from the Neo4j driver on failure.
    """
    try:
        with get_driver().session() as session:
            session.run("MATCH (n) DETACH DELETE n")
        logger.warning("Neo4j: deleted ALL nodes from the database")
    except Exception as e:
        logger.error(f"Neo4j delete_all error: {e}")
        raise


def get_document_count(project_id: str, scope: str = "") -> int:
    """Return the count of documents for a project/scope in Neo4j.

    Args:
        project_id: Tenant project ID.
        scope: Optional tenant scope. If empty, counts all scopes.

    Returns:
        Number of nodes matching the criteria, 0 on error.
    """
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
        logger.warning(f"Neo4j document count error: {e}")
        return 0


def get_chunk_node_count(project_id: str, scope: str = "") -> int:
    """Count source chunk nodes (those with content_hash) for a project/scope.

    Chunk nodes are the raw text documents we explicitly ingested.  They always
    carry a ``content_hash`` property set by nexus.dedup.

    Args:
        project_id: Tenant project ID.
        scope: Optional tenant scope. If empty, counts all scopes.

    Returns:
        Number of chunk nodes, 0 on error.
    """
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
        logger.warning(f"Neo4j chunk count error: {e}")
        return 0


def get_entity_node_count(project_id: str, scope: str = "") -> int:
    """Count LLM-extracted entity nodes connected to chunk nodes for a project/scope.

    Entity nodes are created by the LlamaIndex graph extraction pipeline (via
    ``qwen2.5:3b``).  Unlike chunk nodes, they don't carry ``content_hash``.
    This query traverses one hop from chunk nodes to find connected entities.

    Args:
        project_id: Tenant project ID.
        scope: Optional tenant scope. If empty, counts all scopes.

    Returns:
        Number of distinct entity nodes, 0 on error.
    """
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
        logger.warning(f"Neo4j entity count error: {e}")
        return 0
