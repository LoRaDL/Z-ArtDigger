from __future__ import annotations

import logging
import time
import random
import threading
from pathlib import Path
from typing import Any
from curl_cffi import requests

from fetcher.api import load_cookies, make_session

logger = logging.getLogger(__name__)


class CookieSessionPool:
    """
    Cookie session pool for rotating curl_cffi sessions.
    
    Supports:
    - Automatic loading of all *.txt cookies in cookies_dir.
    - Rate limit tracking per session.
    - Thread-safe session rotation.
    - Dynamic removal of locked/invalid accounts.
    """

    def __init__(self, cookies_dir: str | Path, proxy: str | None = None) -> None:
        self.cookies_dir = Path(cookies_dir)
        self.proxy = proxy
        self.sessions: list[requests.Session] = []
        self.rate_limit_resets: dict[requests.Session, float] = {}  # session -> reset timestamp
        self.session_names: dict[requests.Session, str] = {}        # session -> cookie filename
        self.lock = threading.Lock()
        
        # Keep track of rotation order
        self._current_index = 0
        
        self.load_sessions()

    def load_sessions(self) -> None:
        """Scan cookies_dir for all Mozilla cookie files and construct sessions."""
        if not self.cookies_dir.exists() or not self.cookies_dir.is_dir():
            logger.warning("CookieSessionPool: Directory %s does not exist.", self.cookies_dir)
            return

        cookie_files = list(self.cookies_dir.glob("*.txt"))
        if not cookie_files:
            logger.warning("CookieSessionPool: No cookie files found in %s.", self.cookies_dir)
            return

        for path in cookie_files:
            try:
                cookies = load_cookies(str(path))
                if not cookies:
                    logger.warning("CookieSessionPool: Cookie file %s is empty or invalid.", path.name)
                    continue

                session = make_session(cookies, proxy=self.proxy)
                self.sessions.append(session)
                self.session_names[session] = path.name
                self.rate_limit_resets[session] = 0.0
                logger.info("CookieSessionPool: Loaded session for cookie %s.", path.name)
            except Exception as e:
                logger.error("CookieSessionPool: Failed to load cookie from %s: %s", path.name, e)

        logger.info("CookieSessionPool: Successfully initialized with %d sessions.", len(self.sessions))

    def get_session(self) -> requests.Session | None:
        """
        Get an active session.
        If all sessions are rate-limited, returns the one that resets earliest.
        If the pool is empty, returns None.
        """
        with self.lock:
            if not self.sessions:
                return None

            now = time.time()
            
            # Find non-rate-limited sessions
            available = [s for s in self.sessions if self.rate_limit_resets.get(s, 0.0) <= now]
            
            if available:
                # To balance requests, use round-robin over available sessions
                # Ensure the index wraps around correctly
                self._current_index %= len(available)
                session = available[self._current_index]
                self._current_index = (self._current_index + 1) % len(available)
                return session

            # If all are rate-limited, sort by reset time and return the one resetting earliest
            sorted_sessions = sorted(self.sessions, key=lambda s: self.rate_limit_resets.get(s, 0.0))
            earliest_session = sorted_sessions[0]
            logger.warning(
                "CookieSessionPool: All sessions are rate-limited. Returning the earliest resetting session %s (resets in %.1fs).",
                self.session_names.get(earliest_session, "unknown"),
                max(0.0, self.rate_limit_resets.get(earliest_session, 0.0) - now)
            )
            return earliest_session

    def mark_rate_limited(self, session: requests.Session, reset_time: float) -> None:
        """Mark a session as rate-limited until a specific Unix timestamp."""
        with self.lock:
            if session in self.sessions:
                self.rate_limit_resets[session] = reset_time
                logger.warning(
                    "CookieSessionPool: Session %s marked rate-limited until %s (in %.1fs).",
                    self.session_names.get(session, "unknown"),
                    time.strftime('%H:%M:%S', time.localtime(reset_time)),
                    max(0.0, reset_time - time.time())
                )

    def remove_session(self, session: requests.Session) -> None:
        """Permanently remove a session from the pool (e.g. locked/unauthorized)."""
        with self.lock:
            if session in self.sessions:
                name = self.session_names.pop(session, "unknown")
                self.rate_limit_resets.pop(session, None)
                self.sessions.remove(session)
                logger.critical("CookieSessionPool: Permanently removed invalid session: %s", name)

    def has_available_session(self) -> bool:
        """Return True if there is at least one session not rate-limited."""
        with self.lock:
            if not self.sessions:
                return False
            now = time.time()
            return any(self.rate_limit_resets.get(s, 0.0) <= now for s in self.sessions)

    def get_earliest_wait_time(self) -> float:
        """Return the number of seconds to wait until the earliest rate-limited session resets."""
        with self.lock:
            if not self.sessions:
                return 0.0
            now = time.time()
            min_reset = min(self.rate_limit_resets.get(s, 0.0) for s in self.sessions)
            return max(1.0, min_reset - now + 2.0)  # Add a small buffer of 2 seconds

    def is_empty(self) -> bool:
        """Return True if all sessions have been removed."""
        with self.lock:
            return len(self.sessions) == 0
