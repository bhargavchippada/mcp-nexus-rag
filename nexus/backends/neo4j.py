# Version: v2.1
"""
nexus.backends.neo4j — All Neo4j driver, query, and mutation helpers.
"""

import logging

from neo4j import GraphDatabase

from nexus.config import (
    DEFAULT_NEO4J_URL,
    DEFAULT_NEO4J_USER,
    DEFAULT_NEO4J_PASSWORD,
    ALLOWED_META_KEYS,
)

logger = logging.getLogger("mcp-nexus-rag")


def neo4j_driver():
    """Return a new Neo4j driver configured for use as a context manager.

    Returns:
        neo4j.Driver instance.
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
        with neo4j_driver() as driver:
            with driver.session() as session:
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
        with neo4j_driver() as driver:
            with driver.session() as session:
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
        with neo4j_driver() as driver:
            with driver.session() as session:
                session.run(cypher, **params)
    except Exception as e:
        logger.error(f"Neo4j delete error: {e}")
        raise


def get_all_filepaths(project_id: str, scope: str = "") -> list[str]:
    """Return distinct file_path values for a specific project_id/scope.
    """
    try:
        with neo4j_driver() as driver:
            with driver.session() as session:
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
    """
    try:
        with neo4j_driver() as driver:
            with driver.session() as session:
                if scope:
                    session.run(
                        "MATCH (n {project_id: $project_id, tenant_scope: $scope, file_path: $filepath}) DETACH DELETE n",
                        project_id=project_id,
                        scope=scope,
                        filepath=filepath,
                    )
                else:
                    session.run(
                        "MATCH (n {project_id: $project_id, file_path: $filepath}) DETACH DELETE n",
                        project_id=project_id,
                        filepath=filepath,
                    )
    except Exception as e:
        logger.error(f"Neo4j delete_by_filepath error: {e}")
        raise


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
        with neo4j_driver() as driver:
            with driver.session() as session:
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
        with neo4j_driver() as driver:
            with driver.session() as session:
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
        with neo4j_driver() as driver:
            with driver.session() as session:
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
        with neo4j_driver() as driver:
            with driver.session() as session:
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
    ``llama3.1:8b``).  Unlike chunk nodes, they don't carry ``content_hash``.
    This query traverses one hop from chunk nodes to find connected entities.

    Args:
        project_id: Tenant project ID.
        scope: Optional tenant scope. If empty, counts all scopes.

    Returns:
        Number of distinct entity nodes, 0 on error.
    """
    try:
        with neo4j_driver() as driver:
            with driver.session() as session:
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
