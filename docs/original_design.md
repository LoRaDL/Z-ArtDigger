## 需求
一个能通过x平台粉丝互相转发的行为，进行树状搜索，并通过分类器分类的爬虫。

## 核心逻辑
伪代码示例
```python
def process_task(task):
    if is_scanned(task.author, task.center_artwork_id): 
        return
    
    # fetch_date_block 通过构建cursor直接读取user timeline前后(MINI_BATCH-1)/2条帖子和中间帖子
    articles = fetch_near_articles(task.author, task.center_artwork_id, MINI_BATCH)
    
    hits = 0
    for art in articles:
        if classifier(art) == 'related':
            hits += 1
            if is_repost_or_quote(art):
                # 横向衍生：发现新画师
                task_pool.put(author=art.reposted_author, 
                              center_artwork_id=art.reposted_id, 
                              score=task.score, 
                              depth=task.depth + 1)
            else:
                download_pic(art)
                
    # 计算当前块的分数并进行 EMA 平滑
    local_score = hits / len(articles) if articles else 0
    next_score = alpha * local_score + (1 - alpha) * task.score
    
    # 将决策权交给外置调度器（假设任务池会自动进行阈值和优先级判断）
    task_pool.put(author=task.author, 
                  center_artwork_id=shift_artwork_id(center_artwork_id,MINI_BATCH),  #获取2*MINI_BATCH个帖子前的帖子id
                  score=next_score, 
                  depth=task.depth)
                  
    task_pool.put(author=task.author, 
                  center_artwork_id=shift_artwork_id(center_artwork_id,-MINI_BATCH),  
                  score=next_score, 
                  depth=task.depth)

    mark_as_scanned(task.author, task.center_id)
```
## 层级设计
### 决策层 (Decision)
- 组件：爬虫逻辑任务、process_task

- 核心池：Crawler Task Pool (优先级：Score)

- 数据库：扫描进度记录、L0 已分类 ID 缓存

- 解决的问题：决定“接下来去哪挖”，控制递归深度。

### 数据流通层 (I/O)
- 组件：API 请求器、图片下载器

- 核心池：API Request Pool (优先级：Score)

- 数据库：Timeline Islands DB (作者-帖子孤岛)，......

- 解决的问题：对抗 Twitter 速率限制，实现数据的“随机存取”。

### 认知分类层 (Brain)
- 组件：pHash 匹配器、Vision LLM

- 核心池：Classifier Pool (优先级：Score)

- 数据库：外部Gallery DB (pHash 索引、Metadata)，内部已匹配分类缓存

- 解决的问题：解决“这张图是不是 Fanart”的昂贵计算。

## 组件选型

- 使用redis，sqlite。

## 一些函数设计
### shift_artwork_id和fetch_near_articles
- 基于./fetcher，获取推文的metadata
- 能处理前/后第n条帖子不存在的情况（使用步骤中最后一条存在的），同时可以添加全局日期范围约束
- 被调用时，先查询Timeline Islands DB，如果有未命中的部分，则需要请求api，将请求api的需求发送到API Request Pool。

### classifier()
- 被调用时，发送到Classifier Pool，并等待结果返回。

带四级分类：

- L0：tweet_id在当前项目中已经被分类过，使用缓存的related或unrelated。

- L1：使用url在已知外部gallery数据库做url匹配，如果有则related。

- L2：下载图片，在已知外部gallery数据库的pHash匹配，如果距离小于阈值，则related

- L3：已经下载图片，送入visionLLM分类。visionLLM分类器解耦，接收多个线程的请求，以队列处理。

外部数据库和visionLLM分类器后续再接入，classifier具体分类部分可以先留空
### download_pic() → archive_pic()
- 仅保存图片对应名字的 json，不下载图片。

### ImageDownloader（图片下载模块，供 Classifier L2/L3 使用）
- 先查找本地 ImageCacheDir 缓存（以 media_id 为文件名），命中则直接返回路径。
- 未命中则从网络下载图片并写入缓存，返回本地路径。

### 对于多图帖子：
- 在fetch_near_articles中，以帖子为单位，不分离多图片。直接把包含多个图片的art变量发给classifier。
在下载和保存metadata时，视为独立的图片，确保download保存的json符合这一要求

### 数据缓存与复用设计
- 所有包含图片下载的地方，包括 classifier 的 L2/L3，统一通过 ImageDownloader 共用缓存。archive_pic 不涉及图片下载。

- 对于shift_artwork_id和fetch_near_articles，“本地缓存优先 + 惰性求值（Lazy Evaluation）”架构。未命中需要网络请求时，使用api进行按需增量请求。请求长度是固定的大（无论Batch大小）。行为有向前和向后请求（基于Top/Bottom cursor）。由于X限制的是请求数，并且单次最大请求可以达到20，所以每次请求都使用20大小。

- 引入“区间追踪（Range Tracking）”，已知id随着时间递增但是不连续。使用合适的数据结构，处理连续孤岛表，并有完善的孤岛合并等逻辑

## todo
quote行为中如果当前帖子的作者和被quote的作者都发了图片，需要解决