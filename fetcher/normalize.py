"""
把 GraphQL 原始 tweet 转换成与自定义 JSON 格式
"""
from __future__ import annotations
import re
import datetime
from email.utils import parsedate_to_datetime


def _parse_date(s: str) -> str:
    """'Sun Apr 05 14:31:49 +0000 2026' -> '2026-04-05 14:31:49'"""
    try:
        dt = parsedate_to_datetime(s).astimezone(datetime.timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s


def _user(u: dict) -> dict:
    lg   = u.get("legacy", {})
    core = u.get("core", {})
    return {
        "id":               int(u.get("rest_id", 0)),
        "name":             core.get("screen_name", ""),
        "nick":             core.get("name", ""),
        "date":             _parse_date(core.get("created_at", "")),
        "profile_banner":   lg.get("profile_banner_url", ""),
        "favourites_count": lg.get("favourites_count", 0),
        "followers_count":  lg.get("followers_count", 0),
        "friends_count":    lg.get("friends_count", 0),
        "listed_count":     lg.get("listed_count", 0),
        "media_count":      lg.get("media_count", 0),
        "statuses_count":   lg.get("statuses_count", 0),
        "location":         u.get("location", {}).get("location", ""),
        "verified":         u.get("is_blue_verified", False),
        "protected":        u.get("privacy", {}).get("protected", False),
        "profile_image":    u.get("avatar", {}).get("image_url", ""),
        "description":      lg.get("description", ""),
    }


def _media_items(legacy: dict) -> list[dict]:
    """从 extended_entities 提取每张图片的基础信息"""
    media_list = (
        legacy.get("extended_entities", {}).get("media") or
        legacy.get("entities", {}).get("media") or
        []
    )
    items = []
    for m in media_list:
        if m.get("type") != "photo":
            continue
        orig = m.get("original_info", {})
        items.append({
            "filename":  m.get("media_url_https", "").rsplit("/", 1)[-1].rsplit(".", 1)[0],
            "extension": m.get("media_url_https", "").rsplit(".", 1)[-1].split("?")[0],
            "type":      m.get("type", "photo"),
            "width":     orig.get("width", 0),
            "height":    orig.get("height", 0),
            "url":       m.get("media_url_https", "") + "?name=small",
            "media_id":  int(m.get("id_str", 0)),
        })
    return items


def _ref_tweet(raw: dict) -> dict | None:
    """
    提取被转推或被引用帖子的摘要（统一格式）。
    包含: tweet_id, author, date, content, media
    """
    if not raw:
        return None
    lg    = raw.get("legacy", {})
    u_raw = raw.get("core", {}).get("user_results", {}).get("result", {})
    return {
        "tweet_id": int(raw.get("rest_id", 0)),
        "author":   _user(u_raw) if u_raw else {},
        "date":     _parse_date(lg.get("created_at", "")),
        "content":  lg.get("full_text", ""),
        "media":    _media_items(lg),
    }


def normalize_tweet(raw: dict, timeline_user: dict | None = None) -> list[dict]:
    """
    把一条 GraphQL tweet 转换 metadata 列表（每张图一条）。
    无图帖子返回包含一条无图字段的记录（type='text'）。

    timeline_user: 时间线所属用户的 raw user dict（区分 user vs author）
    """
    legacy = raw.get("legacy", {})
    core   = raw.get("core", {})

    tweet_id = int(raw.get("rest_id", legacy.get("id_str", 0)))
    date_str = _parse_date(legacy.get("created_at", ""))

    # author = 原帖作者（转推时为被转推者）
    author_raw = core.get("user_results", {}).get("result", {})
    author = _user(author_raw) if author_raw else {}

    # user = 时间线所属用户（转推时与 author 不同）
    user = _user(timeline_user) if timeline_user else author

    # retweet / quote / reply ids
    # 注意：Twitter API 可能在 result 下额外嵌套一层 "tweet"
    _rt_result = legacy.get("retweeted_status_result", {}).get("result", {})
    if "tweet" in _rt_result:
        _rt_result = _rt_result["tweet"]
    retweet_id = int(_rt_result.get("rest_id", 0) or 0)
    quote_id   = int(legacy.get("quoted_status_id_str", 0) or 0)
    reply_id   = int(legacy.get("in_reply_to_status_id_str", 0) or 0)
    source_id  = retweet_id

    # ref：retweet 和 quote 统一用同一字段
    ref = None
    date_original = ""
    if retweet_id:
        ref = _ref_tweet(_rt_result)
        if ref:
            date_original = ref["date"]
    elif quote_id:
        qsr = raw.get("quoted_status_result", {}).get("result", {})
        if "tweet" in qsr:
            qsr = qsr["tweet"]
        ref = _ref_tweet(qsr)

    # mentions / hashtags
    mentions = [
        {"id": int(m.get("id_str", 0)), "name": m.get("screen_name", ""), "nick": m.get("name", "")}
        for m in legacy.get("entities", {}).get("user_mentions", [])
    ]
    hashtags = [h.get("text", "") for h in legacy.get("entities", {}).get("hashtags", [])]

    # client name
    m = re.search(r'>([^<]+)<', raw.get("source", ""))
    source = m.group(1) if m else raw.get("source", "")

    base = {
        "tweet_id":        tweet_id,
        "retweet_id":      retweet_id,
        "quote_id":        quote_id,
        "reply_id":        reply_id,
        "source_id":       source_id,
        "conversation_id": int(legacy.get("conversation_id_str", tweet_id) or tweet_id),
        "date":            date_str,
        "author":          author,
        "user":            user,
        "lang":            legacy.get("lang", ""),
        "source":          source,
        "sensitive":       legacy.get("possibly_sensitive", False),
        "favorite_count":  legacy.get("favorite_count", 0),
        "quote_count":     legacy.get("quote_count", 0),
        "reply_count":     legacy.get("reply_count", 0),
        "retweet_count":   legacy.get("retweet_count", 0),
        "bookmark_count":  legacy.get("bookmark_count", 0),
        "view_count":      int(raw.get("views", {}).get("count", 0) or 0),
        "content":         legacy.get("full_text", ""),
        "mentions":        mentions,
        "hashtags":        hashtags,
        "category":        "twitter",
        "subcategory":     "timeline",
    }
    if date_original:
        base["date_original"] = date_original
    if ref:
        base["ref"] = ref

    media = _media_items(legacy)
    if not media:
        return [{**base, "type": "text", "filename": "", "extension": "", "num": 1, "count": 1}]

    count = len(media)
    return [
        {
            **base,
            "filename":  m["filename"],
            "extension": m["extension"],
            "type":      m["type"],
            "width":     m["width"],
            "height":    m["height"],
            "url":       m["url"],
            "media_id":  m["media_id"],
            "num":       i,
            "count":     count,
        }
        for i, m in enumerate(media, 1)
    ]
