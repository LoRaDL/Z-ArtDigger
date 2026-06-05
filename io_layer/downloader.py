"""
ImageDownloader: local cache-first image fetcher for Classifier L2/L3.
"""
from __future__ import annotations

import logging
import mimetypes
import urllib.parse
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


class ImageDownloader:
    """
    Downloads images and caches them locally by media_id.

    Cache layout: cache_dir/{media_id}.{ext}

    On cache hit  → return existing path immediately (no network I/O).
    On cache miss → download from url, write to cache, return path.
    On failure    → propagate exception to caller (Classifier handles degradation).
    """

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, media_id: int, url: str) -> Path:
        """
        Return the local path for the image.
        Uses the filename from the URL (e.g. HCAmRTuaMAE0bA7.jpg) as the primary identifier.
        """
        filename = self._get_target_filename(url, media_id)
        
        cached = self._find_cached(filename)
        if cached is not None:
            logger.info("      [IMG] Cache hit  %s", cached.name)
            return cached

        logger.info("      [IMG] Cache miss media_id=%d  downloading %s ...", media_id, url)
        return self._download(filename, url)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_target_filename(self, url: str, media_id: int) -> str:
        """从 URL 提取文件名，失败则回退到 media_id."""
        path = urllib.parse.urlparse(url).path
        name = Path(path).name
        if name and "." in name:
            return name
        return f"{media_id}.jpg"  # 兜底增加后缀

    def _find_cached(self, filename: str) -> Path | None:
        """查找缓存：仅支持精确的文件名匹配。"""
        p = self.cache_dir / filename
        return p if p.exists() else None

    def _download(self, filename: str, url: str) -> Path:
        """下载并保存为指定的文件名。"""
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        dest = self.cache_dir / filename
        # 如果 URL 没后缀或提取的名字没后缀，则根据 content-type 推断（较少见）
        if not dest.suffix:
            ext = self._infer_extension(url, response)
            dest = dest.with_suffix(f".{ext}")

        dest.write_bytes(response.content)
        logger.info("      [IMG] Downloaded → %s (%d bytes)",
                    dest.name, len(response.content))
        return dest

    @staticmethod
    def _infer_extension(url: str, response: requests.Response) -> str:
        """
        Derive a file extension from the URL path first, then fall back to
        the Content-Type header.  Defaults to 'jpg' if nothing matches.
        """
        # 1. Try URL path (strip query string first)
        parsed_path = urllib.parse.urlparse(url).path
        suffix = Path(parsed_path).suffix  # e.g. ".jpg"
        if suffix:
            return suffix.lstrip(".")

        # 2. Fall back to Content-Type header
        content_type = response.headers.get("Content-Type", "")
        mime_type = content_type.split(";")[0].strip()
        ext = mimetypes.guess_extension(mime_type)
        if ext:
            # mimetypes may return ".jpeg" — normalise to "jpg"
            ext = ext.lstrip(".")
            if ext == "jpeg":
                ext = "jpg"
            return ext

        return "jpg"
