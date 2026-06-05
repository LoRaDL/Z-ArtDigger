"""
ScanProgressDB — SQLite-backed store for scan progress tracking.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


_DDL_SCAN_PROGRESS = """
CREATE TABLE IF NOT EXISTS scan_progress (
    author            TEXT NOT NULL,
    center_artwork_id INTEGER NOT NULL,
    scanned_at        TEXT NOT NULL,
    PRIMARY KEY (author, center_artwork_id)
);
"""


class ScanProgressDB:
    """
    SQLite-backed store for scan progress tracking.
    Thread-safe via a threading.Lock.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        with self._conn:
            self._conn.execute(_DDL_SCAN_PROGRESS)

    def is_scanned(self, author: str, center_artwork_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM scan_progress WHERE author = ? AND center_artwork_id = ?",
                (author, center_artwork_id),
            ).fetchone()
        return row is not None

    def mark_scanned(self, author: str, center_artwork_id: int) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """INSERT OR REPLACE INTO scan_progress (author, center_artwork_id, scanned_at)
                       VALUES (?, ?, ?)""",
                    (author, center_artwork_id, now),
                )
