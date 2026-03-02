# Version: v1.0
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


def cache_key(query: str, project_id: str, scope: str = "") -> str:
    """
    Generate a cache key from query parameters.

    Args:
        query: The search query
        project_id: Project identifier for tenant isolation
        scope: Optional tenant scope

    Returns:
        Hashed cache key with nexus: prefix
    """
    key = f"{project_id}:{scope}:{query.lower().strip()}"
    return "nexus:" + hashlib.sha256(key.encode()).hexdigest()[:16]


def get_cached(query: str, project_id: str, scope: str = "") -> dict[str, Any] | None:
    """
    Retrieve cached query result.

    Args:
        query: The search query
        project_id: Project identifier
        scope: Optional tenant scope

    Returns:
        Cached result dict or None if not found/disabled
    """
    if not CACHE_ENABLED:
        return None

    try:
        cached = get_redis().get(cache_key(query, project_id, scope))
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
) -> bool:
    """
    Store query result in cache.

    Args:
        query: The search query
        project_id: Project identifier
        scope: Optional tenant scope
        result: Result dict to cache
        ttl: Optional TTL override (seconds)

    Returns:
        True if cached successfully, False otherwise
    """
    if not CACHE_ENABLED:
        return False

    try:
        get_redis().setex(
            cache_key(query, project_id, scope),
            ttl or CACHE_TTL,
            json.dumps(result),
        )
        logger.debug(f"Cached result for query: {query[:50]}...")
        return True
    except redis.RedisError as e:
        logger.warning(f"Redis set error: {e}")
        return False


def invalidate_cache(project_id: str, scope: str = "") -> int:
    """
    Invalidate all cache entries for a project/scope.

    Args:
        project_id: Project identifier
        scope: Optional tenant scope

    Returns:
        Number of keys deleted
    """
    if not CACHE_ENABLED:
        return 0

    try:
        pattern = f"nexus:{hashlib.sha256(f'{project_id}:{scope}:'.encode()).hexdigest()[:8]}*"
        keys = list(get_redis().scan_iter(match=pattern))
        if keys:
            return get_redis().delete(*keys)
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
