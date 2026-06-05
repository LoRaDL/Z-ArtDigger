"""
Aggregation logic for Article objects (normalization and multi-image merging).
Note: has_image filtering is NOT performed here at the input stage.
"""
from __future__ import annotations

from core.models import Article, MediaItem
from fetcher.normalize import normalize_tweet


def _has_photo_media(legacy: dict) -> bool:
    """检查 legacy 字段中是否包含 photo media。"""
    media_list = (
        legacy.get("extended_entities", {}).get("media")
        or legacy.get("entities", {}).get("media")
        or []
    )
    return any(m.get("type") == "photo" for m in media_list)


def has_image(article: Article) -> bool:
    """
    判断一个帖子是否含图。
    本函数现在主要供 Fetch 侧查询结果进行内存过滤时调用。
    """
    return len(article.media) > 0


def aggregate_articles(raw_tweets: list[dict]) -> list[Article]:
    """
    将原始推文转换为 Article 列表，按 tweet_id 聚合多图。
    注意：此处不再调用 has_image 过滤。
    """
    aggregated: dict[int, Article] = {}

    for raw in raw_tweets:
        records = normalize_tweet(raw)
        for rec in records:
            tweet_id = rec["tweet_id"]
            if rec.get("type") == "text":
                if tweet_id not in aggregated:
                    aggregated[tweet_id] = Article(
                        tweet_id=tweet_id,
                        author=rec.get("author", {}),
                        date=rec.get("date", ""),
                        content=rec.get("content", ""),
                        retweet_id=rec.get("retweet_id", 0),
                        quote_id=rec.get("quote_id", 0),
                        reply_id=rec.get("reply_id", 0),
                        media=[],
                        ref=rec.get("ref"),
                    )
                continue

            media_item = MediaItem(
                media_id=rec.get("media_id", 0),
                url=rec.get("url", ""),
                filename=rec.get("filename", ""),
                extension=rec.get("extension", ""),
                width=rec.get("width", 0),
                height=rec.get("height", 0),
            )

            if tweet_id not in aggregated:
                aggregated[tweet_id] = Article(
                    tweet_id=tweet_id,
                    author=rec.get("author", {}),
                    date=rec.get("date", ""),
                    content=rec.get("content", ""),
                    retweet_id=rec.get("retweet_id", 0),
                    quote_id=rec.get("quote_id", 0),
                    reply_id=rec.get("reply_id", 0),
                    media=[media_item],
                    ref=rec.get("ref"),
                )
            else:
                # 合并多图：避免重复添加
                if all(m.media_id != media_item.media_id for m in aggregated[tweet_id].media):
                    aggregated[tweet_id].media.append(media_item)

    return sorted(aggregated.values(), key=lambda a: a.tweet_id, reverse=True)
