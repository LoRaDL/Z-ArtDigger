"""
DBFiller — 核心规划与填充组件。
负责执行 FillRequest，维持孤岛接续。
"""
from __future__ import annotations

import logging
import threading
import time
import concurrent.futures
import json
from queue import PriorityQueue
from typing import TYPE_CHECKING, Any

from fetcher.cursor import snowflake_to_timestamp, timestamp_to_snowflake
from storage.timeline.fill_request import FillRequest
from storage.timeline.aggregator import aggregate_articles
from io_layer.api_pool import APIRequest

if TYPE_CHECKING:
    from storage.timeline_db import TimelineIslandsDB
    from io_layer.api_pool import APIRequestPool as APIPool

logger = logging.getLogger(__name__)


class DBFiller:
    """
    负责从优先队列取出 FillRequest 并执行 API 抓取入库。
    核心逻辑：架桥循环 (Bridge Cycle)。
    """

    def __init__(self, db: TimelineIslandsDB, api_pool: APIPool, session_pool: Any = None, num_workers: int = 2) -> None:
        self._db = db
        self._api_pool = api_pool
        self._session_pool = session_pool
        self._queue: PriorityQueue[tuple[float, FillRequest]] = PriorityQueue()
        self._pending_requests: list[FillRequest] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=num_workers, thread_name_prefix="DBFiller"
        )
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def submit(self, req: FillRequest) -> None:
        """提交 FillRequest 到队列。"""
        with self._lock:
            self._pending_requests.append(req)
        
        with self._seq_lock:
            seq = self._seq
            self._seq += 1
            
        # PriorityQueue 默认升序，Score 越大优先级越高，故取负
        self._queue.put((-req.score, seq, req))

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                # 阻塞获取任务
                item = self._queue.get(timeout=1.0)
                if item is None: continue
                _, seq, req = item
            except:
                continue

            try:
                logger.info(f"Dispatching FillRequest: {req.author} anchor={req.anchor_id}")
                self._executor.submit(self._safe_process_request, req)
            except Exception as e:
                logger.error(f"Error dispatching FillRequest: {e}")

    def _safe_process_request(self, req: FillRequest) -> None:
        """多线程安全的处理包装类。"""
        try:
            self._report_status(req.author, f"Gap near {req.anchor_id}")
            self._process_request(req)
        except Exception as e:
            logger.error(f"Error processing FillRequest for {req.author}: {e}", exc_info=True)
        finally:
            self._report_status(req.author, None)
            req.event.set()
            with self._lock:
                if req in self._pending_requests:
                    self._pending_requests.remove(req)
            self._check_satisfied_all()
            try:
                self._queue.task_done()
            except ValueError:
                pass

    def _process_request(self, req: FillRequest) -> None:
        """执行单一请求的处理逻辑。"""
        if req.is_satisfied(self._db):
            req.event.set()
            return

        island = self._db.find_island(req.author, req.anchor_id)
        
        # 初始点植入：如果 anchor_id 不在任何孤岛中，先尝试精确抓取该推文
        if island is None:
            logger.info(f"Filler: {req.author} anchor {req.anchor_id} not found. Seeding initial point...")
            self._seed_anchor(req.author, req.anchor_id)
            island = self._db.find_island(req.author, req.anchor_id)

        # 向旧扩展
        if req.direction < 0:
            logger.info(f"Filler: {req.author} expanding older from {req.anchor_id}...")
            from_id = island.min_id if island else req.anchor_id
            self._scan_older_once(req.author, from_id, score=req.score)
        else:
            # 向新扩展
            logger.info(f"Filler: {req.author} bridging newer from {req.anchor_id}...")
            self._bridge_v2(req)

    def _seed_anchor(self, author: str, tweet_id: int) -> None:
        """从 API 精确获取一条推文并入库，用于建立初始孤岛。"""
        from fetcher.api import fetch_tweet_by_id_raw
        try:
            session = self._session_pool.get_session() if self._session_pool else None
            raw = fetch_tweet_by_id_raw(session, tweet_id)
            if raw:
                articles = aggregate_articles([raw])
                self._db.insert(author, articles, extend_to=tweet_id)
                logger.info(f"Filler: seeded anchor {tweet_id} successfully.")
            else:
                logger.warning(f"Filler: seed anchor {tweet_id} returned no data.")
        except Exception as e:
            logger.warning(f"Filler: failed to seed anchor {tweet_id}: {e}")

    def _scan_older_once(self, author: str, from_id: int, score: float = 0.5) -> None:
        """执行一次 API 向旧抓取并入库。"""
        req_obj = APIRequest(author=author, from_id=from_id)
        raw_tweets = self._api_pool.submit(req_obj, score=score)
        if not raw_tweets:
            logger.info(f"Filler: {author} scan older from {from_id} got 0 tweets. Marking oldest boundary.")
            self._db.insert(author, [], oldest_boundary=True, extend_to=from_id)
            return

        articles = aggregate_articles(raw_tweets)
        logger.info(f"Filler: {author} scan older from {from_id} got {len(articles)} articles.")
        
        # 优化：废弃高开销的 proactive verify 逻辑。
        # 1. 之前的设计在 len(raw_tweets) < 20 时会触发一次额外的 API 请求，验证是否为最旧。
        #    这会导致过滤了 Retweet 的时间线上几乎每次请求都翻倍，严重浪费 precious API 配额。
        # 2. 我们通过自然的分页迭代解决：如果确实已经到头，下一次请求就会返回 0 个 tweets，
        #    从而自然地在上面的 `not raw_tweets` 分支中把 oldest_boundary 标为 True。
        # 这样既避免了冗余的 API 验证请求，又安全地防止了 20 整数倍博主 timeline 或 0 tweets 时的死循环问题。
        self._db.insert(author, articles, oldest_boundary=False, extend_to=from_id)

    def _bridge_v2(self, req: FillRequest) -> None:
        """向新扫描的架桥逻辑。"""
        # 1. 估算偏移
        avg_sec = self._db.get_avg_post_interval(req.author) or (1 * 3600)
        # 1.5 倍冗余系数
        offset_sec = avg_sec * req.count * 1.5
        target_ts = snowflake_to_timestamp(req.anchor_id) + offset_sec
        
        # 确保预测时间点不会超过当前系统时间点
        now = time.time()
        if target_ts > now:
            target_ts = now
            
        start_id = timestamp_to_snowflake(target_ts)
        
        # 优化：防跨岛盲跳感知 (Island-Aware Bridging)
        # 寻找在当前 anchor_id 新方向上最近的下一个孤岛
        all_islands = self._db.get_island_ranges(req.author)
        next_island = next((isl for isl in all_islands if isl.min_id > req.anchor_id), None)
        if next_island:
            nearest_next_min_id = next_island.min_id
            # 如果预测的起跳点越过了下一个孤岛的下边界，直接将起跳点收缩到该边界，避免跨岛重复拉取
            if start_id > nearest_next_min_id:
                logger.info(
                    "Filler: %s bridging newer detected next island starting at %d. "
                    "Shifting start_id from %d to %d to avoid redundant queries.",
                    req.author, nearest_next_min_id, start_id, nearest_next_min_id
                )
                start_id = nearest_next_min_id
        
        MAX_BRIDGE = 5
        cursor = start_id
        hit_newest = False
        for _ in range(MAX_BRIDGE):
            req_obj = APIRequest(author=req.author, from_id=cursor)
            raw = self._api_pool.submit(req_obj, score=req.score)
            if not raw:
                # 无法继续向上建立连接
                hit_newest = True
                break
            
            articles = aggregate_articles(raw)
            
            # 使用 extend_to=cursor 确保孤岛边界能顶到我们的扫描起跳点
            self._db.insert(req.author, articles, newest_checked_at=0.0, extend_to=cursor)
            
            # 检查是否接续
            if req.is_satisfied(self._db):
                break
            
            cursor = int(min(t['rest_id'] for t in raw))
            # 防止死循环：如果 cursor 已经扫到 anchor_id 之前了，也该停
            if cursor <= req.anchor_id:
                break
        else:
            if not req.is_satisfied(self._db):
                hit_newest = True
                
        if hit_newest:
            # 标记最新端已探索，避免反复无效扫描
            self._db.insert(req.author, [], newest_checked_at=time.time(), extend_to=req.anchor_id)

    def _check_satisfied_all(self) -> None:
        """遍历所有 pending 任务，如果已满足则唤醒。"""
        with self._lock:
            satisfied = [r for r in self._pending_requests if r.is_satisfied(self._db)]
            for r in satisfied:
                r.event.set()
                if r in self._pending_requests:
                    self._pending_requests.remove(r)

    def _report_status(self, author: str, detail: str | None) -> None:
        try:
            r = getattr(self._api_pool, '_redis', None)
            if not r: return
            ident = threading.get_ident()
            key = "artdigger:status:workers"
            if detail:
                data = {"type": "Filler", "author": author, "detail": detail}
                r.hset(key, str(ident), json.dumps(data))
            else:
                existing = r.hget(key, str(ident))
                if existing:
                    data = json.loads(existing)
                    data["detail"] = "Idle"
                    r.hset(key, str(ident), json.dumps(data))
        except Exception:
            pass

    def stop(self) -> None:
        self._stop_event.set()
        self._executor.shutdown(wait=False)
        self._worker_thread.join(timeout=2.0)
