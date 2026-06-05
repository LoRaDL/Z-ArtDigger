"""
CrawlerTaskPool — Redis Sorted Set backed priority queue for crawler tasks.

Falls back to an in-memory heapq when Redis is unavailable.
"""
from __future__ import annotations

import dataclasses
import heapq
import itertools
import json
import logging
import threading

import redis

from core.models import Task

logger = logging.getLogger(__name__)
REDIS_KEY = "crawler:tasks"
_counter = itertools.count()


class CrawlerTaskPool:
    """
    Priority queue backed by a Redis Sorted Set.
    Falls back to in-memory heapq when Redis is unavailable (Req 7.4, 7.6).
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0", task_score_threshold: float = 0.0) -> None:
        self._redis_url = redis_url
        self._task_score_threshold = task_score_threshold
        self._redis: redis.Redis | None = None
        self._fallback: list = []          # heapq: (-score, seq, task_json)
        self._fallback_lock = threading.Lock()
        self._use_redis = True
        self._connect()

    @property
    def redis(self) -> redis.Redis | None:
        """Expose the internal redis client for other components."""
        return self._redis

    def _connect(self) -> None:
        try:
            r = redis.from_url(self._redis_url, decode_responses=True,
                               socket_connect_timeout=2)
            r.ping()
            self._redis = r
            self._use_redis = True
            logger.info("CrawlerTaskPool: using Redis at %s", self._redis_url)
        except Exception as exc:
            logger.warning(
                "CrawlerTaskPool: Redis unavailable (%s), falling back to in-memory queue.", exc
            )
            self._redis = None
            self._use_redis = False

    @staticmethod
    def _serialize(task: Task) -> str:
        return json.dumps(dataclasses.asdict(task))

    @staticmethod
    def _deserialize(data: str) -> Task:
        return Task(**json.loads(data))

    def put(self, task: Task) -> None:
        if task.score < self._task_score_threshold:
            logger.info(
                "CrawlerTaskPool: Discard task with score %.3f (below threshold %.3f): %s",
                task.score, self._task_score_threshold, task
            )
            return

        if self._use_redis and self._redis is not None:
            try:
                self._redis.zadd(REDIS_KEY, {self._serialize(task): task.score})
                return
            except Exception as exc:
                logger.warning("CrawlerTaskPool: Redis put failed (%s), switching to memory.", exc)
                self._use_redis = False
        with self._fallback_lock:
            heapq.heappush(self._fallback,
                           (-task.score, next(_counter), self._serialize(task)))

    def get(self) -> Task | None:
        if self._use_redis and self._redis is not None:
            try:
                results = self._redis.zpopmax(REDIS_KEY, count=1)
                if not results:
                    return None
                task_json, _score = results[0]
                return self._deserialize(task_json)
            except Exception as exc:
                logger.warning("CrawlerTaskPool: Redis get failed (%s), switching to memory.", exc)
                self._use_redis = False
        with self._fallback_lock:
            if not self._fallback:
                return None
            _, _, task_json = heapq.heappop(self._fallback)
            return self._deserialize(task_json)

    def is_empty(self) -> bool:
        if self._use_redis and self._redis is not None:
            try:
                return self._redis.zcard(REDIS_KEY) == 0
            except Exception:
                self._use_redis = False
        with self._fallback_lock:
            return len(self._fallback) == 0
