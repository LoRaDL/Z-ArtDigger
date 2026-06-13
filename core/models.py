"""
Data models for the fanart-crawler system.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field


@dataclass
class Task:
    author: str           # screen_name
    center_artwork_id: int
    score: float          # [0.0, 1.0]
    depth: int


@dataclass
class MediaItem:
    media_id: int
    url: str              # 原图 URL（?name=orig）
    filename: str
    extension: str
    width: int
    height: int


@dataclass
class Article:
    tweet_id: int
    author: dict          # {id, name, nick, ...}
    date: str             # "YYYY-MM-DD HH:MM:SS"
    content: str
    retweet_id: int = 0       # 0 表示非转发
    quote_id: int = 0         # 0 表示非引用
    reply_id: int = 0         # 0 表示非回复
    media: list[MediaItem] = field(default_factory=list)  # 空列表表示无图
    ref: dict | None = None  # 被转发/引用帖子的摘要


@dataclass
class IslandRange:
    min_id: int
    max_id: int
    oldest_boundary: bool = False   # 此端已是博主时间线最旧端
    newest_checked_at: float = 0.0  # 此端最近一次检查是否为最新端的时间戳（0.0表示从未检查）

    def should_explore_newer(self, ttl_seconds: float = 21600.0) -> bool:
        """检查是否需要向最新端探索。如果从未检查过，或距离上次检查已超过 TTL，则返回 True。"""
        if self.newest_checked_at == 0.0:
            return True
        return (time.time() - self.newest_checked_at) >= ttl_seconds

    def overlaps_or_adjacent(self, other: 'IslandRange') -> bool:
        """
        Returns True if this range overlaps with or is adjacent to `other`.
        Two ranges are adjacent if one ends exactly one before the other begins
        (i.e., they can be merged into a single contiguous range).
        """
        return self.min_id <= other.max_id + 1 and other.min_id <= self.max_id + 1

    def merge(self, other: 'IslandRange') -> 'IslandRange':
        """
        Returns a new IslandRange that covers both self and other.
        Merges boundary flags: keep True if either side has it for the
        corresponding boundary direction.
        """
        merged_min = min(self.min_id, other.min_id)
        merged_max = max(self.max_id, other.max_id)

        # oldest_boundary: True if the side with the smaller min_id had it
        if self.min_id < other.min_id:
            ob = self.oldest_boundary
        elif other.min_id < self.min_id:
            ob = other.oldest_boundary
        else:
            ob = self.oldest_boundary or other.oldest_boundary

        # newest_checked_at: Take from the side with the larger max_id
        if self.max_id > other.max_id:
            nca = self.newest_checked_at
        elif other.max_id > self.max_id:
            nca = other.newest_checked_at
        else:
            nca = max(self.newest_checked_at, other.newest_checked_at)

        return IslandRange(
            min_id=merged_min,
            max_id=merged_max,
            oldest_boundary=ob,
            newest_checked_at=nca,
        )
