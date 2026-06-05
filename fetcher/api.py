"""
Twitter GraphQL API 底层封装
"""
import json
import base64
import logging
import http.cookiejar
from curl_cffi import requests

logger = logging.getLogger(__name__)

BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
ENDPOINT_USER_BY_NAME  = "https://x.com/i/api/graphql/qW5u-DAuXpMEG0zA1F7UGQ/UserByScreenName"
ENDPOINT_USER_TWEETS   = "https://x.com/i/api/graphql/E8Wq-_jFSaU7hxVcuOPR9g/UserTweets"
ENDPOINT_TWEET_BY_ID   = "https://x.com/i/api/graphql/qxWQxcMLiTPcavz9Qy5hwQ/TweetResultByRestId"


class TwitterRateLimitError(Exception):
    """自定义速率限制异常，支持主动回避逻辑"""
    def __init__(self, remaining: int, reset: int, limit: int = 180, is_proactive: bool = False):
        self.remaining = remaining
        self.reset = reset
        self.limit = limit
        self.is_proactive = is_proactive
        msg = f"Rate limit {'proactive' if is_proactive else 'hit'}: {remaining}/{limit} left, reset at {reset}"
        super().__init__(msg)

FEATURES_USER = {
    "hidden_profile_subscriptions_enabled": True,
    "payments_enabled": False,
    "rweb_xchat_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}

FEATURES_TWEETS = {
    "rweb_video_screen_enabled": False,
    "payments_enabled": False,
    "rweb_xchat_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
}


def load_cookies(cookies_file: str) -> dict:
    jar = http.cookiejar.MozillaCookieJar(cookies_file)
    jar.load(ignore_discard=True, ignore_expires=True)
    return {c.name: c.value for c in jar if "x.com" in c.domain or "twitter.com" in c.domain}


def make_session(cookies: dict, proxy: str | None = None) -> requests.Session:
    """创建带有真实浏览器指纹的 curl_cffi Session"""
    session = requests.Session(impersonate="chrome120")
    
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    
    csrf = cookies.get("ct0", "")
    session.headers.update({
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://x.com/",
        "content-type": "application/json",
        "authorization": f"Bearer {BEARER}",
        "x-csrf-token": csrf,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
    })
    
    # 注入 cookies
    for k, v in cookies.items():
        session.cookies.set(k, v, domain=".x.com")
        
    return session


def _get(session: requests.Session, url: str, params: dict) -> dict:
    import random
    resp = session.get(url, params=params, timeout=30)
    
    # 获取速率限制信息
    rem = resp.headers.get("x-rate-limit-remaining")
    rst = resp.headers.get("x-rate-limit-reset")
    lmt = resp.headers.get("x-rate-limit-limit")
    
    # 同步到 session 对象上，便于外部监控
    if rem is not None:
        session._last_rate_limit = {
            "remaining": int(rem),
            "reset": int(rst) if rst else 0,
            "limit": int(lmt) if lmt else 180,
            "endpoint": url.split("/")[-1]
        }

    # 1. 实时同步 CSRF Token
    new_csrf = session.cookies.get("ct0")
    if new_csrf and session.headers.get("x-csrf-token") != new_csrf:
        session.headers["x-csrf-token"] = new_csrf
        logger.debug("Synced x-csrf-token from cookie: %s...", new_csrf[:8])

    # 2. 提前量随机回避逻辑
    if rem is not None:
        rem_int = int(rem)
        lmt_int = int(lmt or 180)
        # 当剩余请求数 < 6 且小于总量的 10% 时，有概率主动触发
        if rem_int < 6 and rem_int <= random.randrange(1, 6):
            logger.warning("Proactive rate limit avoidance triggered: %d remaining", rem_int)
            raise TwitterRateLimitError(rem_int, int(rst or 0), limit=lmt_int, is_proactive=True)

    resp.raise_for_status()
    
    try:
        data = resp.json()
    except Exception as exc:
        logger.error("Failed to decode JSON from %s. Status: %d, Text: %r", 
                     url, resp.status_code, resp.text[:200])
        raise ValueError(f"Invalid JSON response: {exc}") from exc

    # 有时 Twitter 返回 200 OK，但 JSON 内部包含错误信息
    if "errors" in data:
        msgs = [e.get("message", "Unknown error") for e in data["errors"]]
        # 如果是内容被禁或其他非暂时性错误，也会在这里体现
        logger.warning("Twitter API returned errors for %s: %s", url.split("/")[-1], msgs)
    return data


def get_user_id(session: requests.Session, screen_name: str) -> str:
    params = {
        "variables": json.dumps({
            "screen_name": screen_name,
            "withSafetyModeUserFields": True,
        }),
        "features": json.dumps(FEATURES_USER),
        "fieldToggles": json.dumps({"withAuxiliaryUserLabels": False}),
    }
    data = _get(session, ENDPOINT_USER_BY_NAME, params)
    
    # 防御性解析 (Fix for KeyError: 'user')
    user_result = data.get("data", {}).get("user", {}).get("result", {})
    
    if not user_result:
        # 如果有报错，优先报 errors
        if "errors" in data:
            raise ValueError(f"Twitter API error: {data['errors'][0].get('message')}")
        raise KeyError(f"User '{screen_name}' not found or inaccessible (entire 'user' field missing from data)")

    typename = user_result.get("__typename")
    if typename == "UserUnavailable":
        raise ValueError(f"User '{screen_name}' is unavailable (suspended or protected)")
        
    rest_id = user_result.get("rest_id")
    if not rest_id:
        raise KeyError(f"User '{screen_name}' response structure valid but 'rest_id' is missing. Typename: {typename}")
        
    return rest_id


def fetch_user_tweets_raw(session: requests.Session, user_id: str, cursor: str | None = None, count: int = 20) -> dict:
    variables = {
        "userId": user_id,
        "count": count,
        "includePromotedContent": False,
        "withQuickPromoteEligibilityTweetFields": False,
        "withVoice": True,
    }
    if cursor:
        variables["cursor"] = cursor
    params = {
        "variables": json.dumps(variables),
        "features": json.dumps(FEATURES_TWEETS),
        "fieldToggles": json.dumps({"withArticlePlainText": False}),
    }
    return _get(session, ENDPOINT_USER_TWEETS, params)


def fetch_tweet_by_id_raw(session: requests.Session, tweet_id: int | str) -> dict | None:
    """用 TweetResultByRestId 获取单条推文的原始数据"""
    features = {k: v for k, v in FEATURES_TWEETS.items() if k != "rweb_video_screen_enabled"}
    params = {
        "variables": json.dumps({
            "tweetId": str(tweet_id),
            "withCommunity": False,
            "includePromotedContent": False,
            "withVoice": False,
        }),
        "features": json.dumps(features),
        "fieldToggles": json.dumps({
            "withArticleRichContentState": True,
            "withArticlePlainText": False,
            "withGrokAnalyze": False,
            "withDisallowedReplyControls": False,
        }),
    }
    data = _get(session, ENDPOINT_TWEET_BY_ID, params)
    result = data.get("data", {}).get("tweetResult", {}).get("result", {})
    if "tweet" in result:
        result = result["tweet"]
    return result if result else None


def extract_tweets_and_cursors(data: dict) -> tuple[list[dict], dict[str, str]]:
    """返回 (raw_tweets, {'Top': ..., 'Bottom': ...})"""
    tweets, cursors = [], {}
    try:
        result = data["data"]["user"]["result"]
        tl = result.get("timeline_v2") or result.get("timeline")
        instructions = tl["timeline"]["instructions"]
    except (KeyError, TypeError):
        return tweets, cursors

    for inst in instructions:
        if inst.get("type") != "TimelineAddEntries":
            continue
        for entry in inst.get("entries", []):
            c = entry.get("content", {})
            et = c.get("entryType")
            if et == "TimelineTimelineItem":
                item = c.get("itemContent", {})
                if item.get("itemType") == "TimelineTweet":
                    tr = item.get("tweet_results", {}).get("result", {})
                    if "tweet" in tr:
                        tr = tr["tweet"]
                    tweets.append(tr)
            elif et == "TimelineTimelineCursor":
                cursors[c.get("cursorType")] = c.get("value")

    return tweets, cursors


# ── user_id 缓存（轻量，避免重复请求） ───────────────────────────────────────
_user_id_cache: dict[str, str] = {}

# cursor 模板缓存（跨用户通用，首次请求后复用）
_cursor_template: bytes | None = None


def _cached_user_id(session: requests.Session, screen_name: str) -> str:
    if screen_name not in _user_id_cache:
        _user_id_cache[screen_name] = get_user_id(session, screen_name)
    return _user_id_cache[screen_name]


def _get_cursor_template(session: requests.Session, screen_name: str) -> bytes:
    """获取 cursor 模板（任意用户均可，跨用户通用）"""
    global _cursor_template
    if _cursor_template is None:
        from .cursor import decode
        uid = _cached_user_id(session, screen_name)
        data = fetch_user_tweets_raw(session, uid, count=5)
        _, cursors = extract_tweets_and_cursors(data)
        _cursor_template = decode(cursors["Bottom"])
    return _cursor_template


def fetch_tweets_from(
    session: requests.Session,
    screen_name: str,
    from_id: int | str,
    count: int,
) -> list[dict]:
    """
    从 from_id 开始，往更旧方向获取 count 条原始 tweet。
    
    逻辑：构造一个指向 from_id 的 Bottom cursor，然后向旧抓取。
    """
    from .cursor import make
    template = _get_cursor_template(session, screen_name)
    # 始终使用 direction=2 (Bottom) 往旧翻
    cursor_str = make(template, str(from_id), direction=2)
    uid = _cached_user_id(session, screen_name)
    data = fetch_user_tweets_raw(session, uid, cursor=cursor_str, count=count)
    raw_tweets, _ = extract_tweets_and_cursors(data)

    # 排序并截取（确保是降序返回）
    raw_tweets.sort(
        key=lambda t: int(t.get("rest_id", 0)),
        reverse=True,
    )
    return raw_tweets[:count]
