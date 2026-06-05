"""
ClassificationCacheDB — Redis-backed L0 cache for tweet classification results.

Key format: classify:cache:<tweet_id>
Value: "related" | "unrelated"

When Redis is unavailable, all operations degrade gracefully (no exceptions raised).
"""
from __future__ import annotations

import redis


class ClassificationCacheDB:
    """
    L0 classification cache backed by Redis strings.

    Each tweet_id maps to a single classification result stored at
    key ``classify:cache:<tweet_id>``.

    Redis unavailability is handled silently: ``get`` returns None and
    ``set`` is a no-op, allowing the caller to fall through to L1/L2/L3.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0") -> None:
        self._redis = redis.from_url(redis_url, decode_responses=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _key(tweet_id: str | int) -> str:
        return f"classify:cache:{tweet_id}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, tweet_id: str | int) -> str | None:
        """
        Return the cached classification result for *tweet_id*, or None if
        not cached or Redis is unavailable.
        """
        try:
            return self._redis.get(self._key(tweet_id))
        except (redis.exceptions.RedisError, ConnectionError):
            return None

    def set(self, tweet_id: str | int, result: str) -> None:
        """
        Store *result* ("related" | "unrelated") for *tweet_id*.
        Silently skips if Redis is unavailable.
        """
        try:
            self._redis.set(self._key(tweet_id), result)
        except (redis.exceptions.RedisError, ConnectionError):
            pass
