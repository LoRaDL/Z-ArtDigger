"""
Entry point for the fanart-crawler.

Usage:
    python main.py --author <screen_name> --tweet-id <id> [options]

Options:
    --author      Twitter screen_name (required)
    --tweet-id    Center artwork tweet ID (required)
    --score       Initial score [0.0, 1.0] (default: 1.0)
    --depth       Initial depth (default: 0)
    --config      Path to TOML config file (default: config.toml)
"""
from __future__ import annotations

import argparse
from dataclasses import fields
import logging
import sys
import tomllib
from pathlib import Path

from io_layer.api_pool import APIRequestPool
from brain.cache import ClassificationCacheDB
from brain.classifier import ClassifierPool
from core.config import CrawlerConfig
from core.crawler import Crawler
from storage import ScanProgressDB, TimelineIslandsDB
from io_layer.downloader import ImageDownloader
from brain.l3pool import L3Pool
from core.models import Task
from core.task_pool import CrawlerTaskPool
from fetcher.cookie_pool import CookieSessionPool
from fetcher.api import load_cookies, make_session


def load_config(config_path: Path) -> CrawlerConfig:
    """Load CrawlerConfig from a TOML file. Supports flat and nested table structures."""
    if not config_path.exists():
        return CrawlerConfig()

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    flat_data: dict = {}

    def flatten(d: dict, prefix: str = "") -> None:
        for k, v in d.items():
            if isinstance(v, dict):
                flatten(v, prefix=f"{k}_")
                for sub_k, sub_v in v.items():
                    flat_data[sub_k] = sub_v
                    if k == "vision_llm" and sub_k == "prompt":
                        flat_data["l3_classification_prompt"] = sub_v
            else:
                flat_data[f"{prefix}{k}"] = v
                if prefix == "":
                    flat_data[k] = v

    flatten(data)

    kwargs: dict = {}
    for fld in fields(CrawlerConfig):
        name = fld.name
        if name in flat_data:
            val = flat_data[name]
            if "Path" in str(fld.type):
                kwargs[name] = Path(val) if (val is not None and val != "") else None
            else:
                kwargs[name] = val

    return CrawlerConfig(**kwargs)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="fanart-crawler: recursively discover and archive fanart from Twitter/X"
    )
    parser.add_argument(
        "--author",
        required=True,
        help="Twitter screen_name of the starting artist",
    )
    parser.add_argument(
        "--tweet-id",
        required=True,
        type=int,
        dest="tweet_id",
        help="Center artwork tweet ID (integer)",
    )
    parser.add_argument(
        "--score",
        type=float,
        default=1.0,
        help="Initial task score in [0.0, 1.0] (default: 1.0)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=0,
        help="Initial task depth (default: 0)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Path to TOML config file (default: config.toml)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not (0.0 <= args.score <= 1.0):
        print(f"Error: --score must be in [0.0, 1.0], got {args.score}", file=sys.stderr)
        sys.exit(1)

    config = load_config(args.config)

    # ── 组装所有组件 ──────────────────────────────────────────────────────
    session_pool = CookieSessionPool(config.cookies_dir, proxy=config.proxy)

    timeline_db = TimelineIslandsDB(config.sqlite_db_path)
    scan_db = ScanProgressDB(config.sqlite_db_path)
    task_pool = CrawlerTaskPool(
        config.redis_url,
        task_score_threshold=config.task_score_threshold
    )
    classify_cache = ClassificationCacheDB(config.redis_url)
    image_downloader = ImageDownloader(config.image_cache_dir)
    l3pool = L3Pool(
        image_downloader=image_downloader,
        redis_client=task_pool.redis,
        max_workers=config.vision_llm_max_workers,
        prompt=config.l3_classification_prompt,
        api_url=config.vision_llm_url,
        api_key=config.vision_llm_api_key,
        model=config.vision_llm_model,
        max_long_edge=config.max_long_edge,
    )
    classifier_pool = ClassifierPool(
        cache_db=classify_cache,
        gallery_db_path=config.gallery_db_path,
        image_downloader=image_downloader,
        l3pool=l3pool,
    )
    api_pool = APIRequestPool(session_pool=session_pool, redis_client=task_pool.redis)

    crawler = Crawler(
        config=config,
        task_pool=task_pool,
        scan_db=scan_db,
        timeline_db=timeline_db,
        classifier_pool=classifier_pool,
        api_pool=api_pool,
        session_pool=session_pool,
        num_workers=4,
    )

    initial_task = Task(
        author=args.author,
        center_artwork_id=args.tweet_id,
        score=args.score,
        depth=args.depth,
    )

    try:
        crawler.start(initial_task)
        crawler.run()
    except KeyboardInterrupt:
        print("\n" + "!"*60)
        print("  [STOP] Interrupt received, shutting down gracefully...")
        print("!"*60)
        crawler.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
