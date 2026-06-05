"""
Cursor 构造工具
"""
import re
import struct
import base64


def decode(cursor: str) -> bytes:
    pad = '=' * (4 - len(cursor) % 4)
    return base64.urlsafe_b64decode(cursor + pad)


def encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def snowflake_to_timestamp(sf_id: int | str) -> float:
    """Snowflake ID 转 Unix 时间戳（秒）。"""
    return ((int(sf_id) >> 22) + 1288834974657) / 1000.0


def timestamp_to_snowflake(ts: float) -> int:
    """Unix 时间戳（秒）转 Snowflake ID 下界。"""
    return int(ts * 1000 - 1288834974657) << 22


def make(template: bytes, tweet_id: str | int, direction: int) -> str:
    """
    用模板构造指向 tweet_id 的 cursor。
    direction: 2 = Bottom (往更旧翻，返回比 tweet_id 更旧的帖子)
    注：本系统已废弃 direction=1 (Top)，向新查找通过日期偏移实现。
    """
    tweet_id = str(tweet_id)
    b = bytearray(template)
    m = re.search(r'\d{18,19}', bytes(b).decode('latin-1'))
    if not m:
        raise ValueError("cursor 模板里找不到 tweet_id")
    old_bytes = m.group().encode('ascii')
    new_bytes = tweet_id.encode('ascii')
    idx = bytes(b).find(old_bytes)

    # 替换 tweet_id 长度前缀和内容
    b[idx-4:idx] = struct.pack('>I', len(new_bytes))
    tail = bytes(b)[idx + len(old_bytes):]
    b = bytearray(bytes(b)[:idx] + new_bytes + tail)

    # direction 标志位
    b[-3] = direction

    return encode(bytes(b))
