# Version: v1.2
"""
nexus.cache — Semantic caching for repeated LLM queries.

Uses Redis with LRU eviction to cache query results, reducing redundant
LLM inference for semantically similar queries.
"""

import hashlib
import json
import os
import threading
from typing import Any

import redis

from nexus.config import logger

# ---------------------------------------------------------------------------
# Redis configuration
# ---------------------------------------------------------------------------
DEFAULT_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "86400"))  # 24 hours default
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "true").lower() != "false"

# ---------------------------------------------------------------------------
# Thread-safe Redis client singleton
# ---------------------------------------------------------------------------
_client: redis.Redis | None = None
_lock = threading.Lock()


def get_redis() -> redis.Redis:
    """
    Get or create a thread-safe Redis client singleton.

    Returns:
        Redis client instance
    """
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = redis.from_url(DEFAULT_REDIS_URL, decode_responses=True)
    return _client


def _idx_key(project_id: str, scope: str) -> str:
    """Secondary index key for tracking cache keys by (project_id, scope).

    Used by set_cached / invalidate_cache so that all cache entries for a
    given tenant can be found and purged without scanning the full keyspace.

    Args:
        project_id: Tenant project ID.
        scope: Tenant scope (empty string means "all scopes" queries).

    Returns:
        A Redis key of the form ``nexus:idx:{project_id}:{scope}``.
    """
    safe_pid = project_id.replace(":", "_")
    safe_scope = scope.replace(":", "_") if scope else "__all__"
    return f"nexus:idx:{safe_pid}:{safe_scope}"


def cache_key(query: str, project_id: str, scope: str = "", tool_type: str = "") -> str:
    """
    Generate a cache key from query parameters.

    Args:
        query: The search query
        project_id: Project identifier for tenant isolation
        scope: Optional tenant scope
        tool_type: Tool discriminator (e.g. "graph", "vector", "answer") —
            prevents collisions when graph and vector queries share the same
            (query, project_id, scope) triple.

    Returns:
        Hashed cache key with nexus: prefix
    """
    key = f"{tool_type}:{project_id}:{scope}:{query.lower().strip()}"
    return "nexus:" + hashlib.sha256(key.encode()).hexdigest()[:16]


def get_cached(
    query: str, project_id: str, scope: str = "", tool_type: str = ""
) -> dict[str, Any] | None:
    """
    Retrieve cached query result.

    Args:
        query: The search query
        project_id: Project identifier
        scope: Optional tenant scope
        tool_type: Tool discriminator — must match the value used in set_cached.

    Returns:
        Cached result dict or None if not found/disabled
    """
    if not CACHE_ENABLED:
        return None

    try:
        cached = get_redis().get(cache_key(query, project_id, scope, tool_type))
        if cached:
            logger.debug(f"Cache hit for query: {query[:50]}...")
            return json.loads(cached)
    except redis.RedisError as e:
        logger.warning(f"Redis get error: {e}")

    return None


def set_cached(
    query: str,
    project_id: str,
    scope: str,
    result: dict[str, Any],
    ttl: int | None = None,
    tool_type: str = "",
) -> bool:
    """
    Store query result in cache.

    Args:
        query: The search query
        project_id: Project identifier
        scope: Optional tenant scope
        result: Result dict to cache
        ttl: Optional TTL override (seconds)
        tool_type: Tool discriminator — must match the value used in get_cached.

    Returns:
        True if cached successfully, False otherwise
    """
    if not CACHE_ENABLED:
        return False

    try:
        key = cache_key(query, project_id, scope, tool_type)
        effective_ttl = ttl or CACHE_TTL
        r = get_redis()
        r.setex(key, effective_ttl, json.dumps(result))
        # Secondary index: track key by (project_id, scope) for invalidation
        try:
            idx = _idx_key(project_id, scope)
            r.sadd(idx, key)
            # Keep index alive slightly longer than the cached values
            r.expire(idx, effective_ttl + 3600)
        except redis.RedisError as idx_err:
            logger.warning(f"Redis secondary index update error: {idx_err}")
        logger.debug(f"Cached result for query: {query[:50]}...")
        return True
    except redis.RedisError as e:
        logger.warning(f"Redis set error: {e}")
        return False


def invalidate_cache(project_id: str, scope: str = "") -> int:
    """Invalidate all cache entries for a project/scope.

    Uses the secondary index maintained by :func:`set_cached` to find all
    cache keys for the given tenant without scanning the full keyspace.

    When *scope* is provided, also invalidates "all scopes" cached queries
    (``scope=""``) for the same project, since adding content to any scope
    makes cross-scope results stale too.

    Args:
        project_id: Project identifier.
        scope: Optional tenant scope. Empty string invalidates only cross-scope
            (all-scopes) queries for the project.

    Returns:
        Total number of Redis keys deleted (cache entries + index keys).
    """
    if not CACHE_ENABLED:
        return 0

    try:
        r = get_redis()
        cache_keys_to_delete: set[str] = set()
        idx_keys_to_delete: set[str] = set()

        # Collect cache keys for this specific scope
        idx = _idx_key(project_id, scope)
        scope_keys = r.smembers(idx)
        cache_keys_to_delete.update(scope_keys)
        idx_keys_to_delete.add(idx)

        # Also invalidate "all scopes" queries (scope="") for this project
        # since adding content to any scope makes those stale too
        if scope:
            all_idx = _idx_key(project_id, "")
            all_keys = r.smembers(all_idx)
            cache_keys_to_delete.update(all_keys)
            idx_keys_to_delete.add(all_idx)

        all_to_delete = cache_keys_to_delete | idx_keys_to_delete
        if all_to_delete:
            deleted = r.delete(*all_to_delete)
            logger.debug(
                f"Cache invalidated: project={project_id} scope={scope!r} "
                f"deleted={deleted} keys"
            )
            return deleted
    except redis.RedisError as e:
        logger.warning(f"Redis invalidate error: {e}")

    return 0


def cache_stats() -> dict[str, Any]:
    """
    Get cache statistics.

    Returns:
        Dict with cache stats (keys, memory, etc.)
    """
    try:
        client = get_redis()
        info = client.info("memory")
        keyspace = client.info("keyspace")

        nexus_keys = len(list(client.scan_iter(match="nexus:*", count=1000)))

        return {
            "enabled": CACHE_ENABLED,
            "ttl_seconds": CACHE_TTL,
            "nexus_keys": nexus_keys,
            "used_memory_human": info.get("used_memory_human", "unknown"),
            "keyspace": keyspace,
        }
    except redis.RedisError as e:
        return {"enabled": CACHE_ENABLED, "error": str(e)}
