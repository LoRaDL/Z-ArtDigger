"""
timeline/query.py — 核心查询层。
实现 fetch_near_articles 和 shift_artwork_id。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from storage.timeline.aggregator import has_image
from storage.timeline.fill_request import FillRequest

if TYPE_CHECKING:
    from storage.timeline_db import TimelineIslandsDB
    from storage.timeline.db_filler import DBFiller
    from core.models import Article

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def fetch_near_articles(
    db: TimelineIslandsDB,
    filler: DBFiller,
    author: str,
    center_id: int,
    n: int,
    score: float = 0.5
) -> list[Article]:
    """
    获取 center_id 附近的 n 条含图帖子。
    
    逻辑：
    1. 找孤岛。若无，提交初始 FillRequest 建立第一个坐标。
    2. 在孤岛内过滤并计数。
    3. 不满足则向两端发起 FillRequest(anchor_id=boundary, count=needed)。
    4. 循环逐步逼近。
    """
    for attempt in range(MAX_ATTEMPTS):
        island = db.find_island(author, center_id)
        if island is None:
            # 初始扫描：建立包含 center_id 的第一个点
            logger.info(f"Center {center_id} not found in any island. Requesting initial scan.")
            req = FillRequest(author, anchor_id=center_id, direction=-1, count=1, score=score)
            filler.submit(req)
            req.event.wait()
            continue
        
        # 获取孤岛内所有帖子（含无图）进行中心检查
        all_in_island, _ = db.query(author, island.min_id, island.max_id)
        center_record = next((p for p in all_in_island if p.tweet_id == center_id), None)
        
        if center_record:
            if not has_image(center_record):
                raise ValueError(f"Center artwork {center_id} exists but has no images.")
        else:
            # 这种情况通常是 initial scan 只抓到了 center_id 之前的推文
            # 或者 center_id 根本不在博主的时间线里
            if attempt == MAX_ATTEMPTS - 1:
                raise ValueError(f"Center ID {center_id} not found in database/timeline after scanning.")
        
        # 内存过滤 has_image 用于返回
        image_posts = [p for p in all_in_island if has_image(p)]
        image_posts.sort(key=lambda p: p.tweet_id, reverse=True)
        
        newer = [p for p in image_posts if p.tweet_id > center_id]
        older = [p for p in image_posts if p.tweet_id < center_id]
        center_p = [p for p in image_posts if p.tweet_id == center_id]
        
        # 检查是否满足
        need_n = max(0, n // 2 - len(newer)) if island.should_explore_newer() else 0
        need_o = max(0, n // 2 - len(older)) if not island.oldest_boundary else 0
        
        if need_n == 0 and need_o == 0:
            # 足够了。合并结果并截断
            final = newer[-(n // 2):] if newer else []
            final += center_p
            final += older[:(n // 2)] if older else []
            return final
        
        # 不足：提交边界扩展请求
        if need_n > 0:
            req_n = FillRequest(author, anchor_id=island.max_id, direction=+1, count=need_n, score=score)
            filler.submit(req_n)
        if need_o > 0:
            req_o = FillRequest(author, anchor_id=island.min_id, direction=-1, count=need_o, score=score)
            filler.submit(req_o)
        
        # 等待。由于 filler 会唤醒所有满足的请求，我们等其中一个即可，或者循环等。
        # 这里简化处理：如果是双向需求，通常会先后提交，我们至少等一个完成。
        if need_n > 0: req_n.event.wait()
        elif need_o > 0: req_o.event.wait()

    # 循环结束（或到达边界）后能拿到多少是多少
    # 重新查库取最后一轮结果
    island = db.find_island(author, center_id)
    if not island: return []
    all_posts, _ = db.query(author, island.min_id, island.max_id)
    image_posts = sorted([p for p in all_posts if has_image(p)], key=lambda p: p.tweet_id, reverse=True)
    
    # 简单的截断逻辑
    newer = [p for p in image_posts if p.tweet_id > center_id]
    older = [p for p in image_posts if p.tweet_id < center_id]
    center_p = [p for p in image_posts if p.tweet_id == center_id]
    
    final = newer[-(n // 2):] if newer else []
    final += center_p
    final += older[:(n // 2)] if older else []
    return final


def shift_artwork_id(
    db: TimelineIslandsDB,
    filler: DBFiller,
    author: str,
    center_id: int,
    offset: int,
    score: float = 0.5
) -> int:
    """
    按含图帖子的顺序，将 tweet_id 偏移 offset 位。
    """
    if offset == 0: return center_id

    for attempt in range(MAX_ATTEMPTS):
        island = db.find_island(author, center_id)
        if island is None:
            # 初始扫描
            req = FillRequest(author, anchor_id=center_id, direction=-1, count=1, score=score)
            filler.submit(req); req.event.wait()
            continue
        
        all_posts, _ = db.query(author, island.min_id, island.max_id)
        image_posts = sorted([p for p in all_posts if has_image(p)], key=lambda p: p.tweet_id)
        
        # 找到 center_id 在含图序列里的位置
        try:
            cur_idx = -1
            for i, p in enumerate(image_posts):
                if p.tweet_id == center_id:
                    cur_idx = i
                    break
            
            if cur_idx == -1:
                # 极端情况：center_id 恰好无图，找一个最近的基准
                # TODO: 优化
                return center_id

            target_idx = cur_idx + offset
            
            if 0 <= target_idx < len(image_posts):
                return image_posts[target_idx].tweet_id
            
            # 索引越界：需要扩展
            if target_idx < 0:
                if island.oldest_boundary: return image_posts[0].tweet_id
                needed = abs(target_idx)
                req = FillRequest(author, anchor_id=island.min_id, direction=-1, count=needed, score=score)
            else:
                if not island.should_explore_newer(): return image_posts[-1].tweet_id
                needed = target_idx - len(image_posts) + 1
                req = FillRequest(author, anchor_id=island.max_id, direction=+1, count=needed, score=score)
            
            filler.submit(req)
            req.event.wait()
            
        except Exception as e:
            logger.error(f"Error in shift_artwork_id: {e}")
            break

    return center_id
