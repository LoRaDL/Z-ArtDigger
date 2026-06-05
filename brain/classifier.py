"""
ClassifierPool and Classifier — multi-level image classification pipeline.

Level order: L0 (cache) → L1 (URL match) → L2 (pHash) → L3 (VisionLLM)

Short-circuit: once any level returns a result, subsequent levels are skipped.
After any level produces a result, it is written to ClassificationCacheDB (L0).

Degradation:
  - L1 failure → fall through to L2
  - L2 failure → fall through to L3
  - L3 failure → return 'unrelated' and log
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Literal

import imagehash
from PIL import Image

import time
from brain.cache import ClassificationCacheDB
from io_layer.downloader import ImageDownloader
from brain.l3pool import L3Pool, LLMClassifierError
from core.models import Article

logger = logging.getLogger(__name__)

ClassifyStatus = Literal["related", "unrelated"]
ClassifyResult = tuple[ClassifyStatus, str]

# Hamming distance threshold for pHash matching (L2)
PHASH_THRESHOLD = 10


class Classifier:
    """
    Single-article classifier executing L0 → L1 → L2 → L3 with short-circuit.

    L0/L1/L2 run synchronously in the caller's thread.
    L3 is submitted to L3Pool; the caller blocks on future.result().
    """

    def __init__(
        self,
        cache_db: ClassificationCacheDB,
        gallery_db_path: Path | None,
        image_downloader: ImageDownloader,
        l3pool: L3Pool,
    ) -> None:
        self._cache = cache_db
        self._gallery_db_path = gallery_db_path
        self._downloader = image_downloader
        self._l3pool = l3pool

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self, article: Article, score: float = 0.0) -> ClassifyResult:
        """
        Classify *article* using L0 → L1 → L2 → L3 short-circuit evaluation.
        Returns ('related'/'unrelated', hit_layer).
        """
        tweet_id = article.tweet_id

        # L0: cache lookup
        cached = self._cache.get(tweet_id)
        if cached in ("related", "unrelated"):
            logger.info("      [HIT]  L0 tweet_id=%d → %s", tweet_id, cached.upper())
            return cached, "L0"  # type: ignore[return-value]

        logger.debug("      [L0] tweet_id=%d → miss", tweet_id)
        result = self._run_l1(article)
        if result is None:
            result = self._run_l2(article)
        if result is None:
            result = self._run_l3(article, score)

        self._cache.set(tweet_id, result[0])
        return result

    def _run_l1(self, article: Article) -> ClassifyResult | None:
        """Return 'related' if the tweet's post URL (tweet_id) matches GalleryDB.source_url, else None."""
        if not self._gallery_db_path:
            logger.debug("      [L1] tweet_id=%d → skip (gallery_db_path is None)", article.tweet_id)
            return None
        if not article.media:
            logger.debug("      [L1] tweet_id=%d → skip (no media)", article.tweet_id)
            return None
        logger.debug("      [L1] tweet_id=%d  checking tweet_id against GalleryDB source_url ...",
                     article.tweet_id)
        try:
            # uri=True + ?mode=ro 确保只读，不会意外创建空文件
            uri = self._gallery_db_path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                # 帖子 URL 通常包含 /status/{tweet_id}，匹配此模式以精准匹配
                target_pattern = f"%/status/{article.tweet_id}%"
                row = conn.execute(
                    "SELECT 1 FROM artworks WHERE source_url LIKE ? LIMIT 1",
                    (target_pattern,),
                ).fetchone()
                if row:
                    logger.info("      [HIT]  L1 tweet_id=%d → RELATED (post url match)", article.tweet_id)
                    return "related", "L1"
        except Exception as exc:
            logger.warning("      [L1] tweet_id=%d → error, falling through: %s",
                           article.tweet_id, exc)
        logger.debug("      [L1] tweet_id=%d → miss", article.tweet_id)
        return None

    # ------------------------------------------------------------------
    # L2: pHash comparison against GalleryDB
    # ------------------------------------------------------------------

    def _run_l2(self, article: Article) -> ClassifyResult | None:
        """Return 'related' if any image pHash is within threshold of GalleryDB, else None."""
        if not self._gallery_db_path:
            logger.debug("      [L2] tweet_id=%d → skip (gallery_db_path is None)", article.tweet_id)
            return None
        if not article.media:
            logger.debug("      [L2] tweet_id=%d → skip (no media)", article.tweet_id)
            return None
        logger.debug("      [L2] tweet_id=%d  downloading & computing pHash for %d image(s) ...",
                    article.tweet_id, len(article.media))
        try:
            with sqlite3.connect(self._gallery_db_path) as conn:
                rows = conn.execute("SELECT phash FROM artworks WHERE phash IS NOT NULL").fetchall()
            if not rows:
                logger.debug("      [L2] GalleryDB has no pHash entries, skipping")
                return None

            gallery_hashes = [imagehash.hex_to_hash(r[0]) for r in rows if r[0]]
            logger.debug("      [L2] Loaded %d pHash entries from GalleryDB", len(gallery_hashes))

            for media in article.media:
                try:
                    img_path = self._downloader.get(media.media_id, media.url)
                    with Image.open(img_path) as img:
                        phash = imagehash.phash(img)
                    min_dist = min((phash - gh) for gh in gallery_hashes)
                    logger.debug("      [L2] media_id=%d  pHash=%s  min_dist=%d (threshold=%d)",
                                media.media_id, str(phash), min_dist, PHASH_THRESHOLD)
                    if min_dist <= PHASH_THRESHOLD:
                        logger.info("      [HIT]  L2 tweet_id=%d → RELATED (pHash match)", article.tweet_id)
                        return "related", "L2"
                except Exception as exc:
                    logger.warning("      [L2] media_id=%d → error: %s", media.media_id, exc)
                    continue
        except Exception as exc:
            logger.warning("      [L2] tweet_id=%d → error, falling through: %s",
                           article.tweet_id, exc)
        logger.debug("      [L2] tweet_id=%d → miss", article.tweet_id)
        return None

    # ------------------------------------------------------------------
    # L3: VisionLLM via L3Pool
    # ------------------------------------------------------------------

    def _run_l3(self, article: Article, score: float) -> ClassifyResult:
        """Submit to L3Pool, block on future.result(). Retries on LLMClassifierError."""
        logger.info("      [L3] tweet_id=%d  submitting to L3Pool (score=%.3f) ...",
                    article.tweet_id, score)
        
        while True:
            try:
                future = self._l3pool.submit(article, score)
                result = future.result()
                logger.info("      [L3] tweet_id=%d → %s", article.tweet_id, result.upper())
                return result, "L3"
            except LLMClassifierError as exc:
                logger.warning("      [L3] tweet_id=%d → LLM Error, retrying in 5s: %s", 
                               article.tweet_id, exc)
                time.sleep(5)
            except Exception as exc:
                logger.error("      [L3] tweet_id=%d → UNEXPECTED ERROR, returning unrelated: %s",
                             article.tweet_id, exc, exc_info=True)
                return "unrelated", "L3"


class ClassifierPool:
    """
    Thin coordinator that exposes a single classify() method.

    L0/L1/L2 run synchronously in the caller's thread (process_task thread).
    L3 is submitted to L3Pool; the caller blocks until the Future resolves.
    Multiple process_task threads can call classify() concurrently.
    """

    def __init__(
        self,
        cache_db: ClassificationCacheDB,
        gallery_db_path: Path | None,
        image_downloader: ImageDownloader,
        l3pool: L3Pool,
    ) -> None:
        self._classifier = Classifier(
            cache_db=cache_db,
            gallery_db_path=gallery_db_path,
            image_downloader=image_downloader,
            l3pool=l3pool,
        )

    def classify(self, article: Article, score: float = 0.0) -> ClassifyResult:
        """Classify *article*; thread-safe (L0/L1/L2 sync, L3 via L3Pool)."""
        return self._classifier.classify(article, score)
