from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pathlib import Path
import sqlite3
import os
import sys
import redis
import json
import tomllib
import shutil
import asyncio

# 确保能导入项目的 crawler 模块
sys.path.append(os.getcwd())

from storage.timeline_db import TimelineIslandsDB

def get_output_dir() -> Path:
    with open(PROJECT_ROOT / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    return PROJECT_ROOT / cfg["storage"]["output_dir"]

from core.config import CrawlerConfig

app = FastAPI(title="ArtDigger Control Center API")

# 挂载图片缓存目录，方便前端预览
PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGE_CACHE_DIR = PROJECT_ROOT / "image_cache"
app.mount("/images", StaticFiles(directory=str(IMAGE_CACHE_DIR)), name="images")

# 允许跨域请求（前端开发必备）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 默认数据库路径（可以根据 config.toml 动态调整）
DB_PATH = Path("db/run3.db")
R = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

@app.get("/api/stats")
async def get_stats():
    """获取全局概览数据"""
    res = {
        "authors_count": 0,
        "islands_count": 0,
        "total_tweets": 0,
        "status": "Running",
        "api_queue": 0,
        "l3_queue": 0,
        "task_pool_size": 0,
        "active_workers": {},
        "last_vision": None
    }

    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            res["authors_count"] = conn.execute("SELECT COUNT(DISTINCT author) as count FROM island_ranges").fetchone()["count"]
            res["islands_count"] = conn.execute("SELECT COUNT(*) as count FROM island_ranges").fetchone()["count"]
            res["total_tweets"] = conn.execute("SELECT COUNT(*) as count FROM tweets").fetchone()["count"]
        finally:
            conn.close()
    
    # 从 Redis 读取实时状态
    try:
        res["api_queue"] = int(R.get("artdigger:status:api_queue") or 0)
        res["l3_queue"] = int(R.get("artdigger:status:l3_queue") or 0)
        res["l3_errors"] = int(R.get("artdigger:status:l3_errors") or 0)
        res["task_pool_size"] = R.zcard("crawler:tasks")
        
        # Classification hits stats
        raw_hits = R.hgetall("artdigger:status:classification_hits")
        res["classification_hits"] = {k: int(v) for k, v in raw_hits.items()} if raw_hits else {}
        
        last_v = R.get("artdigger:last_vision")
        if last_v:
            res["last_vision"] = json.loads(last_v)
        
        last_err = R.get("artdigger:status:l3_last_error")
        if last_err:
            res["last_l3_error"] = json.loads(last_err)
        
        raw_limit = R.get("artdigger:status:api_limit")
        if raw_limit:
            res["api_limit"] = json.loads(raw_limit)

        raw_workers = R.hgetall("artdigger:status:workers")
        processed_workers = {}
        for k, v in raw_workers.items():
            try:
                processed_workers[k] = json.loads(v)
            except Exception:
                processed_workers[k] = {"type": "Unknown", "author": "Unknown", "detail": v}
        res["active_workers"] = processed_workers
    except Exception:
        pass

    return res

@app.get("/api/stream/stats")
async def stream_stats():
    """SSE 流式推送全局统计数据"""
    async def event_generator():
        last_json = None
        while True:
            try:
                stats = await get_stats()
                current_json = json.dumps(stats)
                if current_json != last_json:
                    yield f"data: {current_json}\n\n"
                    last_json = current_json
            except Exception as e:
                yield f"event: error\ndata: {str(e)}\n\n"
            await asyncio.sleep(0.3)  # 300毫秒极低延迟检测
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/api/tasks")
async def get_tasks():
    """获取任务池队列详情 (Top 100)"""
    try:
        tasks = R.zrevrange("crawler:tasks", 0, 100, withscores=True)
        return [{"task": str(t), "score": s} for t, s in tasks]
    except Exception:
        return []

@app.get("/api/logs/{worker_id}")
async def get_log(worker_id: str):
    """获取指定 Worker 的最新日志"""
    key = f"artdigger:logs:worker:{worker_id}"
    try:
        logs = R.lrange(key, 0, -1)
        return logs
    except Exception:
        return []

@app.get("/api/authors")
async def get_authors():
    """获取所有博主及其孤岛概况"""
    if not DB_PATH.exists():
        return []
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT DISTINCT author FROM island_ranges").fetchall()
        return [row["author"] for row in rows]
    finally:
        conn.close()

@app.get("/api/islands/{author}")
async def get_author_islands(author: str):
    """获取特定博主的所有孤岛详情"""
    db = TimelineIslandsDB(DB_PATH)
    try:
        ranges = db.get_island_ranges(author)
        result = []
        for r in ranges:
            # 统计每个孤岛内的图片数
            img_count = db.count_has_image_in_range(author, r.min_id, r.max_id)
            result.append({
                "min_id": str(r.min_id),
                "max_id": str(r.max_id),
                "oldest": r.oldest_boundary,
                "newest": r.newest_boundary,
                "image_count": img_count
            })
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/timeline_data")
async def get_timeline_data():
    """获取所有博主的详细时间线数据(整合接口)"""
    if not DB_PATH.exists():
        return {}
        
    db = TimelineIslandsDB(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        author_rows = conn.execute("SELECT DISTINCT author FROM island_ranges").fetchall()
        authors = [row["author"] for row in author_rows]
        
        result = {}
        for author in authors:
            ranges = db.get_island_ranges(author)
            islands_data = []
            for r in ranges:
                img_count = db.count_has_image_in_range(author, r.min_id, r.max_id)
                islands_data.append({
                    "min_id": str(r.min_id),
                    "max_id": str(r.max_id),
                    "oldest": r.oldest_boundary,
                    "newest": r.newest_boundary,
                    "image_count": img_count
                })
            
            tweet_rows = conn.execute(
                "SELECT tweet_id, media_json FROM tweets WHERE author = ? ORDER BY tweet_id ASC", 
                (author,)
            ).fetchall()
            
            tweets_data = []
            for row in tweet_rows:
                mj = row["media_json"]
                has_image = bool(mj and mj != "[]" and mj != "null")
                tweets_data.append({"id": str(row["tweet_id"]), "has_image": has_image})
                
            result[author] = {
                "islands": islands_data,
                "tweets": tweets_data
            }
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/archives")
async def get_archives(page: int = 1, limit: int = 24):
    output_dir = get_output_dir()
    if not output_dir.exists():
        return {"items": [], "total": 0, "page": page, "limit": limit}
    
    files = list(output_dir.glob("*.json"))
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    
    total = len(files)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_files = files[start_idx:end_idx]
    
    res = []
    for f in page_files:
        if not f.name.endswith(".json"): continue
        image_name = f.name[:-5]
        has_image = (IMAGE_CACHE_DIR / image_name).exists()
        
        try:
            with open(f, "r", encoding="utf-8") as jf:
                data = json.load(jf)
                # handle different json formats gracefully
                content = data.get("content") or data.get("full_text") or ""
                author_name = data.get("author", {}).get("name") or data.get("user", {}).get("name") or "Unknown"
                res.append({
                    "filename": f.name,
                    "image_name": image_name if has_image else None,
                    "tweet_id": data.get("tweet_id") or data.get("id_str"),
                    "author": author_name,
                    "content": content[:100] + ("..." if len(content) > 100 else ""),
                    "created_at": data.get("date") or data.get("created_at", "")
                })
        except Exception:
            pass
            
    return {"items": res, "total": total, "page": page, "limit": limit}

@app.get("/api/tweets/{author}")
async def get_author_tweets(author: str):
    """获取某博主在库内的所有推文节点，用于绘制精确时间线。"""
    if not DB_PATH.exists():
        return []
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT tweet_id, media_json FROM tweets WHERE author = ? ORDER BY tweet_id ASC", 
            (author,)
        ).fetchall()
        
        result = []
        for row in rows:
            mj = row["media_json"]
            has_image = bool(mj and mj != "[]" and mj != "null")
            result.append({"id": str(row["tweet_id"]), "has_image": has_image})
            
        return result
    finally:
        conn.close()

@app.delete("/api/archives/{filename}")
async def delete_archive(filename: str):
    """
    删除归档：将 JSON 文件移动到 'deleted' 子目录。
    """
    output_dir = get_output_dir()
    src_path = output_dir / filename
    
    if not src_path.exists():
        raise HTTPException(status_code=404, detail="Archive file not found")
    
    deleted_dir = output_dir / "deleted"
    deleted_dir.mkdir(exist_ok=True)
    
    dest_path = deleted_dir / filename
    
    try:
        # 如果目标已存在，先删除旧的
        if dest_path.exists():
            dest_path.unlink()
        shutil.move(str(src_path), str(dest_path))
        return {"status": "success", "message": f"Moved {filename} to deleted folder"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to move file: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
