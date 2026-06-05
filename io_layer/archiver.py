"""
archive_pic: 归档器 — 为 Article 中的每张图片生成 gallery-dl 兼容的 JSON 元数据文件。
不下载图片，不访问 ImageCacheDir。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from core.models import Article

logger = logging.getLogger(__name__)

# ---------- 归档专用后处理（不影响爬虫逻辑） ----------

_RE_RT_PREFIX = re.compile(r"^RT @(\w+):\s*")
_RE_TRAILING_URLS = re.compile(r"\s*(?:https?://\S+\s*)+$")


def _to_orig_url(url: str) -> str:
    """将 pbs.twimg.com 图片 URL 的 ?name=xxx 替换为 ?name=orig。"""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "name" in qs:
        qs["name"] = ["orig"]
        new_query = urlencode(qs, doseq=True)
        return urlunparse(parsed._replace(query=new_query))
    return url


def _clean_content(content: str) -> str:
    """删除 content 末尾的 https 链接。"""
    return _RE_TRAILING_URLS.sub("", content)


def _extract_rt_author(content: str, author: dict) -> tuple[str, dict]:
    """
    若 content 以 'RT @xxx: ' 开头，去掉该前缀并把 author['name'] 改为 xxx。
    返回 (处理后的 content, 可能修改过的 author 副本)。
    """
    m = _RE_RT_PREFIX.match(content)
    if m:
        rt_name = m.group(1)
        content = content[m.end():]
        author = {**author, "name": rt_name}
    return content, author


def archive_pic(article: Article, output_dir: Path) -> None:
    """
    为 article 中的每张图片生成独立的 JSON 元数据文件。

    文件名格式: {tweet_id}_{num}.json
    num 从 1 开始，count 为该帖子总图片数。

    单张图片 JSON 生成失败不中断其余图片的处理。
    不下载图片，不访问 ImageCacheDir。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    count = len(article.media)
    logger.info("    [ARCHIVE] tweet_id=%d  %d image(s) → %s",
                article.tweet_id, count, output_dir)

    # 归档后处理：清洗 content 和 author（不修改原 article）
    content = _clean_content(article.content)
    content, author = _extract_rt_author(content, article.author)

    for num, media in enumerate(article.media, start=1):
        try:
            metadata = {
                "tweet_id": article.tweet_id,
                "author": author,
                "date": article.date,
                "content": content,
                "retweet_id": article.retweet_id,
                "quote_id": article.quote_id,
                "reply_id": article.reply_id,
                "num": num,
                "count": count,
                "filename": media.filename,
                "extension": media.extension,
                "url": _to_orig_url(media.url),
                "media_id": media.media_id,
                "category": "twitter",
                "subcategory": "timeline",
            }
            json_path = output_dir / f"{media.filename}.{media.extension}.json"
            json_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("    [ARCHIVE] ✓ %s  (url=%s)",
                        json_path.name, media.url)
        except Exception as exc:
            logger.error(
                "    [ARCHIVE] ✗ image %d/%d for tweet %d: %s",
                num, count, article.tweet_id, exc,
            )
