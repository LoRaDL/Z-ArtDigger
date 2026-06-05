"""
Configuration for the fanart-crawler system.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

# Generic defaults for open-source safety
DEFAULT_L3_PROMPT = (
    "You are an image classification expert. Analyze the image and perform classification.\n"
    "To facilitate program parsing, the final word of your response must conclude with [yes], [no], or [unsure]."
)
DEFAULT_VISION_LLM_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_VISION_LLM_API_KEY = ""
DEFAULT_VISION_LLM_MODEL = "generic-vision-model"
DEFAULT_VISION_LLM_MAX_WORKERS = 4
DEFAULT_MAX_LONG_EDGE = 800
DEFAULT_GALLERY_DB_PATH = None


@dataclass
class CrawlerConfig:
    # EMA smoothing coefficient for score calculation: next_score = alpha * local + (1-alpha) * prev
    alpha: float = 0.3

    # Maximum recursion depth for derived tasks
    max_depth: int = 5

    # Number of articles per process_task window (sliding window size)
    mini_batch: int = 20

    # Output directory for JSON metadata files
    output_dir: Path = field(default_factory=lambda: Path("output"))

    # Local image cache directory (keyed by media_id)
    image_cache_dir: Path = field(default_factory=lambda: Path("image_cache"))

    # SQLite database path for TimelineIslandsDB and ScanProgressDB
    sqlite_db_path: Path = field(default_factory=lambda: Path("crawler.db"))

    # Redis connection URL for CrawlerTaskPool and ClassificationCacheDB
    redis_url: str = "redis://localhost:6379/0"

    # Minimum task score threshold. Tasks with a score lower than this will be discarded.
    task_score_threshold: float = 0.4

    # Directory containing cookie files
    cookies_dir: Path = field(default_factory=lambda: Path("cookies"))

    # Fixed API fetch size — 20 based on requirement
    api_fetch_size: int = 20

    # Optional HTTP proxy (e.g. "http://127.0.0.1:10808")
    proxy: str | None = None

    # Path to the gallery SQLite database (GalleryDB)
    gallery_db_path: Path | None = None

    # Prompt text for Level 3 VisionLLM classification
    l3_classification_prompt: str = DEFAULT_L3_PROMPT

    # VisionLLM connection configuration
    vision_llm_url: str = DEFAULT_VISION_LLM_URL
    vision_llm_api_key: str = DEFAULT_VISION_LLM_API_KEY
    vision_llm_model: str = DEFAULT_VISION_LLM_MODEL
    vision_llm_max_workers: int = DEFAULT_VISION_LLM_MAX_WORKERS
    max_long_edge: int = DEFAULT_MAX_LONG_EDGE
