"""
TimelineIslandsDB — SQLite-backed persistence for tweet metadata and island tracking.

所有帖子（含无图）均入库，孤岛区间覆盖所有已扫描 ID 段。
has_image 过滤仅在读取侧进行。
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from pathlib import Path

from sortedcontainers import SortedList

from core.models import Article, IslandRange, MediaItem


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL_TWEETS = """
CREATE TABLE IF NOT EXISTS tweets (
    author      TEXT NOT NULL,
    tweet_id    INTEGER NOT NULL,
    date        TEXT NOT NULL,
    content     TEXT,
    retweet_id  INTEGER DEFAULT 0,
    quote_id    INTEGER DEFAULT 0,
    reply_id    INTEGER DEFAULT 0,
    media_json  TEXT,
    ref_json    TEXT,
    PRIMARY KEY (author, tweet_id)
);
"""

_DDL_ISLAND_RANGES = """
CREATE TABLE IF NOT EXISTS island_ranges (
    author           TEXT NOT NULL,
    min_id           INTEGER NOT NULL,
    max_id           INTEGER NOT NULL,
    oldest_boundary  INTEGER DEFAULT 0,
    newest_checked_at REAL DEFAULT 0.0,
    PRIMARY KEY (author, min_id)
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _article_to_row(author: str, article: Article) -> tuple:
    media_json = json.dumps([dataclasses.asdict(m) for m in article.media])
    ref_json = json.dumps(article.ref) if article.ref is not None else None
    return (
        author,
        article.tweet_id,
        article.date,
        article.content,
        article.retweet_id,
        article.quote_id,
        article.reply_id,
        media_json,
        ref_json,
    )


def _row_to_article(row: sqlite3.Row) -> Article:
    media_list: list[MediaItem] = []
    if row["media_json"]:
        for m in json.loads(row["media_json"]):
            media_list.append(MediaItem(**m))
    ref = json.loads(row["ref_json"]) if row["ref_json"] else None
    author_val = row["author"]
    author_dict = {"name": author_val} if isinstance(author_val, str) else author_val
    return Article(
        tweet_id=row["tweet_id"],
        author=author_dict,
        date=row["date"],
        content=row["content"] or "",
        retweet_id=row["retweet_id"],
        quote_id=row["quote_id"],
        reply_id=row["reply_id"],
        media=media_list,
        ref=ref,
    )


def _merge_into_sorted_list(sl: SortedList, new_range: IslandRange) -> None:
    """Merge new_range into the SortedList in-place."""
    merged = new_range
    to_remove: list[IslandRange] = []

    idx = sl.bisect_key_left(merged.min_id - 1)
    if idx > 0:
        idx -= 1

    while idx < len(sl):
        r = sl[idx]
        if r.min_id > merged.max_id + 1:
            break
        if merged.overlaps_or_adjacent(r):
            merged = merged.merge(r)
            to_remove.append(r)
        idx += 1

    for r in to_remove:
        sl.remove(r)
    sl.add(merged)


# ---------------------------------------------------------------------------
# Gap computation
# ---------------------------------------------------------------------------

def _compute_gaps(
    sl: SortedList, query_min: int, query_max: int
) -> list[tuple[int, int]]:
    gaps: list[tuple[int, int]] = []
    cursor = query_min

    start_idx = sl.bisect_key_left(query_min)
    if start_idx > 0:
        start_idx -= 1

    for i in range(start_idx, len(sl)):
        r = sl[i]
        if r.min_id > query_max:
            break
        if r.max_id < query_min:
            continue
        effective_min = max(r.min_id, query_min)
        effective_max = min(r.max_id, query_max)
        if cursor < effective_min:
            gaps.append((cursor, effective_min - 1))
        cursor = max(cursor, effective_max + 1)

    if cursor <= query_max:
        gaps.append((cursor, query_max))

    return gaps


# ---------------------------------------------------------------------------
# TimelineIslandsDB
# ---------------------------------------------------------------------------

class TimelineIslandsDB:
    """
    SQLite-backed store for tweet metadata and IslandRange tracking.
    Thread-safe via a threading.Lock.

    所有帖子（含无图）均存入 tweets 表。
    孤岛区间覆盖所有已扫描的帖子 ID 段。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        with self._conn:
            self._conn.execute(_DDL_TWEETS)
            self._conn.execute(_DDL_ISLAND_RANGES)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_sorted_list(self, author: str) -> SortedList:
        rows = self._conn.execute(
            "SELECT min_id, max_id, oldest_boundary, newest_checked_at "
            "FROM island_ranges WHERE author = ? ORDER BY min_id",
            (author,),
        ).fetchall()
        sl: SortedList = SortedList(key=lambda r: r.min_id)
        for row in rows:
            sl.add(IslandRange(
                min_id=row["min_id"],
                max_id=row["max_id"],
                oldest_boundary=bool(row["oldest_boundary"]),
                newest_checked_at=float(row["newest_checked_at"]),
            ))
        return sl

    def _save_sorted_list(self, author: str, sl: SortedList) -> None:
        self._conn.execute("DELETE FROM island_ranges WHERE author = ?", (author,))
        self._conn.executemany(
            "INSERT INTO island_ranges (author, min_id, max_id, oldest_boundary, newest_checked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(author, r.min_id, r.max_id, int(r.oldest_boundary), r.newest_checked_at)
             for r in sl],
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def insert(self, author: str, articles: list[Article],
               oldest_boundary: bool = False,
               newest_checked_at: float = 0.0,
               extend_to: int | None = None) -> None:
        """
        Insert articles and update IslandRange for author.
        extend_to: 可选，将孤岛边界强制拉伸到此 ID，用于缝合逻辑连续但 ID 稀疏的片段。
        """
        if not articles and extend_to is None:
            return

        with self._lock:
            tweet_ids = [a.tweet_id for a in articles]
            if extend_to is not None:
                tweet_ids.append(extend_to)
            
            new_range = IslandRange(
                min_id=min(tweet_ids),
                max_id=max(tweet_ids),
                oldest_boundary=oldest_boundary,
                newest_checked_at=newest_checked_at,
            )

            with self._conn:
                if articles:
                    rows = [_article_to_row(author, a) for a in articles]
                    self._conn.executemany(
                        """INSERT OR IGNORE INTO tweets
                           (author, tweet_id, date, content, retweet_id, quote_id, reply_id, media_json, ref_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        rows,
                    )
                sl = self._load_sorted_list(author)
                _merge_into_sorted_list(sl, new_range)
                self._save_sorted_list(author, sl)

    def query(
        self, author: str, min_id: int, max_id: int
    ) -> tuple[list[Article], list[tuple[int, int]]]:
        """
        Returns (articles_in_range, gaps).
        Returns ALL articles (including those without images).
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM tweets
                   WHERE author = ? AND tweet_id BETWEEN ? AND ?
                   ORDER BY tweet_id DESC""",
                (author, min_id, max_id),
            ).fetchall()

            articles = [_row_to_article(row) for row in rows]
            sl = self._load_sorted_list(author)
            gaps = _compute_gaps(sl, min_id, max_id)

        return articles, gaps

    def find_island(self, author: str, tweet_id: int) -> IslandRange | None:
        """Return the IslandRange that contains tweet_id, or None."""
        with self._lock:
            sl = self._load_sorted_list(author)
        for r in sl:
            if r.min_id <= tweet_id <= r.max_id:
                return r
        return None

    def count_has_image_in_range(self, author: str, min_id: int, max_id: int) -> int:
        """Count articles with has_image=True in [min_id, max_id]."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT media_json, retweet_id, quote_id, ref_json FROM tweets
                   WHERE author = ? AND tweet_id BETWEEN ? AND ?""",
                (author, min_id, max_id),
            ).fetchall()
        count = 0
        for row in rows:
            art = Article(
                tweet_id=0, author={}, date="", content="",
                retweet_id=row["retweet_id"], quote_id=row["quote_id"],
                reply_id=0,
                media=[MediaItem(**m) for m in json.loads(row["media_json"] or "[]")],
                ref=json.loads(row["ref_json"]) if row["ref_json"] else None,
            )
            if _article_has_image(art):
                count += 1
        return count

    def get_avg_post_interval(self, author: str) -> float | None:
        """
        Return the average time interval (seconds) between consecutive tweets
        for the given author, or None if insufficient data.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT date FROM tweets WHERE author = ? ORDER BY tweet_id ASC",
                (author,),
            ).fetchall()
        if len(rows) < 2:
            return None
        from datetime import datetime
        dates = []
        for row in rows:
            try:
                dates.append(datetime.strptime(row["date"], "%Y-%m-%d %H:%M:%S"))
            except (ValueError, TypeError):
                continue
        if len(dates) < 2:
            return None
        total_seconds = (dates[-1] - dates[0]).total_seconds()
        return total_seconds / (len(dates) - 1)

    def get_island_ranges(self, author: str) -> list[IslandRange]:
        """Return sorted list of IslandRange for author."""
        with self._lock:
            sl = self._load_sorted_list(author)
        return list(sl)


def _article_has_image(article: Article) -> bool:
    """Check if article has photo media (simplified check on stored data)."""
    return len(article.media) > 0
