"""
Utility functions for processing Article objects.
"""
from __future__ import annotations

from core.models import Article, MediaItem


def split_quote_article(article: Article) -> list[Article]:
    """
    将一个 quote 帖子展开为 1 或 2 个 Article：
    - 若 quote_id == 0 或当前帖子无图，直接返回 [article]（不拆分）
    - 若 quote_id != 0 且当前帖子有图：
        Article A：当前帖子自身的图片，ref 置为 None，quote_id 置为 0
        Article B：以 ref 中被引用帖子的 author/tweet_id/media 构建，
                   retweet_id=0, quote_id=0, reply_id=0
      返回 [Article_A, Article_B]
    """
    # 获取被引用帖的媒体信息
    ref = article.ref or {}
    ref_media = ref.get("media", [])
    
    # 不拆分的情况：
    # 1. 无引用 (quote_id == 0)
    # 2. 虽然有引用，但原帖和被引用帖都无图（拆分没有意义）
    if article.quote_id == 0 or (not article.media and not ref_media):
        return [article]

    # Article A：当前帖子自身图片，清除 ref 和 quote_id
    # 注意：即便 article_a.media 是空的也没关系，下游的 has_image 会处理它
    article_a = Article(
        tweet_id=article.tweet_id,
        author=article.author,
        date=article.date,
        content=article.content,
        retweet_id=article.retweet_id,
        quote_id=0,
        reply_id=article.reply_id,
        media=article.media,
        ref=None,
    )

    # Article B：从 ref 构建被引用帖子的 Article
    ref = article.ref or {}
    raw_media = ref.get("media", [])

    # ref["media"] 可能是 MediaItem 列表或 dict 列表，统一处理
    media_b: list[MediaItem] = []
    for item in raw_media:
        if isinstance(item, MediaItem):
            media_b.append(item)
        elif isinstance(item, dict):
            media_b.append(MediaItem(
                media_id=item.get("media_id", 0),
                url=item.get("url", ""),
                filename=item.get("filename", ""),
                extension=item.get("extension", ""),
                width=item.get("width", 0),
                height=item.get("height", 0),
            ))

    article_b = Article(
        tweet_id=ref.get("tweet_id", article.quote_id),
        author=ref.get("author", {}),
        date=ref.get("date", ""),
        content=ref.get("content", ""),
        retweet_id=0,
        quote_id=0,
        reply_id=0,
        media=media_b,
        ref=None,
    )

    return [article_a, article_b]
