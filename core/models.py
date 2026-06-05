"""
Data models for the fanart-crawler system.
"""
from __future__ import annotations
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
    newest_boundary: bool = False   # 此端已是博主时间线最新端

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

        # newest_boundary: True if the side with the larger max_id had it
        if self.max_id > other.max_id:
            nb = self.newest_boundary
        elif other.max_id > self.max_id:
            nb = other.newest_boundary
        else:
            nb = self.newest_boundary or other.newest_boundary

        return IslandRange(
            min_id=merged_min,
            max_id=merged_max,
            oldest_boundary=ob,
            newest_boundary=nb,
        )
