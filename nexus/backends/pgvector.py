# Version: v1.1
"""
nexus.backends.pgvector — All pgvector/PostgreSQL query and mutation helpers.

Drop-in replacement for nexus.backends.qdrant. Uses direct SQL against the
PGVectorStore table (``nexus_rag`` in ``public`` schema) for dedup checks,
deletions, and metadata queries.

The PGVectorStore manages its own table schema via SQLAlchemy — this module
only does read/delete operations against it using psycopg2 for sync access.
"""

import logging
import threading

import psycopg2
import psycopg2.extras

from nexus.config import (
    ALLOWED_META_KEYS,
    DEFAULT_PG_DATABASE,
    DEFAULT_PG_HOST,
    DEFAULT_PG_PASSWORD,
    DEFAULT_PG_PORT,
    DEFAULT_PG_USER,
    PG_TABLE_NAME_SQL,
)

logger = logging.getLogger("mcp-nexus-rag")

# ---------------------------------------------------------------------------
# Connection pool — reuse connections across calls
# ---------------------------------------------------------------------------
_conn_cache: dict[str, psycopg2.extensions.connection] = {}
_conn_lock = threading.Lock()


def _dsn() -> str:
    """Build psycopg2 DSN from config."""
    return (
        f"host={DEFAULT_PG_HOST} port={DEFAULT_PG_PORT} "
        f"dbname={DEFAULT_PG_DATABASE} user={DEFAULT_PG_USER} "
        f"password={DEFAULT_PG_PASSWORD}"
    )


def get_connection() -> psycopg2.extensions.connection:
    """Return a cached psycopg2 connection, creating one on first call.

    Reconnects automatically if the connection is closed.
    """
    dsn = _dsn()
    if dsn in _conn_cache:
        conn = _conn_cache[dsn]
        if conn.closed:
            del _conn_cache[dsn]
        else:
            return conn

    with _conn_lock:
        if dsn in _conn_cache and not _conn_cache[dsn].closed:
            return _conn_cache[dsn]
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        _conn_cache[dsn] = conn
    return conn


def _query_metadata(sql: str, params: tuple = ()) -> list:
    """Execute a read query and return all rows."""
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception as e:
        logger.warning(f"pgvector query error: {e}")
        return []


