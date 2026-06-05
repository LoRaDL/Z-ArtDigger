"""
L3Pool — single-worker priority queue for VisionLLM (L3) classification.

Requests are processed in descending score order (highest priority first).
The worker thread is a daemon thread started in __init__.
"""
from __future__ import annotations

import base64
import io
import itertools
import logging
import queue
import threading
import json
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Any

import requests
from PIL import Image

from io_layer.downloader import ImageDownloader
from core.models import Article
from core.config import (
    DEFAULT_L3_PROMPT,
    DEFAULT_VISION_LLM_URL,
    DEFAULT_VISION_LLM_API_KEY,
    DEFAULT_VISION_LLM_MODEL,
    DEFAULT_VISION_LLM_MAX_WORKERS,
    DEFAULT_MAX_LONG_EDGE,
)

logger = logging.getLogger(__name__)


class LLMClassifierError(Exception):
    """Raised when the LLM classifier fails (network, parse, or invalid response)."""
    pass

# Sentinel to signal worker shutdown
_SHUTDOWN = object()

# Global counter for tiebreaking in PriorityQueue (min-heap, so negate score)
_counter = itertools.count()


@dataclass(order=True)
class _QueueItem:
    """Wrapper that makes L3Request sortable for PriorityQueue."""
    priority: float          # negated score so min-heap gives descending order
    seq: int                 # tiebreaker
    request: object = field(compare=False)  # L3Request or _SHUTDOWN sentinel


@dataclass
class L3Request:
    article: Article
    score: float
    future: Future


