# Twitter GraphQL UserTweets Cursor 逆向分析

## !!!以下内容已经失效，最新的测试发现不能向新方向翻页，以下的新方向翻页为错误结论。


## 背景

Twitter 的 `UserTweets` GraphQL 接口（`/graphql/.../UserTweets`）通过 cursor 实现翻页。
表面上只能从最新帖子顺序往前翻，但通过逆向 cursor 结构，可以从任意已知 tweet_id 直接跳入时间线，向新或向旧两个方向翻页。

---

## Cursor 编码

Cursor 是 **URL-safe Base64** 编码的 Thrift 二进制结构。

注意：必须用 `base64.urlsafe_b64decode`，不能用标准 `base64.b64decode`，否则 `_` 会被错误解码。

```python
import base64

def decode_cursor(cursor: str) -> bytes:
    pad = '=' * (4 - len(cursor) % 4)
    return base64.urlsafe_b64decode(cursor + pad)

def encode_cursor(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')
```

---

## Cursor 二进制结构

以一个实际 Bottom cursor 为例（49 字节）：

```
0c 00 07  0a 00 01  1c 53 ea d2 a2  ff ff ed
0b 00 02  00 00 00 13  32 30 33 32 30 34 36 31 33 33 35 31 38 39 35 30 36 32 33
08 00 03  00 00 00 02  00 00
```

| offset | 长度 | 含义 |
|--------|------|------|
| 0–2    | 3    | Thrift 结构头 `0c 00 07`（STRUCT, field 7） |
| 3–5    | 3    | INT64 字段头 `0a 00 01`（field 1） |
| 6–13   | 8    | INT64 锚点值（见下文） |
| 14–16  | 3    | STRING 字段头 `0b 00 02`（field 2） |
| 17–20  | 4    | 字符串长度（uint32 big-endian） |
| 21–39  | 19   | **tweet_id ASCII 明文**（定位锚点） |
| 40–42  | 3    | INT32 字段头 `08 00 03`（field 3） |
| 43–46  | 4    | INT32 值，最后一字节为**方向标志** |
| 47–48  | 2    | 结构结束 |

### 关键字段

**tweet_id（offset 21，ASCII 明文）**

这是 cursor 真正的定位锚点，API 根据这个 id 决定从哪里开始返回帖子。

- Bottom cursor：存该批帖子的 **last_id**（最旧那条），往旧方向翻页时返回比它更早的帖子
- Top cursor：存该批帖子的 **first_id**（最新那条），往新方向翻页时返回比它更新的帖子

**方向标志（offset 46）**

| 值 | 方向 |
|----|------|
| `01` | Top，往更新方向翻 |
| `02` | Bottom，往更旧方向翻 |

**INT64 锚点（offset 6–13）**

- Top cursor：正偏移，值约为 `0x00002711`（固定）
- Bottom cursor：负偏移，值约为 `0xffffffed`（固定）

此字段影响不大，使用固定值即可。

---

## 构造任意 Cursor

只需一个真实 cursor 作为模板（任意一次 `UserTweets` 请求的响应里取），然后替换 tweet_id 和方向字节。

```python
import re, struct, base64

def make_cursor(template: bytes, tweet_id: str, direction: int) -> str:
    """
    direction: 1 = Top（往更新翻）, 2 = Bottom（往更旧翻）
    """
    b = bytearray(template)

    # 找并替换 tweet_id（ASCII 明文）
    m = re.search(r'\d{18,19}', bytes(b).decode('latin-1'))
    if not m:
        raise ValueError("cursor 模板里找不到 tweet_id")
    old_bytes = m.group().encode('ascii')
    new_bytes = tweet_id.encode('ascii')
    idx = bytes(b).find(old_bytes)

    # 更新长度字段
    b[idx-4:idx] = struct.pack('>I', len(new_bytes))
    # 替换 id
    tail = bytes(b)[idx+len(old_bytes):]
    b = bytearray(bytes(b)[:idx] + new_bytes + tail)

    # 更新锚点和方向
    if direction == 1:
        b[10:14] = struct.pack('>i', 0x2711)
        b[-3] = 1
    else:
        b[10:14] = struct.pack('>i', -19)
        b[-3] = 2

    return base64.urlsafe_b64encode(bytes(b)).decode().rstrip('=')
```

---

## 使用示例

```python
# 1. 任意请求一次，拿模板 cursor
data = fetch_user_tweets(opener, user_id, count=5)
_, cursors = extract_cursors(data)
template = decode_cursor(cursors['Bottom'])

# 2. 已知某条帖子的 tweet_id，构造前后 cursor
known_id = "2030942239468474696"

bottom = make_cursor(template, known_id, direction=2)  # 拿更旧的帖子
top    = make_cursor(template, known_id, direction=1)  # 拿更新的帖子

older_tweets = fetch_user_tweets(opener, user_id, cursor=bottom)
newer_tweets = fetch_user_tweets(opener, user_id, cursor=top)
```

结果：`older_tweets` 全部比 `known_id` 更早，`newer_tweets` 全部比 `known_id` 更新，两批夹住目标帖子。

---

## 注意事项

- 模板 cursor 可以**跨用户复用**：用任意用户的 cursor 作为模板，替换 tweet_id 后对其他用户同样有效。程序启动时只需请求一次即可，无需每个用户单独获取模板。
- 方向字节位置是 `b[-3]`（倒数第3字节），不同版本的 API 变更可能导致偏移变化，建议通过对比 Top/Bottom cursor 动态确认。
- cursor 里的 INT64 锚点（offset 6–13）目前使用固定值，实际影响待进一步验证。


## important 拿更新的帖子方法已经失效