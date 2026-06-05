"""
APIRequestPool — 串行化所有 X GraphQL API 请求，按 score 优先级排序。

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import json
import random
from curl_cffi import requests
from dataclasses import dataclass, field
from typing import Any

from fetcher.api import fetch_tweets_from, TwitterRateLimitError

logger = logging.getLogger(__name__)

# 根据需求改为 20
API_FETCH_SIZE = 20

# HTTP 429 等待时间（秒），≥15 分钟
RATE_LIMIT_WAIT_SECONDS = 5 * 60

# HTTP 5xx 指数退避基础等待时间（秒）
BACKOFF_BASE_SECONDS = 5

# HTTP 5xx 最大重试次数
MAX_5XX_RETRIES = 3

# 连续失败告警阈值（默认 5 次）
DEFAULT_CONSECUTIVE_FAILURE_THRESHOLD = 5

# 请求间隔（秒）
REQUEST_INTERVAL_SECONDS = 2.0


@dataclass
class APIRequest:
    """封装一次 API 请求所需的参数。"""
    author: str          # screen_name
    from_id: int         # 起点 ID (从此 ID 往更旧扫)
    session: Any = None  # Optional session, for backward compatibility


@dataclass(order=True)
class _PrioritizedItem:
    """PriorityQueue 内部条目，按 priority 升序（用负 score 实现降序）。"""
    priority: float                          # -score，越小越优先
    seq: int                                 # 插入序号，打破 priority 相同时的平局
    request: APIRequest = field(compare=False)
    result_holder: list = field(compare=False)   # [result] or [exception]
    event: threading.Event = field(compare=False)


class APIRequestPool:
    """
    串行化所有 X API 请求的优先级请求池。

    - 单 worker 线程，`queue.PriorityQueue` 按 score 降序排列请求（Req 8.1, 8.3）
    - submit() 阻塞直到 worker 完成，返回原始推文列表（Req 8.1）
    - 固定使用 count=100（API_FETCH_SIZE）（Req 8.4）
    - HTTP 429：等待 ≥15 分钟后重试（Req 8.2）
    - HTTP 5xx：指数退避最多 3 次
    - 连续失败超阈值：暂停并告警（Req 8.5）
    """

    def __init__(
        self,
        session_pool: CookieSessionPool,
        consecutive_failure_threshold: int = DEFAULT_CONSECUTIVE_FAILURE_THRESHOLD,
        redis_client: Any = None,
    ) -> None:
        self._session_pool = session_pool
        self._q: queue.PriorityQueue[_PrioritizedItem] = queue.PriorityQueue()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._consecutive_failures = 0
        self._threshold = consecutive_failure_threshold
        self._paused = False
        self._redis = redis_client

        self._stop_event = threading.Event()
        self._worker_thread = threading.Thread(
            target=self._worker,
            name="APIRequestPool-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def stop(self) -> None:
        """发出停止信号。"""
        self._stop_event.set()
        logger.info("APIRequestPool stop signal set.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(self, request: APIRequest, score: float) -> list[dict]:
        """
        将请求放入优先级队列，阻塞直到 worker 完成，返回原始推文列表。

        Requirement 8.1: 串行化保证（单 worker 线程）
        Requirement 8.3: 按 score 降序排列
        """
        with self._seq_lock:
            seq = self._seq
            self._seq += 1

        event = threading.Event()
        result_holder: list = []
        item = _PrioritizedItem(
            priority=-score,   # 负 score → 最大堆行为
            seq=seq,
            request=request,
            result_holder=result_holder,
            event=event,
        )
        self._q.put(item)
        logger.debug("[API] Queued request: @%s from=%d score=%.3f (seq=%d)",
                     request.author, request.from_id, score, seq)
        
        # 轮询等待，直到收到结果或池被停止
        while not event.is_set() and not self._stop_event.is_set():
            event.wait(timeout=0.5)

        if result_holder and isinstance(result_holder[0], BaseException):
            raise result_holder[0]
        return result_holder[0] if result_holder else []

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        """后台线程：串行执行请求，处理 429 重试和 5xx 退避。"""
        while not self._stop_event.is_set():
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                result = self._execute_with_retry(item.request)
                self._consecutive_failures = 0
                self._paused = False
                item.result_holder.append(result)
            except Exception as exc:
                self._consecutive_failures += 1
                logger.error(
                    "APIRequestPool: request failed (consecutive=%d): %s",
                    self._consecutive_failures,
                    exc,
                )
                err_msg = str(exc).lower()
                if "locked" in err_msg or "authorized" in err_msg:
                    self._paused = True
                    logger.critical("APIRequestPool: 检测到账号异常（可能已锁定或授权失效）: %s。系统已自动暂停，请检查 cookies 或账号状态。", exc)
                elif self._consecutive_failures >= self._threshold:
                    self._paused = True
                    logger.critical(
                        "APIRequestPool: 连续失败 %d 次，超过阈值 %d，暂停所有请求，等待人工干预。",
                        self._consecutive_failures,
                        self._threshold,
                    )
                item.result_holder.append(exc)
            finally:
                item.event.set()
                self._report_status_queue()
                # 引入随机抖动 (Jitter)，避免固定的请求节奏，降低指纹特征
                jitter = REQUEST_INTERVAL_SECONDS * (0.8 + 0.4 * random.random())
                time.sleep(jitter)

    def _report_status_queue(self) -> None:
        if self._redis:
            try:
                self._redis.set("artdigger:status:api_queue", self._q.qsize())
            except Exception:
                pass

    def _execute_with_retry(self, request: APIRequest) -> list[dict]:
        """
        执行单次 API 请求，处理 429 和 5xx 错误。

        - HTTP 429：等待 RATE_LIMIT_WAIT_SECONDS 后重试（无限次，Req 8.2）
        - HTTP 5xx：指数退避，最多 MAX_5XX_RETRIES 次
        """
        retries_5xx = 0
        session = self._session_pool.get_session()

        while True:
            # 如果处于暂停状态，等待直到恢复（人工干预后重置 _paused）
            if self._paused:
                logger.warning("APIRequestPool: 处于暂停状态，等待人工干预...")
                time.sleep(60)
                continue

            if not session:
                logger.critical("APIRequestPool: 没有可用的 session，暂停所有请求...")
                self._paused = True
                time.sleep(10)
                continue

            try:
                raw_tweets = fetch_tweets_from(
                    session=session,
                    screen_name=request.author,
                    from_id=request.from_id,
                    count=API_FETCH_SIZE,
                )
                
                # 报告速率分配信息到 Redis
                if hasattr(session, "_last_rate_limit"):
                    self._report_rate_limit(session, session._last_rate_limit)

                self._report_status_worker(request.author, f"Fetching from {request.from_id}")
                logger.info(
                    "[API] ✓ @%-20s  from=%-20d  got=%d tweets using %s",
                    request.author, request.from_id,
                    len(raw_tweets),
                    self._session_pool.session_names.get(session, "unknown")
                )
                return raw_tweets

            except TwitterRateLimitError as exc:
                self._report_rate_limit(session, {"remaining": exc.remaining, "reset": exc.reset, "limit": exc.limit})
                
                # 标记当前 session 的限速时间
                self._session_pool.mark_rate_limited(session, exc.reset + 5)
                
                if self._session_pool.has_available_session():
                    logger.warning(
                        "APIRequestPool: Session %s 速率限制。正在切换到另一个活跃会话...",
                        self._session_pool.session_names.get(session, "unknown")
                    )
                    session = self._session_pool.get_session()
                    retries_5xx = 0
                    continue
                else:
                    wait_time = int(self._session_pool.get_earliest_wait_time())
                    logger.warning(
                        "APIRequestPool: 所有 Session 均被速率限制，等待 %d 秒...",
                        wait_time,
                    )
                    time.sleep(wait_time)
                    session = self._session_pool.get_session()
                    retries_5xx = 0
                    continue

            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code
                self._report_status_worker(request.author, f"Error {status}")

                err_msg = str(exc).lower() + " " + getattr(exc.response, 'text', '').lower()
                if status in (401, 403) or "locked" in err_msg or "authorized" in err_msg:
                    self._session_pool.remove_session(session)
                    self._report_rate_limit(None, {})
                    if self._session_pool.is_empty():
                        self._paused = True
                        logger.critical("APIRequestPool: 所有 Session 均已失效/被封禁。")
                        raise
                    session = self._session_pool.get_session()
                    retries_5xx = 0
                    continue

                if status == 429:
                    # 尝试从 Header 获取精确重置时间
                    rst = exc.response.headers.get("x-rate-limit-reset")
                    lmt = exc.response.headers.get("x-rate-limit-limit")
                    reset_ts = int(rst) if rst else int(time.time() + RATE_LIMIT_WAIT_SECONDS)
                    
                    self._report_rate_limit(session, {
                        "remaining": 0,
                        "reset": reset_ts,
                        "limit": int(lmt) if lmt else 180
                    })

                    self._session_pool.mark_rate_limited(session, reset_ts + 5)
                    
                    if self._session_pool.has_available_session():
                        logger.warning(
                            "APIRequestPool: Session %s HTTP 429 速率限制。正在切换至另一个活跃会话...",
                            self._session_pool.session_names.get(session, "unknown")
                        )
                        session = self._session_pool.get_session()
                        retries_5xx = 0
                        continue
                    else:
                        wait_time = int(self._session_pool.get_earliest_wait_time())
                        logger.warning(
                            "APIRequestPool: 所有 Session 均被速率限制，等待 %d 秒后重试...",
                            wait_time,
                        )
                        time.sleep(wait_time)
                        session = self._session_pool.get_session()
                        retries_5xx = 0
                        continue

                elif 500 <= status < 600:
                    retries_5xx += 1
                    if retries_5xx > MAX_5XX_RETRIES:
                        logger.error(
                            "APIRequestPool: HTTP %d，已重试 %d 次，放弃。",
                            status,
                            MAX_5XX_RETRIES,
                        )
                        raise
                    wait = BACKOFF_BASE_SECONDS * (2 ** (retries_5xx - 1))
                    logger.warning(
                        "APIRequestPool: HTTP %d，第 %d/%d 次重试，等待 %d 秒...",
                        status,
                        retries_5xx,
                        MAX_5XX_RETRIES,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                else:
                    raise

            except Exception as exc:
                err_msg = str(exc).lower()
                if "locked" in err_msg or "authorized" in err_msg or "unauthorized" in err_msg:
                    self._session_pool.remove_session(session)
                    if self._session_pool.is_empty():
                        self._paused = True
                        logger.critical("APIRequestPool: 所有 Session 均已失效/被封禁。")
                        raise
                    session = self._session_pool.get_session()
                    retries_5xx = 0
                    continue

                retries_5xx += 1  # 借用 5xx 的计数逻辑或独立计数，此处简单处理
                if retries_5xx > MAX_5XX_RETRIES:
                    logger.error("APIRequestPool: 异常已达重试上限 (%d/%d): %s", 
                                 retries_5xx - 1, MAX_5XX_RETRIES, exc)
                    raise
                
                wait = BACKOFF_BASE_SECONDS * (2 ** (retries_5xx - 1))
                logger.warning("APIRequestPool: 遇到非 HTTP 错误 (%s)，进行第 %d/%d 次重试，等待 %d 秒...",
                               exc, retries_5xx, MAX_5XX_RETRIES, wait)
                time.sleep(wait)
                continue
            finally:
                self._report_status_worker(request.author, None)

    def _report_rate_limit(self, session: Any, info: dict) -> None:
        if self._redis:
            try:
                all_limits = {}
                for s in self._session_pool.sessions:
                    name = self._session_pool.session_names.get(s)
                    if not name:
                        continue
                    
                    limit_info = getattr(s, "_last_rate_limit", {}).copy()
                    if not limit_info:
                        limit_info = {
                            "remaining": 180,
                            "limit": 180,
                            "reset": 0,
                            "endpoint": "N/A"
                        }
                    
                    if s == session:
                        limit_info.update(info)
                    
                    # 同步速率限制重置时间
                    reset_time = int(self._session_pool.rate_limit_resets.get(s, 0.0))
                    if reset_time > time.time():
                        limit_info["reset"] = reset_time
                        
                    all_limits[name] = limit_info
                
                self._redis.set("artdigger:status:api_limit", json.dumps(all_limits))
            except Exception:
                pass

    def _report_status_worker(self, author: str, detail: str | None) -> None:
        if self._redis:
            try:
                ident = threading.get_ident()
                key = "artdigger:status:workers"
                if detail:
                    author_str = author
                    if isinstance(author, dict):
                        author_str = author.get("screen_name") or author.get("name") or str(author)
                        
                    data = {"type": "API", "author": str(author_str), "detail": detail}
                    self._redis.hset(key, str(ident), json.dumps(data))
                else:
                    existing = self._redis.hget(key, str(ident))
                    if existing:
                        data = json.loads(existing)
                        data["detail"] = "Idle"
                        self._redis.hset(key, str(ident), json.dumps(data))
            except Exception:
                pass