class L3Pool:
    """Multi-worker thread pool processing L3 requests in descending score order."""

    def __init__(self, image_downloader: ImageDownloader | None = None,
                 cache_dir: Path | None = None,
                 redis_client: Any = None,
                 max_workers: int | None = None,
                 prompt: str | None = None,
                 api_url: str | None = None,
                 api_key: str | None = None,
                 model: str | None = None,
                 max_long_edge: int | None = None) -> None:
        self._queue: queue.PriorityQueue[_QueueItem] = queue.PriorityQueue()
        self._redis = redis_client
        self._max_workers = max_workers if max_workers is not None else DEFAULT_VISION_LLM_MAX_WORKERS
        self._prompt = prompt or DEFAULT_L3_PROMPT
        self._api_url = api_url or DEFAULT_VISION_LLM_URL
        self._api_key = api_key if api_key is not None else DEFAULT_VISION_LLM_API_KEY
        self._model = model or DEFAULT_VISION_LLM_MODEL
        self._max_long_edge = max_long_edge if max_long_edge is not None else DEFAULT_MAX_LONG_EDGE
        
        if image_downloader is not None:
            self._downloader = image_downloader
        else:
            _dir = cache_dir or Path(".image_cache")
            self._downloader = ImageDownloader(_dir)

        self._workers = []
        for i in range(self._max_workers):
            t = threading.Thread(target=self._worker, daemon=True, name=f"L3Pool-worker-{i}")
            t.start()
            self._workers.append(t)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(self, article: Article, score: float) -> Future:
        """
        Enqueue an L3 classification request.
        Returns a Future; caller blocks on future.result().
        """
        future: Future = Future()
        request = L3Request(article=article, score=score, future=future)
        item = _QueueItem(priority=-score, seq=next(_counter), request=request)
        self._queue.put(item)
        return future

    def shutdown(self) -> None:
        """Signal all workers to stop after finishing current work."""
        for _ in range(self._max_workers):
            item = _QueueItem(priority=float("inf"), seq=next(_counter), request=_SHUTDOWN)
            self._queue.put(item)
            
        for t in self._workers:
            t.join()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        """Background thread: dequeue requests, call VisionLLM, resolve Futures."""
        while True:
            item = self._queue.get()
            if item.request is _SHUTDOWN:
                break
            req: L3Request = item.request  # type: ignore[assignment]
            try:
                self._report_status(req.article.author, str(req.article.tweet_id))
                result, path = self._classify(req.article)
                req.future.set_result(result)
                if path:
                    self._save_last_vision_result(path, result)
            except Exception as exc:  # noqa: BLE001
                logger.error("L3Pool worker error for tweet_id=%s: %s",
                             req.article.tweet_id, exc, exc_info=True)
                req.future.set_exception(exc)
            finally:
                self._report_status(req.article.author, None)
                self._report_status_simple()
                
    def _save_last_vision_result(self, image_path: Path, result: str) -> None:
        """保存最新的视觉分析结果到 Redis。"""
        if self._redis:
            try:
                data = {
                    "image_path": str(image_path),
                    "filename": image_path.name,
                    "result": result,
                    "timestamp": time.time()
                }
                self._redis.set("artdigger:last_vision", json.dumps(data))
            except Exception:
                pass

    def _report_error(self, tweet_id: int, error_msg: str) -> None:
        """记录错误到 Redis。"""
        if self._redis:
            try:
                self._redis.incr("artdigger:status:l3_errors")
                err_data = {
                    "tweet_id": tweet_id,
                    "error": error_msg,
                    "timestamp": time.time()
                }
                self._redis.set("artdigger:status:l3_last_error", json.dumps(err_data))
            except Exception:
                pass

    def _report_status(self, author: str, detail: str | None) -> None:
        if self._redis:
            try:
                ident = threading.get_ident()
                key = "artdigger:status:workers"
                if detail:
                    author_str = author
                    if isinstance(author, dict):
                        author_str = author.get("screen_name") or author.get("name") or str(author)
                    
                    data = {"type": "Vision", "author": str(author_str), "detail": detail}
                    self._redis.hset(key, str(ident), json.dumps(data))
                else:
                    existing = self._redis.hget(key, str(ident))
                    if existing:
                        data = json.loads(existing)
                        data["detail"] = "Idle"
                        self._redis.hset(key, str(ident), json.dumps(data))
            except Exception:
                pass

    def _report_status_simple(self) -> None:
        if self._redis:
            try:
                self._redis.set("artdigger:status:l3_queue", self._queue.qsize())
            except Exception:
                pass

    # ------------------------------------------------------------------
    # VisionLLM classification
    # ------------------------------------------------------------------

    def _classify(self, article: Article) -> tuple[Literal["related", "unrelated"], Path | None]:
        """Run VisionLLM classification for the first image in article."""
        if not article.media:
            logger.info("      [LLM] tweet_id=%d → no media, unrelated", article.tweet_id)
            return "unrelated", None

        media = article.media[0]
        logger.info("      [LLM] tweet_id=%d  media_id=%d  fetching image ...",
                    article.tweet_id, media.media_id)
        try:
            img_path = self._downloader.get(media.media_id, media.url)
        except Exception as exc:
            # If image is gone (404), treat as unrelated immediately instead of retrying
            if isinstance(exc, requests.exceptions.HTTPError) and \
               exc.response is not None and exc.response.status_code == 404:
                logger.warning("      [LLM] tweet_id=%d  image 404, marking as unrelated", article.tweet_id)
                return "unrelated", None

            msg = f"Failed to get image: {exc}"
            logger.error("      [LLM] tweet_id=%d  %s", article.tweet_id, msg)
            self._report_error(article.tweet_id, msg)
            raise LLMClassifierError(msg) from exc

        b64_image = self._encode_image(img_path)
        logger.info("      [LLM] tweet_id=%d  sending to VisionLLM @ %s ...",
                    article.tweet_id, self._api_url)
        result = self._call_vision_llm(b64_image, article.tweet_id)
        return result, img_path

    def _encode_image(self, img_path: Path) -> str:
        """Resize to longest edge ≤ self._max_long_edge, convert to JPEG, return base64 string."""
        with Image.open(img_path) as img:
            img = img.convert("RGB")
            w, h = img.size
            if max(w, h) > self._max_long_edge:
                scale = self._max_long_edge / max(w, h)
                new_w = max(1, int(w * scale))
                new_h = max(1, int(h * scale))
                img = img.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG")
            return base64.b64encode(buf.getvalue()).decode("ascii")

    def _call_vision_llm(self, b64_image: str,
                         tweet_id: int) -> Literal["related", "unrelated"]:
        """POST to VisionLLM and parse the response."""
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": self._prompt
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请分析以下图片："},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_image}"
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 2048
        }

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            resp = requests.post(self._api_url, headers=headers, json=payload, timeout=300)
            resp.raise_for_status()
        except Exception as exc:
            msg = f"Request failed: {exc}"
            logger.error("      [LLM] tweet_id=%d  %s", tweet_id, msg)
            self._report_error(tweet_id, msg)
            raise LLMClassifierError(msg) from exc

        try:
            data = resp.json()
            text: str = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            msg = f"Parse error: {exc}"
            logger.error("      [LLM] tweet_id=%d  %s", tweet_id, msg)
            self._report_error(tweet_id, msg)
            raise LLMClassifierError(msg) from exc

        last_word = text.strip().split()[-1].lower() if text.strip() else ""
        logger.info(
            "      [LLM] tweet_id=%d  response_tail='%s'  last_word='%s'",
            tweet_id,
            text.strip()[-80:] if len(text.strip()) > 80 else text.strip(),
            last_word,
        )
        return self._parse_last_word(last_word, tweet_id)

    @staticmethod
    def _parse_last_word(last_word: str,
                         tweet_id: int) -> Literal["related", "unrelated"]:
        if "[no]" in last_word or last_word == "no":
            logger.info("      [LLM] tweet_id=%d -> UNRELATED [no]", tweet_id)
            return "unrelated"
        if "[yes]" in last_word or last_word == "yes":
            logger.info("      [HIT]  L3 tweet_id=%d -> RELATED [yes]", tweet_id)
            return "related"
        if "unsure" in last_word:
            logger.warning("      [LLM] tweet_id=%d -> UNRELATED [unsure] (treated as no)", tweet_id)
            return "unrelated"
        
        msg = f"Unknown keyword in response: {last_word!r}"
        logger.info("      [LLM] tweet_id=%d -> %s", tweet_id, msg)
        raise LLMClassifierError(msg)