def _execute(sql: str, params: tuple = ()) -> None:
    """Execute a write query."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql, params)


# ---------------------------------------------------------------------------
# Public API — mirrors nexus.backends.qdrant interface
# ---------------------------------------------------------------------------


def get_distinct_metadata(key: str) -> list[str]:
    """Return distinct payload values for *key* across the pgvector table.

    Args:
        key: Payload field name (must be in ALLOWED_META_KEYS).

    Returns:
        List of unique string values, empty list on error.

    Raises:
        ValueError: If *key* is not in ALLOWED_META_KEYS.
    """
    if key not in ALLOWED_META_KEYS:
        raise ValueError(f"Disallowed metadata key: {key!r}")
    try:
        rows = _query_metadata(
            f"SELECT DISTINCT metadata_->>%s AS value FROM {PG_TABLE_NAME_SQL} "
            f"WHERE metadata_->>%s IS NOT NULL",
            (key, key),
        )
        return [r["value"] for r in rows if r["value"] is not None]
    except Exception as e:
        logger.warning(f"pgvector distinct '{key}' error: {e}")
        return []


def get_scopes_for_project(project_id: str) -> list[str]:
    """Return distinct tenant_scope values for a specific project_id."""
    try:
        rows = _query_metadata(
            f"SELECT DISTINCT metadata_->>'tenant_scope' AS value FROM {PG_TABLE_NAME_SQL} "
            f"WHERE metadata_->>'project_id' = %s "
            f"AND metadata_->>'tenant_scope' IS NOT NULL",
            (project_id,),
        )
        return [r["value"] for r in rows if r["value"] is not None]
    except Exception as e:
        logger.warning(f"pgvector scopes error: {e}")
        return []


def delete_data(project_id: str, scope: str = "") -> None:
    """Delete pgvector rows matching project_id (and optionally scope).

    Raises:
        Exception: Propagated from psycopg2 on failure.
    """
    try:
        if scope:
            _execute(
                f"DELETE FROM {PG_TABLE_NAME_SQL} "
                f"WHERE metadata_->>'project_id' = %s "
                f"AND metadata_->>'tenant_scope' = %s",
                (project_id, scope),
            )
        else:
            _execute(
                f"DELETE FROM {PG_TABLE_NAME_SQL} WHERE metadata_->>'project_id' = %s",
                (project_id,),
            )
    except Exception as e:
        logger.error(f"pgvector delete error: {e}")
        raise


def delete_by_filepath(project_id: str, filepath: str, scope: str = "") -> None:
    """Delete pgvector rows matching project_id, scope, and file_path.

    Also deletes chunked variants with ``:chunk_`` suffix.
    """
    try:
        chunk_prefix = f"{filepath}:chunk_%"
        if scope:
            _execute(
                f"DELETE FROM {PG_TABLE_NAME_SQL} "
                f"WHERE metadata_->>'project_id' = %s "
                f"AND metadata_->>'tenant_scope' = %s "
                f"AND (metadata_->>'file_path' = %s "
                f"OR metadata_->>'file_path' LIKE %s)",
                (project_id, scope, filepath, chunk_prefix),
            )
        else:
            _execute(
                f"DELETE FROM {PG_TABLE_NAME_SQL} "
                f"WHERE metadata_->>'project_id' = %s "
                f"AND (metadata_->>'file_path' = %s "
                f"OR metadata_->>'file_path' LIKE %s)",
                (project_id, filepath, chunk_prefix),
            )
    except Exception as e:
        logger.error(f"pgvector delete_by_filepath error: {e}")
        raise


def is_duplicate(content_hash: str, project_id: str, scope: str) -> bool:
    """Return True if this content hash already exists in pgvector.

    Fails open (returns False) on any error.
    """
    try:
        rows = _query_metadata(
            f"SELECT 1 FROM {PG_TABLE_NAME_SQL} "
            f"WHERE metadata_->>'project_id' = %s "
            f"AND metadata_->>'tenant_scope' = %s "
            f"AND metadata_->>'content_hash' = %s LIMIT 1",
            (project_id, scope, content_hash),
        )
        return len(rows) > 0
    except Exception as e:
        logger.warning(f"pgvector dedup check failed (fail-open): {e}")
        return False


def is_file_content_duplicate(
    file_content_hash: str, project_id: str, scope: str
) -> bool:
    """Return True if this whole-file content hash already exists in pgvector.

    Fails open (returns False) on any error.
    """
    try:
        rows = _query_metadata(
            f"SELECT 1 FROM {PG_TABLE_NAME_SQL} "
            f"WHERE metadata_->>'project_id' = %s "
            f"AND metadata_->>'tenant_scope' = %s "
            f"AND metadata_->>'file_content_hash' = %s LIMIT 1",
            (project_id, scope, file_content_hash),
        )
        return len(rows) > 0
    except Exception as e:
        logger.warning(f"pgvector file_content_hash check failed (fail-open): {e}")
        return False


def delete_all_data() -> None:
    """Delete ALL rows from the pgvector table.

    This is a destructive, irreversible operation. Use only for full resets.

    Raises:
        Exception: Propagated from psycopg2 on failure.
    """
    try:
        _execute(f"TRUNCATE {PG_TABLE_NAME_SQL}")
        logger.warning("pgvector: truncated table '%s'", PG_TABLE_NAME_SQL)
    except Exception as e:
        logger.error(f"pgvector delete_all error: {e}")
        raise


def get_document_count(project_id: str, scope: str = "") -> int:
    """Return the count of documents for a project/scope in pgvector."""
    try:
        if scope:
            rows = _query_metadata(
                f"SELECT COUNT(*) AS count FROM {PG_TABLE_NAME_SQL} "
                f"WHERE metadata_->>'project_id' = %s "
                f"AND metadata_->>'tenant_scope' = %s",
                (project_id, scope),
            )
        else:
            rows = _query_metadata(
                f"SELECT COUNT(*) AS count FROM {PG_TABLE_NAME_SQL} "
                f"WHERE metadata_->>'project_id' = %s",
                (project_id,),
            )
        return int(rows[0]["count"]) if rows else 0
    except Exception as e:
        logger.warning(f"pgvector document count error: {e}")
        return 0


def get_all_filepaths(project_id: str, scope: str = "") -> list[str]:
    """Return distinct file_path values for a project/scope in pgvector."""
    try:
        if scope:
            rows = _query_metadata(
                f"SELECT DISTINCT metadata_->>'file_path' AS value FROM {PG_TABLE_NAME_SQL} "
                f"WHERE metadata_->>'project_id' = %s "
                f"AND metadata_->>'tenant_scope' = %s "
                f"AND metadata_->>'file_path' IS NOT NULL",
                (project_id, scope),
            )
        else:
            rows = _query_metadata(
                f"SELECT DISTINCT metadata_->>'file_path' AS value FROM {PG_TABLE_NAME_SQL} "
                f"WHERE metadata_->>'project_id' = %s "
                f"AND metadata_->>'file_path' IS NOT NULL",
                (project_id,),
            )
        return [r["value"] for r in rows if r["value"]]
    except Exception as e:
        logger.warning(f"pgvector get_all_filepaths error: {e}")
        return []
