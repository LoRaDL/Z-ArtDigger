"""
Crawler — 主控制器

驱动整个 fanart-crawler 系统：
  - start(initial_task)  将初始 Task 放入 CrawlerTaskPool
  - run()                主循环，多线程执行 process_task
  - process_task(task)   完整处理流程（见 design.md 核心数据流）
"""
from __future__ import annotations

import logging
import threading
import concurrent.futures
import json
import atexit
import time
from pathlib import Path

from core.config import CrawlerConfig
from core.models import Task
from core.task_pool import CrawlerTaskPool
from storage.scan_db import ScanProgressDB
from storage.timeline_db import TimelineIslandsDB
from storage.timeline.query import fetch_near_articles, shift_artwork_id
from storage.timeline.db_filler import DBFiller
from utils.article_utils import split_quote_article
from brain.classifier import ClassifierPool
from io_layer.archiver import archive_pic
from io_layer.api_pool import APIRequestPool, APIRequest

logger = logging.getLogger(__name__)


class Crawler:
    """
    主控制器。

    依赖注入所有外部组件，便于测试。
    """

    def __init__(
        self,
        config: CrawlerConfig,
        task_pool: CrawlerTaskPool,
        scan_db: ScanProgressDB,
        timeline_db: TimelineIslandsDB,
        classifier_pool: ClassifierPool,
        api_pool: APIRequestPool,
        session_pool, # CookieSessionPool
        num_workers: int = 2,
    ) -> None:
        self._config = config
        self._task_pool = task_pool
        self._scan_db = scan_db
        self._timeline_db = timeline_db
        self._classifier_pool = classifier_pool
        self._api_pool = api_pool
        self._session_pool = session_pool
        self._num_workers = num_workers
        self._stop_event = threading.Event()
        # 初始化新组件 DBFiller，使用与 Crawler 相同的并发规模（或固定为 2)
        self._filler = DBFiller(self._timeline_db, self._api_pool, self._session_pool, num_workers=num_workers)

        # 清理旧状态并启动日志捕获
        self._setup_logging_and_cleanup()

    def _setup_logging_and_cleanup(self) -> None:
        r = getattr(self._task_pool, 'redis', None)
        if r:
            r.delete("artdigger:status:workers")  # 清空旧 Worker
            r.delete("artdigger:status:classification_hits")  # 清空旧分类计数
            # 也可以清空旧日志
            keys = r.keys("artdigger:logs:worker:*")
            if keys: r.delete(*keys)
            
            # 注册全局退出清理
            atexit.register(lambda: r.delete("artdigger:status:workers"))
            
            # 添加自定义 Handler
            handler = RedisLogHandler(r)
            logging.getLogger().addHandler(handler)

    def stop(self) -> None:
        """发出停止信号并关闭子组件。"""
        self._stop_event.set()
        r = getattr(self._task_pool, 'redis', None)
        if r: r.delete("artdigger:status:workers")
        
        if hasattr(self, '_filler'):
            self._filler.stop()
        if hasattr(self, '_api_pool'):
            self._api_pool.stop()
        logger.info("Crawler stop signal set.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, initial_task: Task) -> None:
        """将初始 Task 放入 CrawlerTaskPool（Requirement 1.1）。"""
        self._task_pool.put(initial_task)
        logger.info(
            "┌─ Crawler started ─────────────────────────────────────────\n"
            "│  author=%s  tweet_id=%d  score=%.2f  depth=%d\n"
            "│  mini_batch=%d  max_depth=%d  alpha=%.2f  workers=%d\n"
            "└───────────────────────────────────────────────────────────",
            initial_task.author, initial_task.center_artwork_id,
            initial_task.score, initial_task.depth,
            self._config.mini_batch, self._config.max_depth,
            self._config.alpha, self._num_workers,
        )

    def run(self) -> None:
        """
        主循环：从 CrawlerTaskPool 取 Task，用线程池并发执行 process_task。
        队列为空时停止（Requirement 1.9）。
        """
        logger.info("Crawler run loop started (workers=%d)", self._num_workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self._num_workers) as executor:
            futures: set[concurrent.futures.Future] = set()
            tasks_dispatched = 0

            while not self._stop_event.is_set():
                while len(futures) < self._num_workers:
                    # 使用超时机制，以便能响应停止信号
                    try:
                        task = self._task_pool.get()
                        if task is None:
                            break
                    except Exception:
                        break
                    
                    tasks_dispatched += 1
                    logger.info(
                        "[Task #%d] Dispatching → author=%s  center=%d  score=%.3f  depth=%d",
                        tasks_dispatched, task.author, task.center_artwork_id,
                        task.score, task.depth,
                    )
                    fut = executor.submit(self.process_task, task)
                    futures.add(fut)

                if not futures:
                    break

                done, futures = concurrent.futures.wait(
                    futures, timeout=1.0, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for fut in done:
                    exc = fut.exception()
                    if exc:
                        logger.error("process_task raised an exception: %s", exc, exc_info=exc)

        logger.info(
            "Crawler finished — task queue empty. Total dispatched: %d", tasks_dispatched
        )

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    def process_task(self, task: Task) -> None:
        """
        完整处理一个 Task（Requirement 1.3 ~ 1.8, 2.1 ~ 2.5）。

        流程：
        1. 检查 ScanProgressDB，已扫描则跳过
        2. fetch_near_articles 获取 Article 列表
        3. 跳过 reply_id != 0 的 Article
        4. split_quote_article 拆分，对每个拆分结果分别处理
        5. Classifier 分类，统计命中数
        6. related 转发/引用 → 提交衍生 Task（depth+1）
        7. 非纯转发且 related → archive_pic
        8. 计算 next_score（EMA），提交两个延续 Task
        9. ScanProgressDB 标记已扫描
        """
        author = task.author
        center_id = task.center_artwork_id

        # ── Step 1: 已扫描则跳过 ─────────────────────────────────────────
        self._report_worker_status(author, center_id)
        if self._stop_event.is_set(): 
            self._report_worker_status(author, None)
            return
        if self._scan_db.is_scanned(author, center_id):
            logger.info("  [SKIP] Already scanned: author=%s center_id=%d", author, center_id)
            return

        logger.info(
            "  [START] process_task author=%s center_id=%d score=%.3f depth=%d",
            author, center_id, task.score, task.depth,
        )

        # ── Step 2: 获取附近 Article 列表 ────────────────────────────────
        logger.info("  [FETCH] Fetching %d articles around tweet %d for @%s ...",
                    self._config.mini_batch, center_id, author)
        try:
            articles = fetch_near_articles(
                db=self._timeline_db,
                filler=self._filler,
                author=author,
                center_id=center_id,
                n=self._config.mini_batch,
                score=task.score,
            )
        except Exception as exc:
            logger.error("  [FETCH ERROR] fetch_near_articles failed for %s/%d: %s",
                         author, center_id, exc)
            return

        logger.info("  [FETCH] Got %d articles", len(articles))
        for a in articles:
            kind = "RT" if a.retweet_id else ("QT" if a.quote_id else "  ")
            logger.info("    [%s] tweet_id=%-20d  media=%d  %s",
                        kind, a.tweet_id, len(a.media), a.date)

        # ── Step 3~7: 处理每个 Article ───────────────────────────────────
        hits = 0
        total = 0

        for article in articles:
            if self._stop_event.is_set(): return
            # 跳过回复帖子（Requirement 2.5）
            if article.reply_id != 0:
                logger.info("    [SKIP] reply tweet_id=%d", article.tweet_id)
                continue

            # 拆分 quote 帖子（Requirement 2.3）
            split_articles = split_quote_article(article)
            if len(split_articles) > 1:
                logger.info("    [SPLIT] tweet_id=%d → %d articles (quote+self)",
                            article.tweet_id, len(split_articles))

            for split_art in split_articles:
                total += 1
                if self._stop_event.is_set(): return
                logger.info(
                    "    [CLASSIFY] tweet_id=%-20d  media=%d  retweet=%d  quote=%d",
                    split_art.tweet_id, len(split_art.media),
                    split_art.retweet_id, split_art.quote_id,
                )
                result, hit_layer = self._classifier_pool.classify(split_art, score=task.score)
                logger.info("    [RESULT]   tweet_id=%-20d  → %s (%s)", split_art.tweet_id, result.upper(), hit_layer)

                # 上报分类命中计数到 Redis
                r = getattr(self._task_pool, 'redis', None)
                if r:
                    try:
                        field_name = f"{hit_layer}_{result}"
                        r.hincrby("artdigger:status:classification_hits", field_name, 1)
                    except Exception:
                        pass

                if result == "related":
                    hits += 1

                    # 衍生 Task：转发（Requirement 2.1）
                    if split_art.retweet_id != 0 and split_art.ref:
                        ref_author = split_art.ref.get("author", {})
                        ref_author_name = (
                            ref_author.get("name") or ref_author.get("screen_name") or ""
                        )
                        ref_tweet_id = split_art.ref.get("tweet_id", split_art.retweet_id)
                        if ref_author_name and ref_tweet_id:
                            logger.info(
                                "    [DERIVE] RT → author=%s tweet_id=%d depth=%d",
                                ref_author_name, ref_tweet_id, task.depth + 1,
                            )
                            self._maybe_submit_derived_task(
                                task,
                                author=ref_author_name,
                                center_artwork_id=ref_tweet_id,
                            )

                    if (
                        split_art.retweet_id == 0
                        and split_art.quote_id == 0
                        and split_art.tweet_id != article.tweet_id
                        and split_art.media
                    ):
                        b_author = split_art.author
                        b_author_name = (
                            b_author.get("name") or b_author.get("screen_name") or ""
                            if isinstance(b_author, dict) else str(b_author)
                        )
                        if b_author_name:
                            logger.info(
                                "    [DERIVE] QT → author=%s tweet_id=%d depth=%d",
                                b_author_name, split_art.tweet_id, task.depth + 1,
                            )
                            self._maybe_submit_derived_task(
                                task,
                                author=b_author_name,
                                center_artwork_id=split_art.tweet_id,
                            )

                    # archive_pic：非纯转发且 related且仅限L3新发现（避免重复导出）
                    if split_art.retweet_id == 0 and hit_layer == "L3":
                        logger.info("    [ARCHIVE] tweet_id=%d  media=%d",
                                    split_art.tweet_id, len(split_art.media))
                        try:
                            archive_pic(split_art, self._config.output_dir)
                        except Exception as exc:
                            logger.error(
                                "    [ARCHIVE ERROR] tweet_id=%d: %s",
                                split_art.tweet_id, exc,
                            )

        # ── Step 8: 计算 next_score（EMA）并提交延续 Task ────────────────
        local_score = hits / total if total > 0 else 0.0
        next_score = self._config.alpha * local_score + (1 - self._config.alpha) * task.score

        logger.info(
            "  [SCORE] hits=%d/%d  local=%.3f  next=%.3f  (alpha=%.2f)",
            hits, total, local_score, next_score, self._config.alpha,
        )

        older_id = self._get_shifted_id(author, center_id, -self._config.mini_batch, task.score)
        newer_id = self._get_shifted_id(author, center_id, +self._config.mini_batch, task.score)

        if task.depth <= self._config.max_depth:
            if older_id != center_id:
                logger.info("  [CONTINUE] older → tweet_id=%d  score=%.3f", older_id, next_score)
                self._task_pool.put(Task(
                    author=author,
                    center_artwork_id=older_id,
                    score=next_score,
                    depth=task.depth,
                ))
            if newer_id != center_id:
                logger.info("  [CONTINUE] newer → tweet_id=%d  score=%.3f", newer_id, next_score)
                self._task_pool.put(Task(
                    author=author,
                    center_artwork_id=newer_id,
                    score=next_score,
                    depth=task.depth,
                ))

        self._scan_db.mark_scanned(author, center_id)
        self._report_worker_status(author, None)
        logger.info("  [DONE] author=%s center_id=%d marked as scanned", author, center_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _maybe_submit_derived_task(
        self, parent_task: Task, author: str, center_artwork_id: int
    ) -> None:
        if parent_task.depth >= self._config.max_depth:
            logger.info(
                "    [DEPTH LIMIT] depth=%d >= max=%d, not submitting derived task for %s/%d",
                parent_task.depth, self._config.max_depth, author, center_artwork_id,
            )
            return
        derived = Task(
            author=author,
            center_artwork_id=center_artwork_id,
            score=parent_task.score*0.7 + (0.5*0.3),#决策动力调整
            depth=parent_task.depth + 1,
        )
        self._task_pool.put(derived)
        logger.info(
            "    [QUEUED] derived task author=%s tweet_id=%d depth=%d score=%.3f",
            author, center_artwork_id, derived.depth, derived.score,
        )

    def _get_shifted_id(
        self, author: str, center_id: int, offset: int, score: float
    ) -> int:
        """调用 shift_artwork_id，失败时返回 center_id（边界安全）。"""
        try:
            return shift_artwork_id(
                db=self._timeline_db,
                filler=self._filler,
                author=author,
                center_id=center_id,
                offset=offset,
                score=score,
            )
        except Exception as exc:
            logger.warning("shift_artwork_id failed (offset=%d): %s", offset, exc)
            return center_id

    def _report_worker_status(self, author: str, tweet_id: int | None) -> None:
        """上报当前线程的处理状态到 Redis。"""
        try:
            r = getattr(self._task_pool, 'redis', None)
            if not r: return
            ident = threading.get_ident()
            key = "artdigger:status:workers"
            if tweet_id:
                author_str = author
                if isinstance(author, dict):
                    author_str = author.get("screen_name") or author.get("name") or str(author)
                
                data = {
                    "type": "Crawler",
                    "author": str(author_str),
                    "detail": str(tweet_id)
                }
                r.hset(key, str(ident), json.dumps(data))
            else:
                # 任务结束，设为空闲而不是删除
                existing = r.hget(key, str(ident))
                if existing:
                    data = json.loads(existing)
                    data["detail"] = "Idle"
                    r.hset(key, str(ident), json.dumps(data))
        except Exception:
            pass

class RedisLogHandler(logging.Handler):
    """自定义日志处理器，将日志按线程推送到 Redis 列表。"""
    def __init__(self, redis_client):
        super().__init__()
        self._redis = redis_client

    def emit(self, record):
        try:
            ident = record.thread
            # 排除掉已知的外部库噪音，保留内部所有逻辑日志
            if record.name.startswith(('redis', 'urllib3', 'requests', 'asyncio')):
                return
            
            # 加上时间戳
            ts = time.strftime("%H:%M:%S")
            log_entry = f"[{ts}] {self.format(record)}"
            key = f"artdigger:logs:worker:{ident}"
            
            p = self._redis.pipeline()
            p.rpush(key, log_entry)
            p.ltrim(key, -100, -1) # 保留 100 条
            p.expire(key, 86400)   # 24小时
            p.execute()
        except Exception:
            pass
