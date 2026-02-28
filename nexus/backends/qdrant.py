# Version: v1.0
"""
nexus.backends.qdrant — All Qdrant client, query, and mutation helpers.

Bug fix v1.7: QdrantClient is cached per URL via get_client() to avoid
creating a new connection on every helper call (scroll, delete, dedup).
"""
import logging
import threading
from typing import Optional

import qdrant_client
from qdrant_client.http import models as qdrant_models

from nexus.config import (
    DEFAULT_QDRANT_URL,
    COLLECTION_NAME,
    ALLOWED_META_KEYS,
)

logger = logging.getLogger("mcp-nexus-rag")

# ---------------------------------------------------------------------------
# Client cache — one client instance per URL for the process lifetime
# ---------------------------------------------------------------------------
_client_cache: dict[str, qdrant_client.QdrantClient] = {}
_client_lock = threading.Lock()


def get_client(url: str = DEFAULT_QDRANT_URL) -> qdrant_client.QdrantClient:
    """Return a cached QdrantClient for *url*, creating one on first call.

    Args:
        url: Qdrant service URL.

    Returns:
        Shared QdrantClient instance.
    """
    if url not in _client_cache:
        with _client_lock:
            if url not in _client_cache:  # double-checked
                _client_cache[url] = qdrant_client.QdrantClient(url=url)
    return _client_cache[url]


def scroll_field(
    key: str,
    qdrant_filter: Optional[qdrant_models.Filter] = None,
) -> set[str]:
    """Scroll the entire COLLECTION_NAME and collect distinct values for *key*.

    Args:
        key: Payload field name to collect.
        qdrant_filter: Optional filter to restrict which points are scanned.

    Returns:
        Set of unique string values found in the payload.
    """
    values: set[str] = set()
    client = get_client()
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=qdrant_filter,
            limit=1000,
            with_payload=[key],
            offset=offset,
        )
        for record in records:
            if record.payload and key in record.payload:
                values.add(record.payload[key])
        if offset is None:
            break
    return values


def get_distinct_metadata(key: str) -> list[str]:
    """Return distinct payload values for *key* across the Qdrant collection.

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
        return list(scroll_field(key))
    except Exception as e:
        logger.warning(f"Qdrant distinct '{key}' error: {e}")
        return []


def delete_data(project_id: str, scope: str = "") -> None:
    """Delete Qdrant points matching project_id (and optionally scope).

    Args:
        project_id: Tenant project ID to target.
        scope: If non-empty, restricts deletion to this tenant_scope.

    Raises:
        Exception: Propagated from the Qdrant client on failure.
    """
    try:
        client = get_client()
        must_conditions: list = [
            qdrant_models.FieldCondition(
                key="project_id",
                match=qdrant_models.MatchValue(value=project_id),
            )
        ]
        if scope:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="tenant_scope",
                    match=qdrant_models.MatchValue(value=scope),
                )
            )
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=qdrant_models.FilterSelector(
                filter=qdrant_models.Filter(must=must_conditions)
            ),
        )
    except Exception as e:
        logger.error(f"Qdrant delete error: {e}")
        raise


def is_duplicate(content_hash: str, project_id: str, scope: str) -> bool:
    """Return True if this content hash already exists in Qdrant.

    Fails open (returns False) on any error so ingestion is never
    silently blocked by a connectivity issue.

    Args:
        content_hash: SHA-256 hex digest from nexus.dedup.content_hash().
        project_id: Tenant project ID.
        scope: Tenant scope.

    Returns:
        True if a duplicate was found, False otherwise.
    """
    try:
        client = get_client()
        records, _ = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="project_id",
                        match=qdrant_models.MatchValue(value=project_id),
                    ),
                    qdrant_models.FieldCondition(
                        key="tenant_scope",
                        match=qdrant_models.MatchValue(value=scope),
                    ),
                    qdrant_models.FieldCondition(
                        key="content_hash",
                        match=qdrant_models.MatchValue(value=content_hash),
                    ),
                ]
            ),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        return len(records) > 0
    except Exception as e:
        logger.warning(f"Qdrant dedup check failed (fail-open): {e}")
        return False
